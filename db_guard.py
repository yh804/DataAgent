"""执行前 SQL 体检:静态识别「大表 + 重查询」写法,返回 ok / warn / block。

设计要点:
- 纯函数、无 IO:表规模由调用方从 information_schema.TABLES 或 COUNT(*) 取,
  本模块只做 SQL 文本解析与阈值判定,便于单元测试;
- 检测前先剥离字符串字面量与注释,避免把 'group by' 这类字面量误判成关键字;
- 同时命中两条及以上规则才判 block,单条命中只提示 warn。

调用方约定 table_stats 结构:
    {"rows": 最大表行数, "size_bytes": 最大表体积(字节), "text_columns": [文本列名]}
缺字段或传 None 时按「小表」处理,只保留与表规模无关的规则。
"""

from __future__ import annotations

import re

# 大表阈值:行数超过 20 万视为大表(与提示词第 6 条口径一致)
BIG_TABLE_ROWS = 200_000
# 单条 SELECT 中 COUNT(DISTINCT ...) 的容忍上限:最多 2 个
MAX_COUNT_DISTINCT = 2
# 同时命中的规则数达到该值即 block,否则 warn
BLOCK_REASON_COUNT = 2

# GROUP BY 子句:截到 HAVING / ORDER BY / LIMIT 为止,避免把后续子句算进来
_GROUP_BY_RE = re.compile(
    r"\bgroup\s+by\b(.*?)(?:\bhaving\b|\border\s+by\b|\blimit\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_COUNT_DISTINCT_RE = re.compile(r"\bcount\s*\(\s*distinct\b", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
_AS_ALIAS_RE = re.compile(r"\bas\s+([0-9A-Za-z_\u4e00-\u9fff]+)\s*$", re.IGNORECASE)

# GROUP BY 表达式里的关键字,不参与文本列比对
_GROUP_BY_KEYWORDS = {
    "as",
    "and",
    "or",
    "case",
    "when",
    "then",
    "else",
    "end",
    "null",
    "asc",
    "desc",
    "true",
    "false",
}


def _strip_literals(sql: str) -> str:
    """剥离字符串字面量与注释,保留关键字与标识符内容。

    Args:
        sql: 原始 SQL 文本。

    Returns:
        去掉单引号字符串、-- / # 行注释与 /* */ 块注释后的文本;
        引号包裹的标识符(`列名` / "列名")保留内容并去掉引号,便于列名比对。
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            # 单引号字符串:支持 '' 转义与反斜杠转义
            index += 1
            while index < length:
                if sql[index] == "\\" and index + 1 < length:
                    index += 2
                    continue
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            out.append(" ")
            continue
        if char in ("`", '"'):
            # 标识符:保留内容,去掉包裹符号
            quote = char
            index += 1
            buffer: list[str] = []
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        buffer.append(quote)
                        index += 2
                        continue
                    index += 1
                    break
                buffer.append(sql[index])
                index += 1
            out.append("".join(buffer))
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            while index < length and sql[index] != "\n":
                index += 1
            out.append(" ")
            continue
        if char == "#":
            while index < length and sql[index] != "\n":
                index += 1
            out.append(" ")
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index = min(index + 2, length)
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_top_level(expression: str) -> list[str]:
    """按顶层逗号拆分 GROUP BY 表达式,忽略函数括号内的逗号。

    Args:
        expression: GROUP BY 之后的表达式文本。

    Returns:
        拆分后的表达式列表,已去掉空白项。
    """
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == "," and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _group_by_columns(sql: str) -> list[list[str]]:
    """提取 GROUP BY 每个表达式里的标识符 token。

    Args:
        sql: 已剥离字面量与注释的 SQL。

    Returns:
        每个 GROUP BY 表达式对应一个 token 列表;没有 GROUP BY 时返回 []。
    """
    match = _GROUP_BY_RE.search(sql)
    if not match:
        return []
    columns: list[list[str]] = []
    for item in _split_top_level(match.group(1)):
        alias = _AS_ALIAS_RE.search(item)
        if alias:
            # 去掉 AS 别名,只保留被聚合的表达式
            item = item[: alias.start()]
        tokens = [
            token
            for token in _IDENTIFIER_RE.findall(item)
            if token.lower() not in _GROUP_BY_KEYWORDS
        ]
        if tokens:
            columns.append(tokens)
    return columns


def _normalize_text_columns(table_stats: dict | None) -> set[str]:
    """把 table_stats.text_columns 归一成小写集合,非列表时返回空集合。"""
    raw = (table_stats or {}).get("text_columns") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(name).strip().lower() for name in raw if str(name).strip()}


def _table_rows(table_stats: dict | None) -> int:
    """读取 table_stats.rows,非法或缺失时按 0 处理。"""
    try:
        return int((table_stats or {}).get("rows") or 0)
    except (TypeError, ValueError):
        return 0


def assess(sql: str, table_stats: dict | None = None) -> dict:
    """静态评估 SQL 是否存在「大表 + 重查询」风险。

    Args:
        sql: 待执行的单条只读 SQL。
        table_stats: 调用方提供的表规模统计,结构见模块文档;
            缺字段或为空时按小表处理,只保留与表规模无关的规则。

    Returns:
        {"level": "ok"|"warn"|"block", "reasons": [原因, ...]}。
        命中 0 条为 ok,命中 1 条为 warn,命中 2 条及以上为 block。
    """
    reasons: list[str] = []
    sanitized = _strip_literals(str(sql or "")).strip()
    if not sanitized:
        # 空 SQL 由调用方报错,体检不额外产生噪音
        return {"level": "ok", "reasons": []}
    rows = _table_rows(table_stats)
    text_columns = _normalize_text_columns(table_stats)

    # 规则 1:大表 + GROUP BY 文本列
    if rows > BIG_TABLE_ROWS and text_columns:
        hits: list[str] = []
        for tokens in _group_by_columns(sanitized):
            matched = next((token for token in tokens if token.lower() in text_columns), "")
            if matched and matched not in hits:
                hits.append(matched)
        if hits:
            reasons.append(
                f"表约 {rows} 行,GROUP BY 了文本列「{'、'.join(hits)}」;"
                "建议先按时间/状态过滤,或先 SELECT DISTINCT 取维度值再 JOIN 聚合"
            )

    # 规则 2:COUNT(DISTINCT ...) 过多(与表规模无关,任何表都限流)
    distinct_count = len(_COUNT_DISTINCT_RE.findall(sanitized))
    if distinct_count > MAX_COUNT_DISTINCT:
        reasons.append(
            f"单条 SQL 含 {distinct_count} 个 COUNT(DISTINCT ...);"
            f"建议不超过 {MAX_COUNT_DISTINCT} 个,且只对低基数列使用"
        )

    # 规则 3:大表且既无 WHERE 过滤也无 LIMIT 采样
    if rows > BIG_TABLE_ROWS and not _WHERE_RE.search(sanitized) and not _LIMIT_RE.search(
        sanitized
    ):
        reasons.append(
            f"表约 {rows} 行且既无 WHERE 过滤也无 LIMIT 采样;"
            f"建议先过滤或加 LIMIT {BIG_TABLE_ROWS} 抽样"
        )

    if len(reasons) >= BLOCK_REASON_COUNT:
        level = "block"
    elif reasons:
        level = "warn"
    else:
        level = "ok"
    return {"level": level, "reasons": reasons}
