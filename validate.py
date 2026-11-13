"""结果 sanity check:执行结果与 LLM 汇总的低成本校验。

为什么只警告、不阻断:
    这里的检查都是启发式规则(正则抓行数、子串匹配数字),存在误判可能;
    阻断会把正常分析误杀。统一写入 state["warnings"],由报告单列一节呈现,
    让用户自己判断,符合「禁止静默失败」而非「禁止一切不确定结果」。
"""

from __future__ import annotations

import re
from pathlib import Path

# 行数类数字:只匹配「123 行」「123 rows」这类明确表述,宁漏不误
_ROW_PATTERN = re.compile(r"(\d{1,9})\s*(?:行|rows?)", re.IGNORECASE)

# 数字 Token:支持千分位(1,234)与小数(68.3 / 800.0)
_NUMBER_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

# 纯 NaN / 标点组成的 stdout 视为「结果全空」
_ALL_EMPTY_PATTERN = re.compile(r"[NaNnan\s,.\[\]()|:+-]+")

# ---------- 汇总数字溯源:归一化标识符类数字 ----------
# 这些正则匹配的是「不是指标」的数字载体(编码 / dtype / 指纹 / run_id / 日期),
# 必须在抽数字前剔除,否则 64、8、9、075603113 这类片段会被误当成指标。

# run_id: 20261031_102930_0710a1
_RUN_ID_PATTERN = re.compile(r"\b\d{8}_\d{6}_[0-9a-z]{2,}\b", re.IGNORECASE)
# 数据指纹: name@sha12(如 测试数据.csv@b075603113f9)
_FINGERPRINT_PATTERN = re.compile(r"[^\s,，。:：()（）]+@[0-9a-f]{6,}", re.IGNORECASE)
# 裸长十六进制串(sha256 片段等)
_LONG_HEX_PATTERN = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
# ISO 日期 / 年月 / 日期时间: 2026-10-31 / 2026-10 / 2026-10-31T00:00:00
_ISO_DATE_PATTERN = re.compile(
    r"\b\d{4}-\d{2}(?:-\d{2})?(?:T\d{2}:\d{2}:\d{2})?\b"
)
# 编码名: utf-8 / utf-8-sig / gb18030 / gbk
_ENCODING_PATTERN = re.compile(r"\b(?:utf|gb)\w*(?:-\w+)*\b", re.IGNORECASE)
# dtype 名: int64 / float64 / int32 / object / str / bool / datetime64 ...
_DTYPE_PATTERN = re.compile(
    r"\b(?:int|uint|float)\d+\b"
    r"|\b(?:object|str|string|bool|boolean|category|datetime64|timedelta64)\b",
    re.IGNORECASE
)

# ---------- 汇总数字溯源:剔除口径标签与区间写法 ----------
# 分位标签(P50 / 50% 分位)与区间(60~80 / 60-80)是口径描述,不是指标数字。
# 不在此剔除会导致 method 里「5%/25%/50%/75%/90%/95%/99% 分位数」的 50 被误报。

# 分位标签: P50 / P99 / p25
_QUANTILE_LABEL_PATTERN = re.compile(r"\bP\d+\b", re.IGNORECASE)
# 百分位分位写法: 50% 分位 / 95% 分位数
_QUANTILE_PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%\s*分位(?:数)?")
# 分位列表写法: 5%/25%/50%/75%/90%/95%/99% 分位数(整串是口径说明)
# 为什么单独处理: 真实汇总会把分位标签写成百分位列表,逐个匹配会把 50 当成指标。
_QUANTILE_LIST_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*%[ \t]*[/、,，]?[ \t]*)+\s*分位(?:数)?"
)
# 区间写法: 60~80 / 60-80 / 60 至 80
_RANGE_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?")

# 候选数字 + 紧随单位:单位(% / 倍 / 个百分点)决定该数字是否值得报警
_CANDIDATE_PATTERN = re.compile(
    r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|倍|个百分点)?"
)

# 百分比:用于识别「报告里同时出现两个转化率」
_PERCENT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*%")
# 置信水平写法:95% 置信区间 / 99% CI —— 95/99 是口径常数,不是执行结果里的指标
_CONFIDENCE_PERCENT_PATTERN = re.compile(
    r"(?:95|99)\s*%\s*(?:置信|confidence|\bCI\b)", re.IGNORECASE
)
# 显著性检验痕迹:p 值 / 置信区间 / CI
# 只要正文出现任意一种,就认为 A/B 结论做了显著性检验,不再告警
_SIGNIFICANCE_PATTERN = re.compile(
    r"p\s*[=＝值]|p\s*值|p-value|置信区间|confidence\s+interval|\bCI\b",
    re.IGNORECASE,
)


