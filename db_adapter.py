"""数据库适配器：统一 MySQL / SQLite 的测试、元数据与只读查询接口。

安全边界：
- 所有查询在主进程执行,沙箱只消费导出的 CSV;
- SQL 必须经 db.validate_select 校验后才执行,适配器不提供写接口;
- MySQL 连接建立后立即 `SET SESSION TRANSACTION READ ONLY`,即使账号有写权限,
  当前会话也只能读(只读账号是第二道闸);
- 凭据只存在于调用方内存,本模块不落盘、不打日志。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from typing import Any

import pandas as pd

import db as sqlite_db
import db_cache

# 取消异常统一由底层定义,调用方只处理 db_adapter.QueryCancelled 一种类型
QueryCancelled = sqlite_db.QueryCancelled

try:  # pymysql 只在主进程使用,沙箱环境无需安装
    import pymysql
except ImportError:  # pragma: no cover - 依赖缺失时由 _connect 给出人话错误
    pymysql = None

MYSQL_DEFAULT_PORT = 3306
MYSQL_CONNECT_TIMEOUT = 5
_MYSQL_TIMEOUT_DEFAULT_MS = 30000
_MYSQL_TIMEOUT_MIN_MS = 1000
_MYSQL_TIMEOUT_MAX_MS = 300000


def _mysql_timeout_from_env() -> int:
    """读取 MySQL 查询超时配置,非法或越界值回退到默认值。

    Returns:
        查询超时毫秒数(范围 1000~300000,默认 30000)。
    """
    try:
        value = int(os.getenv("MYSQL_QUERY_TIMEOUT_MS", str(_MYSQL_TIMEOUT_DEFAULT_MS)))
    except (TypeError, ValueError):
        return _MYSQL_TIMEOUT_DEFAULT_MS
    if not _MYSQL_TIMEOUT_MIN_MS <= value <= _MYSQL_TIMEOUT_MAX_MS:
        return _MYSQL_TIMEOUT_DEFAULT_MS
    return value


# MySQL 8 的 MAX_EXECUTION_TIME 仅对 SELECT 生效;socket 读超时必须更晚
MYSQL_QUERY_TIMEOUT_MS = _mysql_timeout_from_env()
MYSQL_READ_TIMEOUT = MYSQL_QUERY_TIMEOUT_MS / 1000 + 5

# 仅用于超时后的 information_schema 补充查询,取首个真实表引用
_TABLE_REFERENCE_RE = re.compile(
    r"\b(?:from|join)\s+(?:(?:`[^`]+`|\w+)\s*\.\s*)?(?:`([^`]+)`|(\w+))",
    re.IGNORECASE,
)

MYSQL_KIND = "mysql"
SQLITE_KIND = "sqlite"

# MySQL 分批取数:每批检查一次取消信号,避免一次性拉满内存也无法及时响应取消
_FETCH_BATCH_SIZE = 1000

# 视为文本类型的列类型关键字:命中任意一个即参与 GROUP BY 文本列检测
_TEXT_TYPE_HINTS = ("char", "text", "clob", "enum", "set", "json")


class AdapterError(RuntimeError):
    """数据库连接或查询失败,message 面向用户可直接展示。"""


def _translate_mysql_error(
    exc: BaseException,
    *,
    elapsed: float | None = None,
    table_stats: tuple[int, float] | None = None,
    timeout_ms: int | None = None,
) -> BaseException:
    """把 pymysql 异常翻译成可展示的中文错误。

    Args:
        exc: pymysql 抛出的原始异常。
        elapsed: 查询实际已等待秒数(仅超时错误使用,未知时省略)。
        table_stats: 首个表的约略行数与体积 MB(仅超时错误使用,未知时省略)。
        timeout_ms: 本次查询实际生效的超时毫秒数(默认取 MYSQL_QUERY_TIMEOUT_MS)。

    Returns:
        AdapterError(或 TimeoutError):带错误码与处理建议的异常实例。
    """
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    message = str(exc)
    if code == 1045:
        return AdapterError("MySQL 用户名或密码错误(1045),请检查账号与密码")
    if code == 2003:
        return AdapterError("无法连接到 MySQL 服务器(2003),请检查主机、端口与网络")
    if code == 1049:
        return AdapterError("数据库不存在(1049),请检查库名")
    if code == 1044:
        return AdapterError("账号没有访问该库的权限(1044),请检查只读账号授权")
    if code == 3024:
        waited = f"(已等待 {elapsed:.1f}s)" if elapsed is not None else ""
        stats = (
            f"；该表约 {table_stats[0]} 行/{table_stats[1]:.1f}MB"
            if table_stats is not None
            else ""
        )
        effective_ms = timeout_ms if timeout_ms is not None else MYSQL_QUERY_TIMEOUT_MS
        return TimeoutError(
            f"查询超过 {effective_ms / 1000:g} 秒未完成{waited}{stats}，"
            "建议加 WHERE 缩小范围，或把表转为 InnoDB 并调大 innodb_buffer_pool_size"
        )
    return AdapterError(f"MySQL 连接/查询失败({code}): {message}")


def _mysql_table_stats(cursor: Any, database: str, sql: str) -> tuple[int, float] | None:
    """在查询超时后尽力读取首个表的约略行数与体积。

    Args:
        cursor: 已连接的 MySQL 游标。
        database: 当前业务库名。
        sql: 已超时的 SQL,用于识别首个 FROM/JOIN 表名。

    Returns:
        (约略行数, 体积 MB);表名无法识别或元数据查询失败时返回 None。
    """
    match = _TABLE_REFERENCE_RE.search(sql)
    if not match:
        return None
    table = match.group(1) or match.group(2)
    try:
        cursor.execute(
            "SELECT TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
            "FROM information_schema.tables WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (database, table),
        )
        row = cursor.fetchone()
        if not row:
            return None
        rows = int(row[0] or 0)
        size_bytes = int(row[1] or 0) + int(row[2] or 0)
        return rows, size_bytes / (1024 * 1024)
    except Exception:  # noqa: BLE001 - 统计信息只是增强提示,不能掩盖原超时
        return None


def _text_columns(tables: list[dict[str, Any]]) -> list[str]:
    """从表元数据里挑出文本类型列名,供执行前体检识别 GROUP BY 文本列。

    Args:
        tables: list_tables 的返回值,每项含 columns[{name, type, ...}]。

    Returns:
        去重后的文本列名列表;没有文本列时返回 []。
    """
    names: list[str] = []
    for table in tables or []:
        for column in table.get("columns") or []:
            data_type = str(column.get("type") or "").lower()
            if any(hint in data_type for hint in _TEXT_TYPE_HINTS):
                name = str(column.get("name") or "")
                if name and name not in names:
                    names.append(name)
    return names


def _to_export_value(value: Any) -> Any:
    """把数据库驱动类型转成 CSV 友好的原生类型,避免出现 b'..' 与 Decimal(...)。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, datetime_time)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


