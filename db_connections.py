"""数据库连接的元数据与凭据存储。

存储拆分：
- outputs/databases.jsonl:连接元数据,可读、可审计,绝不包含密码;
- outputs/db_credentials.json:仅存 id -> password,权限 600,接口只回 password_set;
- outputs/db_queries.jsonl:数据库问答审计(问题、SQL、行数、成功与否),不含密码。

所有读写串行化,避免并发追加造成半行 JSON。
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# 本模块位于项目根目录:父目录就是项目根,不能再用 parent.parent
PROJECT_ROOT = Path(__file__).resolve().parent
CONNECTIONS_PATH = PROJECT_ROOT / "outputs" / "databases.jsonl"
CREDENTIALS_PATH = PROJECT_ROOT / "outputs" / "db_credentials.json"
QUERIES_PATH = PROJECT_ROOT / "outputs" / "db_queries.jsonl"

# 所有连接/凭据文件的读改写都走这把锁;路由层也用它串行化真实建连
LOCK = threading.RLock()

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,32}$")

_VALID_KINDS = ("mysql", "sqlite")
# 查询审计白名单:只允许这些字段落盘,密码 / 连接串永远不可能被写进去
_AUDIT_FIELDS = (
    "query_id",
    "db_id",
    "db_label",
    "question",
    "sql",
    "ok",
    "status",
    "row_count",
    "elapsed_ms",
    "error",
    "csv_name",
    "run_id",
    "cached",
    "warnings",
    "db_deleted",
)

# 审计文件只保留最近 500 条,防止长期使用后无限增长
_MAX_QUERY_RECORDS = 500

# 数据库结果 CSV 的命名:`db_<label>_<YYYYMMDD_HHMMSS>[_<毫秒>].csv`
_DB_DATASET_RE = re.compile(
    r"^db_(?P<label>.+)_(?P<ts>\d{8}_\d{6})(?:_\d{3})?\.csv$", re.IGNORECASE
)


class ConnectionNotFoundError(ValueError):
    """连接 id 不存在。"""


class DuplicateConnectionError(ValueError):
    """连接标签重复。"""


def is_valid_id(value: str) -> bool:
    """判断连接 id 是否符合正则(防止路径/注入类输入)。"""
    return bool(ID_PATTERN.match(str(value or "")))


def _now() -> str:
    """本地时区 ISO 时间,写入 last_checked_at / created_at。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL,坏行直接跳过(容错),返回字典列表。"""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """整文件重写 JSONL,先写临时文件再原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temp_path, path)


def _read_credentials() -> dict[str, str]:
    """读取凭据文件;不存在或损坏时返回空字典。"""
    if not CREDENTIALS_PATH.is_file():
        return {}
    try:
        payload = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}


