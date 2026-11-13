"""把 MySQL 表从 MyISAM 迁移到 InnoDB,支持 dry-run 与可回滚的 --apply。

用途:
    bizdata.customer_purchase(MyISAM,约 49 万行 / 69MB)全表聚合超过 10 秒,
    迁移到 InnoDB 后可利用 buffer pool 与行级锁,重复查询显著变快。

用法:
    python scripts/migrate_to_innodb.py --db-id 2f79f5f5 --table customer_purchase
    python scripts/migrate_to_innodb.py --db-id 2f79f5f5 --table customer_purchase --apply

安全约束:
    1. 默认只打印计划,不加 --apply 不做任何变更;
    2. 备份密码通过 MYSQL_PWD 环境变量传入,不进入命令行与日志;
    3. 任一步失败立即停止,并在已产生备份时打印回滚命令;
    4. 已经是 InnoDB 时直接退出 0,不备份、不改表。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db_connections

try:  # 迁移脚本只在主进程运行,pymysql 缺失时给出人话错误
    import pymysql
except ImportError:  # pragma: no cover
    pymysql = None

MYSQLDUMP_PATH = Path(
    r"D:\download_res\phpstudy_pro\Extensions\MySQL8.0.12\bin\mysqldump.exe"
)
BACKUP_DIR = PROJECT_ROOT / "outputs" / "backups"
MIGRATIONS_PATH = PROJECT_ROOT / "outputs" / "migrations.jsonl"

SOURCE_ENGINE = "MyISAM"
TARGET_ENGINE = "InnoDB"
CONNECT_TIMEOUT_SECONDS = 5
METADATA_TIMEOUT_SECONDS = 30
DDL_TIMEOUT_SECONDS = 600  # 改引擎属于 DDL,不受 MAX_EXECUTION_TIME 约束

ESTIMATE_MIN_MBPS = 3.0  # 保守吞吐下限,用于估算迁移耗时
ESTIMATE_MAX_MBPS = 5.0  # 乐观吞吐上限
EXTRA_SPACE_FACTOR = 1.2  # 迁移期间新旧表共存,额外空间按表大小 1.2 倍估算
PERF_SUM_LIMIT = 11  # 11 个 SUM + 1 个 COUNT = 12 列,贴近真实聚合负载
VERIFY_SUM_COLUMNS = ("Age", "AnnualIncome")

_NUMERIC_DATA_TYPES = (
    "tinyint",
    "smallint",
    "mediumint",
    "int",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "float",
    "double",
    "real",
    "year",
)
_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z_$\u4e00-\u9fff]+$")


class MigrationError(RuntimeError):
    """迁移前置检查或执行失败,message 面向用户可直接展示。"""


def _mb(size_bytes: float) -> str:
    """把字节数格式化为 MB 文本。

    Args:
        size_bytes: 字节数。

    Returns:
        形如 "69.0 MB" 的文本。
    """
    return f"{float(size_bytes) / (1024 * 1024):.1f} MB"


def _quote_identifier(name: str) -> str:
    """校验并反引号包裹表名,防止 DDL 注入。

    Args:
        name: 表名(仅允许中英文、数字、下划线、$)。

    Returns:
        可安全拼进 SQL 的反引号标识符。

    Raises:
        MigrationError: 表名含非法字符。
    """
    if not _IDENTIFIER_RE.fullmatch(str(name or "")):
        raise MigrationError(f"表名含非法字符: {name!r}")
    return "`" + str(name) + "`"


def _mysql_error_text(exc: BaseException) -> str:
    """把驱动异常翻译成不含密码的中文提示。

    Args:
        exc: pymysql 抛出的原始异常。

    Returns:
        中文错误文本;已知错误码给出处理建议。
    """
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code == 1045:
        return "MySQL 用户名或密码错误(1045),请检查账号与密码"
    if code == 2003:
        return "无法连接到 MySQL 服务器(2003),请检查主机、端口与网络"
    if code == 1049:
        return "数据库不存在(1049),请检查库名"
    if code == 1146:
        return "表不存在(1146),请检查库名与表名"
    return f"MySQL 操作失败({code}): {exc}" if code else f"MySQL 操作失败: {exc}"


class _Client:
    """迁移专用 MySQL 短连接:自动提交、可配置读写超时。"""

    def __init__(self, config: dict[str, Any], *, timeout: float) -> None:
        """建立连接。

        Args:
            config: 含 host / port / user / password / database / ssl 的连接配置。
            timeout: socket 读写超时秒数(元数据 30,DDL 600)。

        Raises:
            MigrationError: 驱动缺失或连接失败。
        """
        if pymysql is None:
            raise MigrationError("未安装 pymysql,无法连接 MySQL;请执行 pip install pymysql==1.1.1")
        ssl_option = {"check_hostname": False} if config.get("ssl") else None
        try:
            self._connection = pymysql.connect(
                host=str(config.get("host") or ""),
                port=int(config.get("port") or 3306),
                user=str(config.get("user") or ""),
                password=str(config.get("password") or ""),
                database=str(config.get("database") or ""),
                charset="utf8mb4",
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=timeout,
                write_timeout=timeout,
                autocommit=True,
                ssl=ssl_option,
            )
        except Exception as exc:  # 统一翻译成中文提示,保留原始异常链
            raise MigrationError(f"连接 MySQL 失败: {_mysql_error_text(exc)}") from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False

    def fetch_one(self, sql: str, params: tuple | None = None) -> tuple | None:
        """执行查询并返回首行;无结果返回 None。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()
        except Exception as exc:
            raise MigrationError(_mysql_error_text(exc)) from exc

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[tuple]:
        """执行查询并返回全部行。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except Exception as exc:
            raise MigrationError(_mysql_error_text(exc)) from exc

    def execute(self, sql: str) -> None:
        """执行不取结果的语句(DDL / ANALYZE)。"""
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql)
        except Exception as exc:
            raise MigrationError(_mysql_error_text(exc)) from exc

    def close(self) -> None:
        """关闭连接;忽略关闭阶段的异常。"""
        with suppress(Exception):
            self._connection.close()


def _open_client(config: dict[str, Any], *, timeout: float) -> _Client:
    """创建迁移连接(测试可替换此工厂)。

    Args:
        config: 连接配置(含密码)。
        timeout: socket 读写超时秒数。

    Returns:
        _Client 实例。
    """
    return _Client(config, timeout=timeout)


def _load_config(db_id: str) -> dict[str, Any]:
    """从 databases.jsonl 与凭据文件组装连接配置。

    Args:
        db_id: 连接 id。

    Returns:
        含 password 的运行时配置。

    Raises:
        MigrationError: 连接不存在、不是 MySQL 或密码读取失败。
    """
    record = db_connections.get_connection(db_id)
    if record is None:
        raise MigrationError(f"连接不存在: {db_id}")
    if str(record.get("kind") or "").lower() != "mysql":
        raise MigrationError(f"连接 {db_id} 不是 MySQL 类型,无法迁移存储引擎")
    return {**record, "password": db_connections.get_password(db_id)}


def _read_table_info(client: _Client, database: str, table: str) -> dict[str, Any]:
    """读取表的引擎与体积元数据。

    Args:
        client: 迁移连接。
        database: 库名。
        table: 表名。

    Returns:
        {"engine", "table_rows", "data_length", "index_length"}

    Raises:
        MigrationError: 表不存在。
    """
    row = client.fetch_one(
        "SELECT ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
        "FROM information_schema.tables WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (database, table),
    )
    if row is None:
        raise MigrationError(f"表不存在: {database}.{table}")
    engine, table_rows, data_length, index_length = row
    if not engine:
        raise MigrationError(f"对象不是表(可能是视图): {database}.{table}")
    return {
        "engine": str(engine),
        "table_rows": int(table_rows or 0),
        "data_length": int(data_length or 0),
        "index_length": int(index_length or 0),
    }


def _count_rows(client: _Client, quoted_table: str) -> int:
    """统计表精确行数。

    Args:
        client: 迁移连接。
        quoted_table: 已反引号包裹的表名。

    Returns:
        精确行数。
    """
    row = client.fetch_one(f"SELECT COUNT(*) FROM {quoted_table}")
    return int(row[0]) if row else 0


def _numeric_columns(client: _Client, database: str, table: str) -> list[str]:
    """按定义顺序取数值列,用于 SUM 校验与性能对比。

    Args:
        client: 迁移连接。
        database: 库名。
        table: 表名。

    Returns:
        数值列名列表(可能为空)。
    """
    placeholders = ", ".join(["%s"] * len(_NUMERIC_DATA_TYPES))
    rows = client.fetch_all(
        "SELECT COLUMN_NAME FROM information_schema.columns "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        f"AND DATA_TYPE IN ({placeholders}) ORDER BY ORDINAL_POSITION",
        (database, table, *_NUMERIC_DATA_TYPES),
    )
    return [str(row[0]) for row in rows]


def _sum_columns(columns: list[str]) -> list[str]:
    """选用于校验的 SUM 列:优先 Age / AnnualIncome。

    Args:
        columns: 表的数值列。

    Returns:
        最多 2 个列名;都缺失时退化为前两个数值列。
    """
    preferred = [name for name in VERIFY_SUM_COLUMNS if name in columns]
    return preferred or columns[:2]


def _sum_values(client: _Client, quoted_table: str, columns: list[str]) -> list[float | None]:
    """计算指定列的 SUM(四舍五入 2 位)。

    Args:
        client: 迁移连接。
        quoted_table: 已反引号包裹的表名。
        columns: 待求和的数值列。

    Returns:
        与 columns 顺序一致的 SUM 列表;空表对应 None。
    """
    if not columns:
        return []
    selected = ", ".join(f"ROUND(SUM(`{name}`), 2)" for name in columns)
    row = client.fetch_one(f"SELECT {selected} FROM {quoted_table}")
    if row is None:
        return [None] * len(columns)
    return [None if value is None else float(Decimal(str(value))) for value in row]


def _aggregate_sql(quoted_table: str, columns: list[str]) -> str:
    """构造 12 列聚合语句(COUNT + 最多 11 个 SUM)用于计时。

    Args:
        quoted_table: 已反引号包裹的表名。
        columns: 数值列。

    Returns:
        单条只读聚合 SQL。
    """
    selected = ["COUNT(*) AS `总行数`"]
    selected.extend(
        f"ROUND(SUM(`{name}`), 2) AS `sum_{index}`"
        for index, name in enumerate(columns[:PERF_SUM_LIMIT], start=1)
    )
    return f"SELECT {', '.join(selected)} FROM {quoted_table}"


def _timed_query(client: _Client, sql: str) -> float:
    """执行查询并返回墙钟耗时秒数。

    Args:
        client: 迁移连接。
        sql: 待执行聚合 SQL。

    Returns:
        耗时秒数。
    """
    started = time.perf_counter()
    client.fetch_all(sql)
    return time.perf_counter() - started


def _disk_free_bytes(client: _Client) -> tuple[int | None, str]:
    """读取 MySQL datadir 所在磁盘的剩余空间。

    Args:
        client: 迁移连接。

    Returns:
        (剩余字节数或 None, datadir 文本);读取失败时字节数为 None。
    """
    try:
        row = client.fetch_one("SELECT @@datadir")
        datadir = str(row[0]) if row else ""
    except MigrationError:
        return None, ""
    candidate = Path(datadir)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free, datadir
    except OSError:
        return None, datadir


def _fulltext_indexes(client: _Client, database: str, table: str) -> list[str]:
    """查询表的 FULLTEXT 索引名。

    Args:
        client: 迁移连接。
        database: 库名。
        table: 表名。

    Returns:
        索引名列表;无 FULLTEXT 返回 []。
    """
    rows = client.fetch_all(
        "SELECT DISTINCT INDEX_NAME FROM information_schema.statistics "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_TYPE = 'FULLTEXT'",
        (database, table),
    )
    return [str(row[0]) for row in rows]


def _build_plan(client: _Client, config: dict[str, Any], table: str) -> dict[str, Any]:
    """汇总 dry-run 计划所需的全部信息。

    Args:
        client: 迁移连接。
        config: 连接配置。
        table: 表名。

    Returns:
        含引擎、行数、体积、磁盘空间、耗时估算的计划字典。
    """
    database = str(config.get("database") or "")
    quoted = _quote_identifier(table)
    info = _read_table_info(client, database, table)
    rows = _count_rows(client, quoted)
    size_bytes = info["data_length"] + info["index_length"]
    size_mb = size_bytes / (1024 * 1024)
    free_bytes, datadir = _disk_free_bytes(client)
    return {
        **info,
        "rows": rows,
        "size_bytes": size_bytes,
        "size_mb": size_mb,
        "required_bytes": int(size_bytes * EXTRA_SPACE_FACTOR),
        "free_bytes": free_bytes,
        "datadir": datadir,
        "fulltext": _fulltext_indexes(client, database, table),
        "estimate_min_s": size_mb / ESTIMATE_MAX_MBPS,
        "estimate_max_s": size_mb / ESTIMATE_MIN_MBPS,
    }


def _print_plan(database: str, table: str, plan: dict[str, Any]) -> None:
    """打印 dry-run / apply 前的迁移计划。

    Args:
        database: 库名。
        table: 表名。
        plan: _build_plan 的结果。
    """
    data_mb = plan["data_length"] / (1024 * 1024)
    index_mb = plan["index_length"] / (1024 * 1024)
    print(f"表: {database}.{table}")
    print(f"当前引擎: {plan['engine']}")
    print(f"精确行数: {plan['rows']}")
    print(
        f"表大小: {_mb(plan['size_bytes'])}"
        f"(数据 {data_mb:.1f} MB + 索引 {index_mb:.1f} MB)"
    )
    datadir = plan["datadir"] or "未知"
    free = "未知" if plan["free_bytes"] is None else _mb(plan["free_bytes"])
    print(f"datadir: {datadir}(所在盘剩余 {free})")
    print(
        f"预计耗时: {plan['estimate_min_s']:.1f} ~ {plan['estimate_max_s']:.1f} 秒"
        f"(按 {ESTIMATE_MIN_MBPS:g}~{ESTIMATE_MAX_MBPS:g} MB/s 估算)"
    )
    print(
        f"额外空间需求: {_mb(plan['required_bytes'])}"
        f"(表大小 × {EXTRA_SPACE_FACTOR:g})"
    )
    if plan["fulltext"]:
        print(
            f"FULLTEXT 索引: {', '.join(plan['fulltext'])}"
            "(InnoDB 下需重建 FULLTEXT 索引,迁移会相应变慢)"
        )
    else:
        print("FULLTEXT 索引: 无")


def _backup_path(backup_dir: Path, table: str) -> Path:
    """生成带时间戳的备份文件路径。

    Args:
        backup_dir: 备份目录。
        table: 表名。

    Returns:
        outputs/backups/{table}_{YYYYmmdd_HHMMSS}.sql
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return backup_dir / f"{table}_{timestamp}.sql"