class MysqlAdapter:
    """MySQL 适配器:连接、元数据浏览与只读 SELECT 执行。"""

    kind = MYSQL_KIND

    def _connect(self, conn: dict[str, Any], read_timeout: float | None = None):
        """建立 MySQL 连接,并开启只读会话。

        Args:
            conn: 含 host / port / user / password / database / ssl 的运行时配置。
            read_timeout: socket 读超时秒数;默认使用模块级 MYSQL_READ_TIMEOUT,
                block 重写后的长查询会传入更大的值。

        Returns:
            pymysql.Connection。

        Raises:
            AdapterError: 依赖缺失或连接失败(已翻译成人话)。
        """
        if pymysql is None:
            raise AdapterError(
                "未安装 pymysql,无法连接 MySQL;请执行 pip install pymysql==1.1.1"
            )

        ssl_option = {"check_hostname": False} if conn.get("ssl") else None
        try:
            connection = pymysql.connect(
                host=str(conn.get("host") or ""),
                port=int(conn.get("port") or MYSQL_DEFAULT_PORT),
                user=str(conn.get("user") or ""),
                password=str(conn.get("password") or ""),
                database=str(conn.get("database") or ""),
                charset="utf8mb4",
                connect_timeout=MYSQL_CONNECT_TIMEOUT,
                read_timeout=(
                    read_timeout if read_timeout is not None else MYSQL_READ_TIMEOUT
                ),
                autocommit=False,
                ssl=ssl_option,
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻译驱动异常
            raise _translate_mysql_error(exc) from exc

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
        except Exception as exc:  # noqa: BLE001
            connection.close()
            raise _translate_mysql_error(exc) from exc
        return connection

    def test(self, conn: dict[str, Any]) -> dict[str, Any]:
        """测试连接并返回服务器版本与耗时。

        Args:
            conn: 运行时连接配置(含 password)。

        Returns:
            {"ok": True, "version": "8.x", "elapsed_ms": 12.3}
        """
        started = time.perf_counter()
        connection = self._connect(conn)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                row = cursor.fetchone()
            version = str(row[0]) if row else ""
        except Exception as exc:  # noqa: BLE001
            raise _translate_mysql_error(exc) from exc
        finally:
            connection.close()
        return {
            "ok": True,
            "version": version,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def list_tables(self, conn: dict[str, Any]) -> list[dict[str, Any]]:
        """列出库内表与视图,并附带列元数据。

        Args:
            conn: 运行时连接配置。

        Returns:
            [{"name": 表名, "type": "BASE TABLE"/"VIEW", "columns": [...]}]
        """
        database = str(conn.get("database") or "")
        connection = self._connect(conn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                    (database,),
                )
                table_rows = cursor.fetchall()
                cursor.execute(
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY "
                    "FROM information_schema.columns "
                    "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
                    (database,),
                )
                column_rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise _translate_mysql_error(exc) from exc
        finally:
            connection.close()

        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for table_name, column_name, data_type, nullable, column_key in column_rows:
            columns_by_table.setdefault(str(table_name), []).append(
                {
                    "name": str(column_name),
                    "type": str(data_type),
                    "nullable": str(nullable).upper() == "YES",
                    "key": str(column_key or ""),
                }
            )

        return [
            {
                "name": str(table_name),
                "type": str(table_type),
                "columns": columns_by_table.get(str(table_name), []),
            }
            for table_name, table_type in table_rows
        ]

    def describe_table(self, conn: dict[str, Any], name: str) -> dict[str, Any]:
        """返回单表/视图的列元数据。

        Args:
            conn: 运行时连接配置。
            name: 表名或视图名,必须存在于库中。

        Returns:
            {"name": ..., "type": ..., "columns": [...]}

        Raises:
            ValueError: 表不存在。
        """
        for table in self.list_tables(conn):
            if table["name"] == name:
                return table
        raise ValueError(f"表或视图不存在: {name}")

    def table_stats(
        self, conn: dict[str, Any], tables: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """从 information_schema 读取表规模,并汇总列元数据里的文本列。

        Args:
            conn: 运行时连接配置。
            tables: list_tables 的返回值,用于识别文本列。

        Returns:
            {"rows": 最大表行数, "size_bytes": 最大表数据体积(字节),
             "text_columns": [文本列名]}。

        Raises:
            AdapterError: 元数据查询失败。
        """
        database = str(conn.get("database") or "")
        connection = self._connect(conn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_ROWS, DATA_LENGTH FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                    (database,),
                )
                rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 - 统一翻译驱动异常
            raise _translate_mysql_error(exc) from exc
        finally:
            connection.close()

        return {
            "rows": max((int(row[0] or 0) for row in rows), default=0),
            "size_bytes": max((int(row[1] or 0) for row in rows), default=0),
            "text_columns": _text_columns(tables),
        }

    def explain(self, conn: dict[str, Any], sql: str) -> str:
        """用 EXPLAIN 校验 SQL 引用的表 / 列是否存在(只做解析,不取数据)。

        Args:
            conn: 运行时连接配置。
            sql: 待校验 SQL,只能是单条 SELECT / WITH。

        Returns:
            空串表示通过;否则返回错误文本(常见为表 / 列不存在)。

        Raises:
            ValueError: SQL 未通过只读安全校验。
            AdapterError: 连接失败。
        """
        safe_sql = sqlite_db.validate_select(sql)
        connection = self._connect(conn)
        try:
            with connection.cursor() as cursor:
                try:
                    cursor.execute("EXPLAIN " + safe_sql)
                    cursor.fetchall()
                except Exception as exc:  # noqa: BLE001 - 计划错误原样返回给调用方
                    return str(_translate_mysql_error(exc))
        finally:
            connection.close()
        return ""

    def run_select(
        self,
        conn: dict[str, Any],
        sql: str,
        max_rows: int = sqlite_db.DEFAULT_MAX_ROWS,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """执行经 db.validate_select 校验的只读 SELECT。

        Args:
            conn: 运行时连接配置。
            sql: 待执行 SQL,只能是单条 SELECT / WITH。
            max_rows: 最多返回行数,超出部分截断。
            timeout_seconds: 本次查询超时秒数;默认使用 MYSQL_QUERY_TIMEOUT_MS,
                block 重写后的执行会传入更长的超时。
            cancel_event: 取消信号;置位后尽快关闭连接并抛 QueryCancelled。

        Returns:
            {"dataframe": pd.DataFrame, "row_count": int, "truncated": bool, "cached": bool}

        Raises:
            ValueError: SQL 未通过安全校验。
            TimeoutError: 查询超时。
            QueryCancelled: 被用户取消。
            AdapterError: 连接或执行失败。
        """
        safe_sql = sqlite_db.validate_select(sql, max_rows=max_rows)
        cache_id = db_cache.connection_cache_id(conn, self.kind)
        cached = db_cache.load(cache_id, sql)
        if cached is not None:
            return cached
        timeout_ms = (
            int(round(timeout_seconds * 1000))
            if timeout_seconds is not None
            else MYSQL_QUERY_TIMEOUT_MS
        )
        # socket 读超时必须晚于服务端 MAX_EXECUTION_TIME,否则会先触发驱动层超时
        connection = self._connect(conn, read_timeout=timeout_ms / 1000 + 5)
        watcher_stop = threading.Event()
        if cancel_event is not None:
            def _watch_cancel() -> None:
                """查询尚在服务端执行时,只有关闭连接才能立即打断阻塞的读。"""
                while not watcher_stop.wait(0.2):
                    if cancel_event.is_set():
                        try:
                            connection.close()
                        except Exception:  # noqa: BLE001 - 关闭失败也要退出
                            pass
                        return

            threading.Thread(target=_watch_cancel, daemon=True).start()

        try:
            with connection.cursor() as cursor:
                # 尽力而为地限制执行时间;旧版 MySQL/MariaDB 不支持时忽略
                try:
                    cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={timeout_ms}")
                except Exception:  # noqa: BLE001
                    pass
                started = time.perf_counter()
                try:
                    cursor.execute(safe_sql)
                    rows: list[Any] = []
                    # 多取一行用于判断是否被 max_rows 截断
                    while len(rows) <= max_rows:
                        if cancel_event is not None and cancel_event.is_set():
                            raise QueryCancelled("查询已取消")
                        chunk = cursor.fetchmany(_FETCH_BATCH_SIZE)
                        if not chunk:
                            break
                        rows.extend(chunk)
                        # fetchmany 返回不足一批即表示结果已取完
                        if len(chunk) < _FETCH_BATCH_SIZE:
                            break
                    columns = [item[0] for item in (cursor.description or [])]
                except QueryCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if cancel_event is not None and cancel_event.is_set():
                        raise QueryCancelled("查询已取消") from exc
                    elapsed = time.perf_counter() - started
                    is_timeout = bool(exc.args and exc.args[0] == 3024)
                    table_stats = (
                        _mysql_table_stats(
                            cursor,
                            str(conn.get("database") or ""),
                            safe_sql,
                        )
                        if is_timeout
                        else None
                    )
                    raise _translate_mysql_error(
                        exc,
                        elapsed=elapsed,  # 实际驱动执行与取数耗时,用于超时诊断
                        table_stats=table_stats,  # 仅在 3024 后尽力补充,失败则为 None
                        timeout_ms=timeout_ms,  # 本次生效的超时,长查询提示不会被说成 30 秒
                    ) from exc
        except QueryCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 取消时可能先关闭连接导致游标创建失败
            if cancel_event is not None and cancel_event.is_set():
                raise QueryCancelled("查询已取消") from exc
            raise
        finally:
            watcher_stop.set()
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - 连接可能已被取消线程关闭
                pass

        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        frame = pd.DataFrame(
            [[_to_export_value(value) for value in row] for row in rows],
            columns=columns,
        )
        db_cache.save(
            cache_id,
            sql,
            frame,
            truncated=truncated,  # 命中缓存后保留首次查询的截断状态
        )
        return {
            "dataframe": frame,
            "row_count": len(frame),
            "truncated": truncated,
            "cached": False,
        }


class SqliteAdapter:
    """SQLite 适配器:复用 db.py 的只读实现,供测试与 examples/ecommerce.db 使用。"""

    kind = SQLITE_KIND

    @staticmethod
    def _path(conn: dict[str, Any]) -> str:
        """从连接配置中取 SQLite 文件路径。"""
        return str(conn.get("path") or conn.get("database") or "")

    def test(self, conn: dict[str, Any]) -> dict[str, Any]:
        """打开只读连接并返回 SQLite 版本。"""
        started = time.perf_counter()
        path = self._path(conn)
        with closing(sqlite_db._connect_readonly(path)) as connection:
            version = connection.execute("SELECT sqlite_version()").fetchone()[0]
        return {
            "ok": True,
            "version": f"SQLite {version}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def list_tables(self, conn: dict[str, Any]) -> list[dict[str, Any]]:
        """列出 SQLite 用户表,统一补上 type / nullable / key 字段。"""
        tables = sqlite_db.list_tables(self._path(conn))
        return [
            {
                "name": table["name"],
                "type": "BASE TABLE",
                "columns": [
                    {
                        "name": column["name"],
                        "type": column["type"],
                        "nullable": "",
                        "key": "",
                    }
                    for column in table["columns"]
                ],
            }
            for table in tables
        ]

    def describe_table(self, conn: dict[str, Any], name: str) -> dict[str, Any]:
        """返回单表列结构;视图不在 list_tables 中,按不存在处理。"""
        for table in self.list_tables(conn):
            if table["name"] == name:
                return table
        raise ValueError(f"表或视图不存在: {name}")

    def table_stats(
        self, conn: dict[str, Any], tables: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """用 COUNT(*) 估算库内表规模,并汇总列元数据里的文本列。

        Args:
            conn: 运行时连接配置。
            tables: list_tables 的返回值,用于枚举表与识别文本列。

        Returns:
            {"rows": 最大表行数, "size_bytes": 整库文件体积(字节),
             "text_columns": [文本列名]}。
        """
        path = self._path(conn)
        max_rows = 0
        with closing(sqlite_db._connect_readonly(path)) as connection:
            for table in tables or []:
                name = str(table.get("name") or "")
                if not name:
                    continue
                # 表名来自 sqlite_master,仍按标准规则转义双引号
                quoted = '"' + name.replace('"', '""') + '"'
                try:
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {quoted}"
                    ).fetchone()[0]
                except sqlite3.Error:
                    # 视图 / 损坏表计数失败不影响体检,跳过即可
                    continue
                max_rows = max(max_rows, int(count or 0))
        try:
            size_bytes = os.path.getsize(path) if path else 0
        except OSError:
            size_bytes = 0
        return {
            "rows": max_rows,
            "size_bytes": size_bytes,
            "text_columns": _text_columns(tables),
        }

    def explain(self, conn: dict[str, Any], sql: str) -> str:
        """用 EXPLAIN QUERY PLAN 校验 SQL 引用的表 / 列是否存在。

        Args:
            conn: 运行时连接配置。
            sql: 待校验 SQL,只能是单条 SELECT / WITH。

        Returns:
            空串表示通过;否则返回错误文本。

        Raises:
            ValueError: SQL 未通过只读安全校验。
        """
        safe_sql = sqlite_db.validate_select(sql)
        with closing(sqlite_db._connect_readonly(self._path(conn))) as connection:
            try:
                connection.execute("EXPLAIN QUERY PLAN " + safe_sql).fetchall()
            except sqlite3.Error as exc:
                return str(exc)
        return ""

    def run_select(
        self,
        conn: dict[str, Any],
        sql: str,
        max_rows: int = sqlite_db.DEFAULT_MAX_ROWS,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """复用 db.run_select 执行只读查询,并共用磁盘结果缓存。

        Args:
            conn: 运行时连接配置。
            sql: 待执行 SQL,只能是单条 SELECT / WITH。
            max_rows: 最多返回行数。
            timeout_seconds: 本次查询超时秒数;默认使用 DB_QUERY_TIMEOUT_SECONDS,
                block 重写后的执行会传入更长的超时。
            cancel_event: 取消信号;置位后由 SQLite 进度回调中断查询。
        """
        safe_sql = sqlite_db.validate_select(sql, max_rows=max_rows)
        cache_id = db_cache.connection_cache_id(conn, self.kind)
        cached = db_cache.load(cache_id, sql)
        if cached is not None:
            return cached
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else sqlite_db.DEFAULT_QUERY_TIMEOUT
        )
        result = sqlite_db.run_select(
            self._path(conn),
            safe_sql,
            max_rows=max_rows,
            timeout=timeout,
            cancel_event=cancel_event,
        )
        frame = result["dataframe"]
        db_cache.save(cache_id, sql, frame)
        return {
            "dataframe": frame,
            "row_count": result["row_count"],
            "truncated": False,
            "cached": False,
        }


_ADAPTERS: dict[str, MysqlAdapter | SqliteAdapter] = {
    MYSQL_KIND: MysqlAdapter(),
    SQLITE_KIND: SqliteAdapter(),
}


def get_adapter(kind: str) -> MysqlAdapter | SqliteAdapter:
    """按连接类型取适配器实例。

    Args:
        kind: "mysql" 或 "sqlite"。

    Returns:
        对应的适配器单例。

    Raises:
        ValueError: 不支持的类型。
    """
    adapter = _ADAPTERS.get(str(kind or "").lower())
    if adapter is None:
        raise ValueError(f"不支持的数据库类型: {kind}")
    return adapter