def _to_float(token: str) -> float | None:
    """把数字 Token 归一化为 float;非法 Token 返回 None。

    为什么归一化:800 与 800.0 是同一数字的不同写法,不能因为格式差异误报。
    """
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def _numbers(text: str) -> list[str]:
    """提取文本中的数字 Token(保留原始写法,便于警告里展示)。"""
    return _NUMBER_PATTERN.findall(text or "")


def _dedupe(items: list[str]) -> list[str]:
    """按首次出现顺序去重,避免同一问题在报告里重复刷屏。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def validate_result(exec_result: dict, schema_info: dict | None) -> list[str]:
    """校验沙箱执行结果的基本合理性,返回警告列表(空列表表示无明显问题)。

    参数:
        exec_result: execute_code 的返回值
            {stdout, stderr, return_value, charts, files, duration_ms, exit_status}。
            注意:charts 里命中「文件缺失 / 大小为 0」的项会被就地剔除,
            避免下游继续引用坏图。
        schema_info: describe_dataset 的返回值(需含 row_count),可为空字典。

    返回:
        人读的中文警告列表。

    检查项:
        a) exit_status=success 但 stdout 为空;
        b) charts 文件不存在或大小为 0(剔除并警告);
        c) stdout 行数类数字超过数据总行数 1.5 倍;
        d) 结果全为 NaN / 空。
    """
    warnings: list[str] = []
    stdout = str(exec_result.get("stdout") or "")

    # a) 成功但没有任何输出:大概率是 LLM 忘了 print,结论无从溯源
    if exec_result.get("exit_status") == "success" and not stdout.strip():
        warnings.append("执行成功但 stdout 为空,结果未打印,无法核对数值")

    # b) 图表文件校验:路径约定是相对 outputs/ 且不带前缀
    charts = exec_result.get("charts")
    if isinstance(charts, list):
        kept: list[str] = []
        for chart in charts:
            path = Path("outputs") / str(chart)
            if not path.is_file():
                warnings.append(f"图表文件不存在: {chart}")
                continue
            if path.stat().st_size == 0:
                warnings.append(f"图表文件大小为 0(渲染失败): {chart}")
                continue
            kept.append(chart)
        exec_result["charts"] = kept

    # c) 行数类数字的粗校验:超过总行数 1.5 倍说明统计口径可能错了
    row_count = int((schema_info or {}).get("row_count") or 0)
    if row_count > 0:
        limit = row_count * 1.5
        for match in _ROW_PATTERN.finditer(stdout):
            value = int(match.group(1))
            if value > limit:
                warnings.append(
                    f"stdout 出现行数 {value},超过数据总行数 {row_count} 的 1.5 倍,请核对统计口径"
                )

    # d) 结果全空:纯 NaN / 标点,或 pandas 的空 DataFrame 输出
    stripped = stdout.strip()
    if stripped and _ALL_EMPTY_PATTERN.fullmatch(stripped):
        warnings.append("结果全部为 NaN / 空值,无法形成有效结论")
    if "Empty DataFrame" in stripped:
        warnings.append("结果为空 DataFrame(0 行),请核对筛选条件")

    return _dedupe(warnings)


def _normalize_text(text: str) -> str:
    """剔除标识符类数字后再做指标匹配。

    为什么先归一化:
        汇总文本里既有指标,也有编码名(utf-8-sig)、dtype(int64)、指纹
        (name@sha12)、run_id 与 ISO 日期;后者携带的数字不是指标,
        直接抓取会制造大量误报(64 / 8 / 9 / 075603113)。
    """
    normalized = text or ""
    for pattern in (
        _RUN_ID_PATTERN,
        _FINGERPRINT_PATTERN,
        _LONG_HEX_PATTERN,
        _ISO_DATE_PATTERN,
        _ENCODING_PATTERN,
        _DTYPE_PATTERN,
        _QUANTILE_LABEL_PATTERN,
        _QUANTILE_LIST_PATTERN,
        _QUANTILE_PERCENT_PATTERN,
        _CONFIDENCE_PERCENT_PATTERN,
        _RANGE_PATTERN,
    ):
        normalized = pattern.sub(" ", normalized)
    return normalized


def _legal_values(stdout: str, schema_info: dict | None) -> set[float]:
    """收集「合法数字来源」的归一化数值集合。

    合法来源 = stdout ∪ schema 摘要 ∪ 指纹 / 编码里的数字:
        - stdout:执行结果的原始输出(主要指标来源);
        - schema:row_count / 列数 / 各列 null_ratio / size_bytes,
          以及 name / sha256 / encoding / mtime_iso 里的数字;
        - 归一化为 float,保证 800 与 800.0 视为同一数值。
    """
    values: set[float] = set()

    def add_token(token: str) -> None:
        value = _to_float(token)
        if value is not None:
            values.add(value)

    for token in _numbers(stdout):
        add_token(token)

    schema = schema_info or {}
    for key in ("row_count", "size_bytes"):
        raw = schema.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            values.add(float(raw))

    columns = schema.get("columns")
    if isinstance(columns, list):
        # 列数本身是合法派生计数(如「共 5 个字段」)
        values.add(float(len(columns)))
        for column in columns:
            if not isinstance(column, dict):
                continue
            ratio = column.get("null_ratio")
            if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
                values.add(float(ratio))
            # 本次新增的质量统计字段也是合法数字来源:汇总可能引用缺失数 /
            # 异常值数 / 负值数 / 极值,若不纳入会被误报为「自造数字」
            for key in ("n_unique", "outlier_count", "negative_count", "min", "max"):
                raw = column.get(key)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    values.add(float(raw))

    # 整表质量统计(duplicate_rows)同样合法
    dup = schema.get("duplicate_rows")
    if isinstance(dup, (int, float)) and not isinstance(dup, bool):
        values.add(float(dup))

    # 指纹 / 编码 / 文件名里的数字同样合法(如 测试数据.csv、utf-8-sig)
    for key in ("name", "sha256", "encoding", "mtime_iso"):
        raw = schema.get(key)
        if raw:
            for token in _numbers(str(raw)):
                add_token(token)

    return values


def _is_notable(token: str, unit: str) -> bool:
    """判断数字是否「值得报警」,避免裸个小位数刷屏。

    只报:带小数点、带千分位、≥100 的整数、或紧跟 % / 倍 / 个百分点 的数字。
    单个数字(1 / 5 / 8 / 9)通常只是字段数、行差或标识符片段,一律跳过。
    """
    if unit:
        return True
    if "." in token or "," in token:
        return True
    value = _to_float(token)
    return value is not None and value >= 100


def validate_report(
    summary: dict | None, stdout: str, schema_info: dict | None = None
) -> list[str]:
    """校验 LLM 汇总里的指标数字是否都能在合法来源中找到,返回警告列表。

    参数:
        summary: ReportSummary.model_dump() 的结果(headline / findings /
            recommendations / caveats / method),可为 None。
        stdout: 沙箱执行的原始输出(主要数字溯源基准)。
        schema_info: describe_dataset 的返回值,提供 row_count / 列数 /
            null_ratio / size_bytes / 指纹 / 编码等合法数字来源。

    返回:
        警告列表。summary 中出现但合法来源里找不到的「值得报警」数字,
        记为「汇总疑似自造数字」。

    为什么只警告不阻断:
        LLM 可能把 "68.30%" 写成 "68.3%",或做无害的措辞改写;严格阻断误杀率高。
        但这类数字一旦出现,报告中必须提示用户人工复核(规则 1.1 第 6/7 条)。
        裸个位数(字段数 / 行差 / dtype 片段)不算指标,不报警。
    """
    if not summary:
        return []

    legal_values = _legal_values(stdout, schema_info)

    fields: list[str] = []
    headline = summary.get("headline")
    if isinstance(headline, str):
        fields.append(headline)
    for key in ("findings", "recommendations", "caveats"):
        values = summary.get(key) or []
        if isinstance(values, list):
            fields.extend(str(item) for item in values)
    method = summary.get("method")
    if isinstance(method, str):
        fields.append(method)

    missing: list[str] = []
    for text in fields:
        # 先剔除编码 / dtype / 指纹 / run_id / 日期,再做数字匹配
        normalized = _normalize_text(text)
        for match in _CANDIDATE_PATTERN.finditer(normalized):
            token = match.group(1)
            unit = match.group(2) or ""
            if not _is_notable(token, unit):
                continue
            value = _to_float(token)
            if value is None or value in legal_values:
                continue
            if token not in missing:
                missing.append(token)

    warnings: list[str] = []
    for token in missing:
        warnings.append(f"汇总疑似自造数字: {token}(执行输出中未找到,请人工复核)")

    # A/B 结构告警:正文提到转化率且出现两个及以上百分比(通常是两组转化率),
    # 但完全没有 p 值 / 置信区间的痕迹,提示结论缺少显著性检验(只告警不阻断)。
    # 加「转化」前置条件,避免把分位数列表(5%/25%/50%...)误判成 A/B 结论。
    report_text = "\n".join(fields)
    if (
        "转化" in report_text
        and len(_PERCENT_PATTERN.findall(report_text)) >= 2
        and not _SIGNIFICANCE_PATTERN.search(report_text)
    ):
        warnings.append(
            "A/B 结论缺少显著性检验: 报告出现多个转化率,但未给出 p 值或 95% 置信区间"
        )

    return _dedupe(warnings)
