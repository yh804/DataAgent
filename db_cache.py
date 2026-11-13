"""MySQL / SQLite 共用的只读查询结果磁盘缓存。

缓存只保存适配器已经限制过行数的结果,CSV 与元数据放在 outputs/db_cache/。
任何缓存读取或写入失败都按未命中处理,不影响数据库正常查询。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_ROOT / "outputs" / "db_cache"
MAX_CACHE_BYTES = 50 * 1024 * 1024  # 单个查询的 CSV 与 JSON 合计上限 50MB
DEFAULT_CACHE_TTL_SECONDS = 600.0

_CACHEABLE_RE = re.compile(r"^(select|show|describe|explain)\b", re.IGNORECASE)
_SAFE_DB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_sql(sql: str) -> str:
    """归一化 SQL 空白,使仅排版不同的查询共用缓存。

    Args:
        sql: 原始 SQL 文本。

    Returns:
        去首尾空白并把连续空白压缩为一个空格的 SQL。
    """
    return re.sub(r"\s+", " ", str(sql).strip())


def is_cacheable(sql: str) -> bool:
    """判断语句是否属于允许缓存的只读查询类型。

    Args:
        sql: 原始 SQL 文本。

    Returns:
        仅 SELECT/SHOW/DESCRIBE/EXPLAIN 返回 True,其他语句返回 False。
    """
    return bool(_CACHEABLE_RE.match(normalize_sql(sql)))


def cache_key(db_id: str, sql: str) -> str:
    """计算连接与归一化 SQL 组成的缓存键。

    Args:
        db_id: 数据库连接标识。
        sql: 原始 SQL 文本。

    Returns:
        sha1 十六进制摘要。
    """
    payload = f"{db_id}\n{normalize_sql(sql)}".encode()
    return hashlib.sha1(payload).hexdigest()


def cache_enabled() -> bool:
    """读取缓存总开关。

    Returns:
        DB_CACHE=0 时返回 False,其他情况默认返回 True。
    """
    return os.getenv("DB_CACHE", "1").strip().lower() not in {"0", "false", "off", "no"}


def _cache_ttl_seconds() -> float:
    """读取缓存有效期,非法或负值回退默认值。

    Returns:
        缓存 TTL 秒数(非负数,默认 600)。
    """
    try:
        value = float(os.getenv("DB_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_TTL_SECONDS
    if not math.isfinite(value) or value < 0:
        return DEFAULT_CACHE_TTL_SECONDS
    return value


def connection_cache_id(conn: dict[str, Any], kind: str) -> str:
    """取稳定连接标识;未保存的连接用非敏感连接信息派生标识。

    Args:
        conn: 运行时连接配置,优先使用其中的 id,不读取密码。
        kind: 数据库类型(mysql 或 sqlite)。

    Returns:
        原连接 id,或不含凭据的派生标识。
    """
    explicit_id = str(conn.get("id") or "").strip()
    if explicit_id:
        return explicit_id
    if kind == "sqlite":
        identity = str(Path(str(conn.get("path") or conn.get("database") or "")).resolve())
    else:
        identity = "\n".join(
            [
                str(conn.get("host") or ""),
                str(conn.get("port") or ""),
                str(conn.get("database") or ""),
                str(conn.get("user") or ""),
            ]
        )
    digest = hashlib.sha1(f"{kind}\n{identity}".encode()).hexdigest()[:16]
    return f"{kind}-{digest}"


def _paths(db_id: str, sql: str) -> tuple[Path, Path]:
    """返回缓存 CSV 与元数据路径,非法目录字符用哈希替代。

    Args:
        db_id: 数据库连接标识。
        sql: 原始 SQL 文本。

    Returns:
        (CSV 路径, JSON 元数据路径)。
    """
    raw_id = str(db_id)
    safe_id = raw_id if _SAFE_DB_ID_RE.fullmatch(raw_id) else hashlib.sha1(
        raw_id.encode()
    ).hexdigest()
    key = cache_key(raw_id, sql)
    directory = CACHE_DIR / safe_id
    return directory / f"{key}.csv", directory / f"{key}.json"


def load(db_id: str, sql: str) -> dict[str, Any] | None:
    """读取未过期的查询缓存。

    Args:
        db_id: 数据库连接标识。
        sql: 原始 SQL 文本,写语句不会读取缓存。

    Returns:
        含 dataframe/row_count/truncated/cached=True 的结果;未命中返回 None。
    """
    if not cache_enabled() or not is_cacheable(sql):
        return None
    csv_path, metadata_path = _paths(db_id, sql)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(str(metadata["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        ttl = _cache_ttl_seconds()
        age = (datetime.now(UTC) - created_at).total_seconds()
        if ttl <= 0 or age > ttl:
            return None
        frame = pd.read_csv(csv_path, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return None
    return {
        "dataframe": frame,
        "row_count": len(frame),
        "truncated": bool(metadata.get("truncated", False)),
        "cached": True,
    }


def save(
    db_id: str,
    sql: str,
    frame: pd.DataFrame,
    truncated: bool = False,
) -> bool:
    """将只读查询结果写入 CSV 与 JSON,超过大小上限则不落盘。

    Args:
        db_id: 数据库连接标识。
        sql: 原始 SQL 文本,写语句不会写入缓存。
        frame: 已限制行数的查询结果。
        truncated: 数据库结果是否因 max_rows 被截断(默认 False)。

    Returns:
        成功落盘返回 True;关闭缓存、语句不可缓存、超限或 IO 失败返回 False。
    """
    if not cache_enabled() or not is_cacheable(sql):
        return False
    created_at = datetime.now(UTC).isoformat()
    metadata = {
        "sql": str(sql),
        "row_count": len(frame),
        "created_at": created_at,
        "truncated": bool(truncated),
    }
    csv_bytes = frame.to_csv(index=False).encode()
    metadata_bytes = json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode()
    if len(csv_bytes) + len(metadata_bytes) > MAX_CACHE_BYTES:
        return False

    csv_path, metadata_path = _paths(db_id, sql)
    token = uuid.uuid4().hex
    csv_temp = csv_path.with_name(f".{csv_path.name}.{token}.tmp")
    metadata_temp = metadata_path.with_name(f".{metadata_path.name}.{token}.tmp")
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_temp.write_bytes(csv_bytes)
        metadata_temp.write_bytes(metadata_bytes)
        os.replace(csv_temp, csv_path)
        os.replace(metadata_temp, metadata_path)
        return True
    except OSError:
        for path in (csv_temp, metadata_temp):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False