def _dump_command(config: dict[str, Any], table: str) -> list[str]:
    """组装 mysqldump 参数(密码只走 MYSQL_PWD,不进命令行)。

    Args:
        config: 连接配置。
        table: 表名。

    Returns:
        传给 subprocess 的参数列表。
    """
    return [
        str(MYSQLDUMP_PATH),
        "-h",
        str(config.get("host") or ""),
        "-P",
        str(config.get("port") or 3306),
        "-u",
        str(config.get("user") or ""),
        "--databases",
        str(config.get("database") or ""),
        "--tables",
        table,
        "--lock-tables",
        "--default-character-set=utf8mb4",
    ]


def _run_mysqldump(config: dict[str, Any], table: str, backup_dir: Path) -> Path:
    """调用 mysqldump 备份单表,并校验备份文件非空。

    Args:
        config: 连接配置(密码通过 MYSQL_PWD 传入)。
        table: 表名。
        backup_dir: 备份目录。

    Returns:
        备份文件路径。

    Raises:
        MigrationError: mysqldump 不存在、退出码非零或备份为空。
    """
    if not MYSQLDUMP_PATH.is_file():
        raise MigrationError(f"找不到 mysqldump: {MYSQLDUMP_PATH}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = _backup_path(backup_dir, table)
    environment = {**os.environ, "MYSQL_PWD": str(config.get("password") or "")}
    try:
        with path.open("wb") as handle:
            completed = subprocess.run(
                _dump_command(config, table),
                stdout=handle,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise MigrationError(f"启动 mysqldump 失败: {exc}") from exc

    if completed.returncode != 0:
        path.unlink(missing_ok=True)
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise MigrationError(f"mysqldump 备份失败(退出码 {completed.returncode}): {detail}")
    if not path.is_file() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise MigrationError("备份文件为空,已停止迁移")
    return path


def _rollback_command(config: dict[str, Any], backup: Path) -> str:
    """生成恢复备份的 mysql 命令(密码交互输入,不落明文)。

    Args:
        config: 连接配置。
        backup: 备份文件路径。

    Returns:
        可直接复制执行的命令文本。
    """
    return (
        f"mysql -h {config.get('host') or ''} -P {config.get('port') or 3306} "
        f"-u {config.get('user') or ''} -p {config.get('database') or ''} < {backup}"
    )


def _append_audit(record: dict[str, Any]) -> None:
    """追加一行迁移审计。

    Args:
        record: 含 ts/table/from_engine/to_engine/rows/size_mb/backup/duration_s/verify_ok。
    """
    MIGRATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MIGRATIONS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _apply(
    config: dict[str, Any],
    table: str,
    plan: dict[str, Any],
    backup_dir: Path,
) -> int:
    """执行备份、迁移、校验、性能对比与审计。

    Args:
        config: 连接配置。
        table: 表名。
        plan: _build_plan 的结果。
        backup_dir: 备份目录。

    Returns:
        0 = 成功;1 = 执行失败;2 = 校验失败需要恢复。
    """
    started = time.perf_counter()
    database = str(config.get("database") or "")
    quoted = _quote_identifier(table)
    backup: Path | None = None

    try:
        if plan["free_bytes"] is not None and plan["free_bytes"] < plan["required_bytes"]:
            raise MigrationError(
                f"磁盘剩余空间不足: 需要约 {_mb(plan['required_bytes'])},"
                f"实际剩余 {_mb(plan['free_bytes'])}"
            )

        with _open_client(config, timeout=METADATA_TIMEOUT_SECONDS) as client:
            columns = _numeric_columns(client, database, table)
            sum_columns = _sum_columns(columns)
            sums_before = _sum_values(client, quoted, sum_columns)
        aggregate_sql = _aggregate_sql(quoted, columns)

        print(f"[1/6] 备份 {database}.{table} ...")
        backup = _run_mysqldump(config, table, backup_dir)
        print(f"      备份完成: {backup}")

        print(f"[2/6] 迁移引擎 {plan['engine']} -> {TARGET_ENGINE} (ALGORITHM=COPY) ...")
        with _open_client(config, timeout=DDL_TIMEOUT_SECONDS) as client:
            client.execute(f"ALTER TABLE {quoted} ENGINE={TARGET_ENGINE}, ALGORITHM=COPY")

        print(f"[3/6] ANALYZE TABLE {quoted} ...")
        with _open_client(config, timeout=DDL_TIMEOUT_SECONDS) as client:
            client.execute(f"ANALYZE TABLE {quoted}")

        print("[4/6] 校验引擎、行数与 SUM ...")
        with _open_client(config, timeout=METADATA_TIMEOUT_SECONDS) as client:
            engine_after = _read_table_info(client, database, table)["engine"]
            rows_after = _count_rows(client, quoted)
            sums_after = _sum_values(client, quoted, sum_columns)

        verify_ok = (
            engine_after.upper() == TARGET_ENGINE.upper()
            and rows_after == plan["rows"]
            and sums_after == sums_before
        )
        if not verify_ok:
            print("校验失败，请用备份恢复")
            print(f"实际引擎: {engine_after}(期望 {TARGET_ENGINE})")
            print(f"行数: {rows_after}(迁移前 {plan['rows']})")
            print(f"SUM: {sums_after}(迁移前 {sums_before})")
            print(f"回滚命令: {_rollback_command(config, backup)}")
            return 2

        print("[5/6] 性能对比(同一条 12 列聚合跑两次) ...")
        with _open_client(config, timeout=DDL_TIMEOUT_SECONDS) as client:
            first = _timed_query(client, aggregate_sql)
            second = _timed_query(client, aggregate_sql)
        print(f"      第一次: {first:.2f}s,第二次: {second:.2f}s")

        duration = time.perf_counter() - started
        print("[6/6] 写迁移审计 ...")
        _append_audit(
            {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "table": f"{database}.{table}",
                "from_engine": plan["engine"],
                "to_engine": TARGET_ENGINE,
                "rows": plan["rows"],
                "size_mb": round(plan["size_mb"], 1),
                "backup": str(backup),
                "duration_s": round(duration, 1),
                "verify_ok": True,
            }
        )
        print(
            f"迁移完成: {plan['engine']} -> {TARGET_ENGINE},"
            f"行数 {plan['rows']},用时 {duration:.1f}s"
        )
        return 0
    except MigrationError as exc:
        print(f"错误: {exc}")
        if backup is not None:
            print(f"迁移已中断,回滚命令: {_rollback_command(config, backup)}")
        return 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表;None 时取 sys.argv[1:]。

    Returns:
        含 db_id / table / apply / backup_dir 的命名空间。
    """
    parser = argparse.ArgumentParser(description="把 MySQL 表从 MyISAM 迁移到 InnoDB")
    parser.add_argument("--db-id", required=True, help="outputs/databases.jsonl 中的连接 id")
    parser.add_argument("--table", required=True, help="表名")
    parser.add_argument("--apply", action="store_true", help="真正执行;不加则只打印计划")
    parser.add_argument(
        "--backup-dir",
        default=str(BACKUP_DIR),
        help="备份目录,默认 outputs/backups",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """脚本入口。

    Args:
        argv: 参数列表;None 时取 sys.argv[1:]。

    Returns:
        0 = 成功或无需迁移;1 = 执行失败;2 = 校验失败。
    """
    args = _parse_args(argv)
    backup_dir = Path(args.backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = PROJECT_ROOT / backup_dir

    try:
        config = _load_config(args.db_id)
        database = str(config.get("database") or "")
        with _open_client(config, timeout=METADATA_TIMEOUT_SECONDS) as client:
            info = _read_table_info(client, database, args.table)
            if info["engine"].upper() != SOURCE_ENGINE.upper():
                print(f"当前引擎: {info['engine']}")
                print(f"无需迁移: 目标表不是 {SOURCE_ENGINE},未做任何变更")
                return 0
            plan = _build_plan(client, config, args.table)
    except MigrationError as exc:
        print(f"错误: {exc}")
        return 1

    _print_plan(database, args.table, plan)
    if not args.apply:
        print("(dry-run) 未执行任何变更;确认无误后加 --apply")
        return 0
    return _apply(config, args.table, plan, backup_dir)


if __name__ == "__main__":
    sys.exit(main())