def _write_credentials(credentials: dict[str, str]) -> None:
    """原子写入凭据文件,并尽量收紧到 600 权限。"""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CREDENTIALS_PATH.with_name(f".{CREDENTIALS_PATH.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(credentials, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, CREDENTIALS_PATH)
    try:
        os.chmod(CREDENTIALS_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows 上 chmod 语义有限,写权限位已由 ACL 控制;失败不阻断主流程
        pass


def _public(record: dict[str, Any], password_set: bool) -> dict[str, Any]:
    """返回可给前端的元数据副本,只暴露 password_set。"""
    public = {key: value for key, value in record.items() if key != "password"}
    public["password_set"] = bool(password_set)
    return public


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化创建/更新连接时提交的字段。

    Args:
        payload: 前端提交的原始字典(可能包含 password,本函数忽略它)。

    Returns:
        规范化后的可持久化字段(不含 id/密码/时间戳)。

    Raises:
        ValueError: 必填字段缺失或取值非法。
    """
    kind = str(payload.get("kind") or "mysql").lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"不支持的数据库类型: {kind}")

    label = str(payload.get("label") or "").strip()
    if not label or len(label) > 64:
        raise ValueError("连接名称必填,且长度不超过 64 个字符")

    if kind == "mysql":
        host = str(payload.get("host") or "").strip()
        user = str(payload.get("user") or "").strip()
        database = str(payload.get("database") or "").strip()
        if not host:
            raise ValueError("主机不能为空")
        if not user:
            raise ValueError("用户名不能为空")
        if not database:
            raise ValueError("库名不能为空")
        try:
            port = int(payload.get("port") or 3306)
        except (TypeError, ValueError) as exc:
            raise ValueError("端口必须是 1-65535 的整数") from exc
        if not 1 <= port <= 65535:
            raise ValueError("端口必须是 1-65535 的整数")
        return {
            "label": label,
            "kind": kind,
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "ssl": bool(payload.get("ssl")),
        }

    path = str(payload.get("path") or payload.get("database") or "").strip()
    if not path:
        raise ValueError("SQLite 文件路径不能为空")
    return {"label": label, "kind": kind, "path": path}


def list_connections() -> list[dict[str, Any]]:
    """列出全部连接(不含密码),新创建的在前。"""
    with LOCK:
        credentials = _read_credentials()
        records = _read_jsonl(CONNECTIONS_PATH)
        return [
            _public(record, record.get("id") in credentials)
            for record in reversed(records)
        ]


def get_connection(connection_id: str) -> dict[str, Any] | None:
    """按 id 取连接公开信息;不存在返回 None。"""
    if not is_valid_id(connection_id):
        return None
    with LOCK:
        credentials = _read_credentials()
        for record in _read_jsonl(CONNECTIONS_PATH):
            if record.get("id") == connection_id:
                return _public(record, connection_id in credentials)
    return None


def get_password(connection_id: str) -> str:
    """取连接密码;不存在返回空字符串。仅用于主进程建连。"""
    with LOCK:
        return _read_credentials().get(connection_id, "")


def create_connection(payload: dict[str, Any], password: str) -> dict[str, Any]:
    """新建连接:元数据写 jsonl,密码单独写凭据文件。

    Args:
        payload: 前端字段(label/kind/host/port/database/user/path/ssl)。
        password: MySQL 密码;不会写入 databases.jsonl。

    Returns:
        公开的连接信息(含 password_set)。

    Raises:
        ValueError: 字段校验失败。
        DuplicateConnectionError: 同名连接已存在。
    """
    fields = _validate_payload(payload)
    with LOCK:
        records = _read_jsonl(CONNECTIONS_PATH)
        if any(item.get("label") == fields["label"] for item in records):
            raise DuplicateConnectionError(f"连接名称已存在: {fields['label']}")

        existing_ids = {item.get("id") for item in records}
        connection_id = uuid.uuid4().hex[:8]
        while connection_id in existing_ids:
            connection_id = uuid.uuid4().hex[:8]

        record = {
            "id": connection_id,
            **fields,
            "created_at": _now(),
            "last_status": "unknown",
            "last_checked_at": "",
        }
        records.append(record)
        _write_jsonl(CONNECTIONS_PATH, records)

        if fields["kind"] == "mysql":
            credentials = _read_credentials()
            credentials[connection_id] = str(password or "")
            _write_credentials(credentials)

        return _public(record, bool(password))


def update_connection(
    connection_id: str,
    payload: dict[str, Any],
    password: str | None = None,
) -> dict[str, Any]:
    """更新连接元数据;password 为空表示保留原密码。

    Raises:
        ConnectionNotFoundError: id 不存在。
        DuplicateConnectionError: 改名后与其他连接重名。
    """
    if not is_valid_id(connection_id):
        raise ConnectionNotFoundError(f"连接不存在: {connection_id}")

    fields = _validate_payload(payload)
    with LOCK:
        records = _read_jsonl(CONNECTIONS_PATH)
        target_index = next(
            (index for index, item in enumerate(records) if item.get("id") == connection_id),
            None,
        )
        if target_index is None:
            raise ConnectionNotFoundError(f"连接不存在: {connection_id}")
        if any(
            item.get("label") == fields["label"] and item.get("id") != connection_id
            for item in records
        ):
            raise DuplicateConnectionError(f"连接名称已存在: {fields['label']}")

        target = records[target_index]
        records[target_index] = {
            **target,
            **fields,
            "last_status": target.get("last_status", "unknown"),
            "last_checked_at": target.get("last_checked_at", ""),
        }
        _write_jsonl(CONNECTIONS_PATH, records)

        credentials = _read_credentials()
        if password:
            credentials[connection_id] = str(password)
            _write_credentials(credentials)
        elif fields["kind"] == "sqlite":
            credentials.pop(connection_id, None)
            _write_credentials(credentials)

        return _public(records[target_index], connection_id in credentials)


def delete_connection(connection_id: str) -> bool:
    """删除连接与对应密码;返回是否命中。"""
    if not is_valid_id(connection_id):
        return False
    with LOCK:
        records = _read_jsonl(CONNECTIONS_PATH)
        remaining = [item for item in records if item.get("id") != connection_id]
        if len(remaining) == len(records):
            return False
        _write_jsonl(CONNECTIONS_PATH, remaining)

        credentials = _read_credentials()
        if connection_id in credentials:
            credentials.pop(connection_id)
            _write_credentials(credentials)
        # 查询历史不随连接删除一起丢失:只在该连接的历史行上标记 db_deleted
        _mark_queries_deleted(connection_id)
        return True


def mark_status(connection_id: str, status: str) -> dict[str, Any] | None:
    """记录最近一次连接测试结果(ok / error)。"""
    if not is_valid_id(connection_id):
        return None
    with LOCK:
        records = _read_jsonl(CONNECTIONS_PATH)
        for index, record in enumerate(records):
            if record.get("id") == connection_id:
                records[index] = {
                    **record,
                    "last_status": status,
                    "last_checked_at": _now(),
                }
                _write_jsonl(CONNECTIONS_PATH, records)
                credentials = _read_credentials()
                return _public(records[index], connection_id in credentials)
    return None


def record_query(record: dict[str, Any]) -> str:
    """追加一条数据库问答记录并裁剪到最近 500 条。

    Args:
        record: 问答字段(db_id/db_label/question/sql/ok/status/row_count/
            elapsed_ms/error/csv_name/run_id/cached/warnings);白名单之外的字段一律丢弃,
            密码与连接串因此永远不会落盘。

    Returns:
        本条记录的 query_id;后续补写 run_id 时用它定位。
    """
    with LOCK:
        query_id = str(record.get("query_id") or uuid.uuid4().hex[:12])
        payload: dict[str, Any] = {"ts": _now(), "query_id": query_id}
        payload.update({key: record.get(key) for key in _AUDIT_FIELDS})
        payload["query_id"] = query_id
        records = _read_jsonl(QUERIES_PATH)
        records.append(payload)
        # 写入时裁剪并原子重写:旧行被截掉,但不会出现半行 / 并发撕裂
        _write_jsonl(QUERIES_PATH, records[-_MAX_QUERY_RECORDS:])
        return query_id


# 向后兼容:旧调用方(web/app.py 与既有测试)仍按 append_query_audit 调用
append_query_audit = record_query


def attach_run_id(query_id: str, run_id: str) -> bool:
    """把 run_id 补写进指定审计记录(同一个 run 只补一次)。

    Args:
        query_id: record_query 返回的记录 id。
        run_id: 本轮分析归档的运行 ID。

    Returns:
        True 表示本次写入成功;记录不存在、已有 run_id 或入参为空时返回 False。
    """
    if not query_id or not run_id:
        return False
    with LOCK:
        records = _read_jsonl(QUERIES_PATH)
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if str(record.get("query_id") or "") != str(query_id):
                continue
            # 已补写过(无论是否同一个 run)都不再改写,保证只补一次
            if record.get("run_id"):
                return False
            records[index] = {**record, "run_id": str(run_id)}
            _write_jsonl(QUERIES_PATH, records)
            return True
    return False


def read_queries(limit: int = 50, db_id: str | None = None) -> list[dict[str, Any]]:
    """读取数据库问答历史,按时间倒序返回。

    Args:
        limit: 最多返回条数(1~500,非法值回退 50)。
        db_id: 可选,只返回该连接的记录。

    Returns:
        字段白名单过滤后的记录列表;旧行缺 status 时按 ok 推导,缺 db_label 时补空串。
        密码不在白名单内,不会出现在返回值里。
    """
    try:
        safe_limit = int(limit)
    except (TypeError, ValueError):
        safe_limit = 50
    safe_limit = max(1, min(safe_limit, _MAX_QUERY_RECORDS))
    with LOCK:
        records = _read_jsonl(QUERIES_PATH)
    if db_id:
        records = [
            record for record in records if str(record.get("db_id") or "") == str(db_id)
        ]
    records.sort(key=lambda record: str(record.get("ts") or ""), reverse=True)
    result: list[dict[str, Any]] = []
    for record in records[:safe_limit]:
        item = {key: record.get(key) for key in _AUDIT_FIELDS if key in record}
        # 旧行没有 status:按 ok 推导,保证前端状态徽章永远有值
        if not item.get("status"):
            item["status"] = "ok" if record.get("ok") else "failed"
        item.setdefault("db_label", "")
        result.append(item)
    return result


def get_query(query_id: str) -> dict[str, Any] | None:
    """按 query_id 取单条审计记录;不存在返回 None。"""
    target = str(query_id or "")
    if not target:
        return None
    with LOCK:
        for record in reversed(_read_jsonl(QUERIES_PATH)):
            if str(record.get("query_id") or "") == target:
                return {key: record.get(key) for key in _AUDIT_FIELDS if key in record}
    return None


def find_query_by_csv(csv_name: str) -> dict[str, Any] | None:
    """按结果 CSV 文件名回查最近一条审计记录(供运行归档补充 db_query)。"""
    target = str(csv_name or "")
    if not target:
        return None
    with LOCK:
        for record in reversed(_read_jsonl(QUERIES_PATH)):
            if str(record.get("csv_name") or "") == target:
                return {key: record.get(key) for key in _AUDIT_FIELDS if key in record}
    return None


def clear_queries() -> int:
    """清空查询历史审计文件,返回删除的条数(不触碰任何运行记录)。"""
    with LOCK:
        removed = len(_read_jsonl(QUERIES_PATH))
        _write_jsonl(QUERIES_PATH, [])
        return removed


def parse_db_dataset(name: str) -> dict[str, str] | None:
    """解析数据库结果 CSV 文件名 db_<label>_<YYYYMMDD_HHMMSS>.csv。

    Args:
        name: 数据集文件名,例如 db_MySQL_20261113_101500.csv。

    Returns:
        {"label": 从文件名还原的库标签, "ts": 时间戳片段};不匹配时返回 None。
    """
    match = _DB_DATASET_RE.match(str(name or ""))
    if not match:
        return None
    return {"label": match.group("label"), "ts": match.group("ts")}


def _mark_queries_deleted(connection_id: str) -> None:
    """把某连接的历史查询标记为 db_deleted(删除连接时保留可回溯性)。"""
    with LOCK:
        records = _read_jsonl(QUERIES_PATH)
        changed = False
        for index, record in enumerate(records):
            if str(record.get("db_id") or "") != connection_id:
                continue
            if record.get("db_deleted"):
                continue
            records[index] = {**record, "db_deleted": True}
            changed = True
        # 没有任何命中时不重写文件,避免无意义的 IO
        if changed:
            _write_jsonl(QUERIES_PATH, records)
