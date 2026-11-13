"""SQLite 只读访问层：供「数据库问答」入口在主进程使用。

设计约束：
- 沙箱以 --network none 启动,无法连接数据库;因此所有数据库访问都在主进程完成,
  查询结果导出为 CSV 后再交给分析 Agent,沙箱只消费文件。
- 连接以只读 URI 打开(mode=ro),SQL 只允许单条 SELECT / WITH,
  禁止任何写操作与多语句,命中即抛 ValueError,绝不执行。
- 查询设置超时,避免 LLM 生成的低效 SQL 长时间占用进程。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd

_SQLITE_TIMEOUT_DEFAULT_SECONDS = 30.0


def _sqlite_timeout_from_env() -> float:
    """读取 SQLite 查询超时配置,非法或非正数回退到默认值。

    Returns:
        查询超时秒数(正数,默认 30)。
    """
    try:
        value = float(
            os.getenv("DB_QUERY_TIMEOUT_SECONDS", str(_SQLITE_TIMEOUT_DEFAULT_SECONDS))
        )
    except (TypeError, ValueError):
        return _SQLITE_TIMEOUT_DEFAULT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _SQLITE_TIMEOUT_DEFAULT_SECONDS
    return value


# 单次查询默认超时(秒)与最大返回行数
DEFAULT_QUERY_TIMEOUT = _sqlite_timeout_from_env()
DEFAULT_MAX_ROWS = 10000


class QueryCancelled(RuntimeError):
    """查询被用户主动取消(客户端断开 / 点击取消)。"""

# 禁止出现的关键字：覆盖写操作、DDL、挂载其他库、PRAGMA 等副作用语句。
# 用 \b 词边界匹配,避免 created_at 这类列名被误判。
_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "attach",
    "detach",
    "pragma",
    "vacuum",
)
# 限制用户只能执行 SELECT 查询语句,拦截修改、删除、改动数据库结构的 SQL
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)

# 语句必须以 SELECT 或 WITH 开头(WITH 用于 CTE)
_SELECT_PREFIX_RE = re.compile(r"^(select|with)\b", re.IGNORECASE)

# 判断语句末尾是否已有 LIMIT：只有末尾的 LIMIT 才能限制整体返回行数,
# 子查询里的 LIMIT 不算(否则会漏加外层限制)
_TRAILING_LIMIT_RE = re.compile(
    r"\blimit\s+\d+(\s*,\s*\d+|\s+offset\s+\d+)?\s*$", re.IGNORECASE
)


def _strip_comments(sql: str) -> str:
    """去掉 SQL 注释,返回用于安全校验与执行的语句。

    为什么不用正则替换：
        -- 可能出现在字符串字面量里(如 'A--B'),正则会把字面量一起删掉;
        这里用逐字符扫描并跟踪引号,保证只删真正的注释。
    """
    out: list[str] = []
    i = 0
    quote_char: str | None = None
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote_char:
            out.append(ch)
            if ch == quote_char:
                # 处理 '' / "" 转义：两个相同引号表示一个字面量引号
                if i + 1 < n and sql[i + 1] == quote_char:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote_char = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote_char = ch
            out.append(ch)
            i += 1
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i)
            if newline == -1:
                break
            i = newline  # 保留换行,避免把前后 token 粘在一起
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def validate_select(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """校验并规范化 LLM 生成的 SQL,返回可直接执行的语句。

    参数：
        sql: LLM 生成的原始 SQL。
        max_rows: 未显式写 LIMIT 时追加的最大行数。

    返回：
        去注释、去尾部单分号、必要时追加 LIMIT 的可执行 SQL。

    异常：
        ValueError: 空语句、非 SELECT/WITH、多语句或含禁止关键字。
    """
    cleaned = _strip_comments(sql).strip()
    if not cleaned:
        raise ValueError("SQL 为空,拒绝执行")

    lowered = cleaned.lower()
    if not _SELECT_PREFIX_RE.match(lowered):
        raise ValueError("只允许单条 SELECT / WITH 查询")

    # 允许结尾单个分号;去掉后若仍有分号,说明是多条语句
    body = cleaned[:-1].rstrip() if cleaned.endswith(";") else cleaned
    if ";" in body:
        raise ValueError("检测到多条语句,只允许单条 SELECT / WITH")

    hit = _FORBIDDEN_RE.search(body)
    if hit:
        raise ValueError(f"SQL 含禁止关键字: {hit.group(0).upper()},拒绝执行")

    # 末尾没有 LIMIT 时补上,防止一次性拉全表
    if not _TRAILING_LIMIT_RE.search(body):
        body = f"{body}\nLIMIT {int(max_rows)}"
    return body


def _connect_readonly(db_path: str | Path, timeout: float = DEFAULT_QUERY_TIMEOUT) -> sqlite3.Connection:
    """以只读 URI 打开 SQLite 连接(mode=ro)。

    异常：
        FileNotFoundError: 数据库文件不存在。
    """
    path = Path(db_path).expanduser().resolve()  # 确保路径绝对化
    if not path.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {path}")
    # URI 中的中文 / 空格必须百分号编码,safe 保留路径分隔符与冒号(Windows 盘符)
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout)

def _quote_ident(name: str) -> str:
    """把标识符安全地包进双引号(内部双引号转义)。"""
    return '"' + name.replace('"', '""') + '"'


def list_tables(db_path: str | Path) -> list[dict[str, Any]]:
    """列出库内所有用户表及其列结构。

    参数：
        db_path: SQLite 文件路径。

    返回：
        [{"name": 表名, "columns": [{"name": 列名, "type": 类型}]}]
        忽略 sqlite_ 前缀的系统表。

    异常：
        FileNotFoundError: 数据库文件不存在。
    """
    # closing 保证连接一定关闭：sqlite3 的 with 只提交/回滚,不会关闭连接
    with closing(_connect_readonly(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()    # 一次查询所有表名,避免重复查询

        tables: list[dict[str, Any]] = []
        for (table_name,) in rows:
            # PRAGMA table_info 是只读元数据查询,用它拿到列名与声明类型
            info = conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()
            tables.append(
                {
                    "name": table_name,
                    "columns": [
                        {"name": column[1], "type": column[2] or ""} for column in info
                    ],
                }
            )
    return tables


def describe_table(
    db_path: str | Path,
    table: str,
    sample_rows: int = 5,
) -> dict[str, Any]:
    """返回指定表的列结构与少量抽样行。

    参数：
        db_path: SQLite 文件路径。
        table: 表名(必须存在于库中)。
        sample_rows: 抽样行数,仅用于让 LLM 了解取值形态。

    返回：
        {"columns": [...], "sample": [最多 sample_rows 行]}

    异常：
        ValueError: 表不存在或 sample_rows 非法。
        FileNotFoundError: 数据库文件不存在。
    """
    if sample_rows <= 0:
        raise ValueError(f"sample_rows 必须为正整数,收到: {sample_rows}")

    tables = {item["name"]: item for item in list_tables(db_path)}
    if table not in tables:
        raise ValueError(f"表不存在: {table},可用表: {list(tables)}")

    frame = run_select(
        db_path,
        f"SELECT * FROM {_quote_ident(table)} LIMIT {int(sample_rows)}",
        max_rows=sample_rows,
    )["dataframe"]
    # to_json 可把 NaN 转 null、numpy 类型转原生类型,保证结果可序列化
    sample = json.loads(
        frame.to_json(orient="records", date_format="iso", force_ascii=False)
    )
    return {"columns": tables[table]["columns"], "sample": sample}


def run_select(
    db_path: str | Path,
    sql: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """执行只读 SELECT,返回 DataFrame 与行数。

    参数：
        db_path: SQLite 文件路径。
        sql: 待执行 SQL,必须先通过 validate_select 的安全校验。
        max_rows: 未写 LIMIT 时自动追加的最大行数。
        timeout: 查询超时秒数,超时抛 TimeoutError。
        cancel_event: 取消信号;置位后由进度回调中断查询并抛 QueryCancelled。

    返回：
        {"dataframe": pd.DataFrame, "row_count": int}

    异常：
        ValueError: SQL 未通过安全校验。
        TimeoutError: 查询超时。
        QueryCancelled: 被用户取消。
        FileNotFoundError: 数据库文件不存在。
        sqlite3.Error: SQL 执行失败(语法错误 / 表列不存在等)。
    """
    safe_sql = validate_select(sql, max_rows=max_rows)
    deadline = time.monotonic() + timeout

    def _should_interrupt() -> int:
        """每 1000 条 VM 指令回调一次:取消优先于超时。"""
        if cancel_event is not None and cancel_event.is_set():
            return 1
        return 1 if time.monotonic() > deadline else 0

    with closing(_connect_readonly(db_path, timeout=timeout)) as conn:
        # 进度回调每 1000 个 VM 指令检查一次耗时,超时返回非 0 让 SQLite 中断查询;
        # Windows 没有 signal.alarm,这是跨平台的超时方案
        conn.set_progress_handler(_should_interrupt, 1000)
        try:
            frame = pd.read_sql_query(safe_sql, conn)
        except (sqlite3.OperationalError, pd.errors.DatabaseError) as exc:
            # pandas 会把 sqlite3 的异常包成 DatabaseError,两种都要处理
            if "interrupted" in str(exc).lower():
                if cancel_event is not None and cancel_event.is_set():
                    raise QueryCancelled("查询已取消") from exc
                raise TimeoutError(
                    f"查询超过 {timeout:g} 秒未完成,已中断;请缩小查询范围后重试"
                ) from exc
            if isinstance(exc, pd.errors.DatabaseError):
                # 统一转回 sqlite3.Error,调用方只需处理一种执行异常
                raise sqlite3.OperationalError(str(exc)) from exc
            raise
        finally:
            conn.set_progress_handler(None, 1000)

    return {"dataframe": frame, "row_count": len(frame)}
