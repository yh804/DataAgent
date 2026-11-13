"""数据库业务术语表与待澄清记录。

存储:
- outputs/db_glossary.json:人工确认过的术语口径,按连接隔离;
- outputs/db_pending.jsonl:每个会话一轮待澄清问题(按 thread_id 覆盖)。

设计要点:
- 术语表只存「用户确认过的口径」,不做 LLM 自动落盘,避免把猜测固化成规则;
- 待澄清记录按 thread_id 覆盖写入,同一会话不会堆积多轮旧问题;
- 全部写操作走临时文件 + os.replace,防止进程被强杀留下半截 JSON。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
GLOSSARY_PATH = PROJECT_ROOT / "outputs" / "db_glossary.json"
PENDING_PATH = PROJECT_ROOT / "outputs" / "db_pending.jsonl"

SCHEMA_VERSION = 1

LOCK = threading.RLock()


def _now() -> str:
    """本地时区 ISO 时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    """空术语表结构。"""
    return {"version": SCHEMA_VERSION, "databases": {}}


def _read_all() -> dict[str, Any]:
    """读取术语表;文件不存在或损坏时返回空结构。"""
    if not GLOSSARY_PATH.is_file():
        return _empty()
    try:
        payload = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(payload, dict) or not isinstance(payload.get("databases"), dict):
        return _empty()
    payload["version"] = SCHEMA_VERSION
    return payload


def _write_json(path: Path, payload: Any) -> None:
    """原子写入 JSON 文件:先写临时文件,再 os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def load_glossary() -> dict[str, Any]:
    """读取全部术语表(深拷贝)。"""
    with LOCK:
        return json.loads(json.dumps(_read_all(), ensure_ascii=False))


def get_terms(connection_id: str) -> dict[str, dict[str, str]]:
    """取某连接下全部术语:{term: {definition, sql_hint, updated_at}}。"""
    with LOCK:
        database = _read_all()["databases"].get(str(connection_id)) or {}
        terms = database.get("terms") or {}
        return {
            str(term): {
                "definition": str(value.get("definition") or ""),
                "sql_hint": str(value.get("sql_hint") or ""),
                "updated_at": str(value.get("updated_at") or ""),
            }
            for term, value in terms.items()
            if isinstance(value, dict)
        }


def upsert_term(
    connection_id: str, term: str, definition: str, sql_hint: str = ""
) -> dict[str, str]:
    """新增 / 更新一条术语口径。

    Args:
        connection_id: 连接 id。
        term: 业务词,首尾空白会被去掉;空词直接返回空结构。
        definition: 用户确认的业务含义。
        sql_hint: 对应的 SQL 口径(如 表名.列名),可为空。

    Returns:
        {"term", "definition", "sql_hint", "updated_at"};空词时返回 {}。
    """
    clean_term = str(term or "").strip()
    if not clean_term:
        return {}
    record = {
        "definition": str(definition or "").strip(),
        "sql_hint": str(sql_hint or "").strip(),
        "updated_at": _now(),
    }
    with LOCK:
        payload = _read_all()
        database = payload["databases"].setdefault(
            str(connection_id), {"terms": {}}
        )
        database.setdefault("terms", {})[clean_term] = record
        database["updated_at"] = _now()
        _write_json(GLOSSARY_PATH, payload)
    return {"term": clean_term, **record}


def terms_for_prompt(connection_id: str) -> list[dict[str, str]]:
    """把术语表整理成提示词使用的列表,按术语排序保证输出稳定。"""
    terms = get_terms(connection_id)
    return [
        {
            "term": term,
            "definition": value.get("definition") or "",
            "sql_hint": value.get("sql_hint") or "",
        }
        for term, value in sorted(terms.items())
        if value.get("definition")
    ]


def _read_pending() -> list[dict[str, Any]]:
    """读取待澄清记录,坏行跳过。"""
    if not PENDING_PATH.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in PENDING_PATH.read_text(encoding="utf-8").splitlines():
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


def load_pending(thread_id: str) -> dict[str, Any] | None:
    """取该会话的待澄清记录;没有返回 None。"""
    with LOCK:
        for record in _read_pending():
            if record.get("thread_id") == thread_id:
                return record
    return None


def save_pending(record: dict[str, Any]) -> None:
    """写入待澄清记录:同 thread_id 的旧记录被覆盖。"""
    thread_id = str(record.get("thread_id") or "")
    if not thread_id:
        return
    with LOCK:
        records = [
            item for item in _read_pending() if item.get("thread_id") != thread_id
        ]
        records.append({**record, "created_at": _now()})
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = PENDING_PATH.with_name(f".{PENDING_PATH.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(temp_path, PENDING_PATH)


def clear_pending(thread_id: str) -> None:
    """删除该会话的待澄清记录。"""
    with LOCK:
        records = [
            item for item in _read_pending() if item.get("thread_id") != thread_id
        ]
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = PENDING_PATH.with_name(f".{PENDING_PATH.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(temp_path, PENDING_PATH)
