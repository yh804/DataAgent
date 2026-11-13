"""数据库连接管理端点:CRUD、连接测试、表/视图浏览。

安全约束：
- 浏览器只提交连接参数,不提交 SQL、不接受连接串;
- 响应只回 password_set,从不回密码;
- 所有真实建连都串行执行(DB_LOCK),避免瞬时打满数据库;
- 连接/查询异常统一翻成人话并以 400 返回,不让驱动异常冒成 500。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import db_connections as connections
import db_semantics
from db_adapter import AdapterError, get_adapter

router = APIRouter()

# 真实建连串行化:同一时刻只处理一个连接测试/元数据/查询请求
DB_LOCK = threading.Lock()

# 归档目录与删除审计:在本模块内独立定位,避免 import web.app 造成循环导入
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = PROJECT_ROOT / "outputs" / "runs"
DELETIONS_PATH = PROJECT_ROOT / "outputs" / "deletions.jsonl"
_DATA_DIR_DEFAULT = "data/processed"

# run_id / 结果 CSV 文件名白名单:拼路径前必须校验,防止路径穿越
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_PREVIEW_LIMIT_MAX = 100


class DatabaseIn(BaseModel):
    """新建/更新连接的请求体;password 只用于建连,不返回、不落元数据文件。"""

    label: str
    kind: str = "mysql"
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: str | None = None
    path: str | None = None
    ssl: bool = False


class TableSemanticsIn(BaseModel):
    """单表业务说明请求体;空串表示清空该字段。"""

    description: str = ""
    columns: dict[str, str] = {}


class DatabaseTestIn(DatabaseIn):
    """测试连接请求体;id 存在时表示测试已保存连接(密码/字段可留空沿用)。"""

    id: str | None = None
    label: str = ""
    kind: str = ""
    ssl: bool | None = None


def _locked(func, *args, **kwargs):
    """在 DB_LOCK 内执行阻塞操作。"""
    with DB_LOCK:
        return func(*args, **kwargs)


def _require_connection(connection_id: str) -> dict[str, Any]:
    """取连接公开信息;id 非法 400、不存在 404。"""
    if not connections.is_valid_id(connection_id):
        raise HTTPException(status_code=400, detail="连接 id 格式非法")
    record = connections.get_connection(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"连接不存在: {connection_id}")
    return record


def _runtime_config(record: dict[str, Any]) -> dict[str, Any]:
    """拼出仅存在于主进程内存的运行时配置(含密码)。"""
    return {**record, "password": connections.get_password(record["id"])}


def _friendly_error(exc: BaseException) -> str:
    """把适配器异常转成可展示文案,避免把 traceback 暴露给前端。"""
    if isinstance(exc, (AdapterError, ValueError, FileNotFoundError, TimeoutError)):
        return str(exc)
    return f"数据库操作失败: {exc}"


def _test_runtime(payload: DatabaseTestIn) -> dict[str, Any]:
    """把测试请求合并成运行时配置:已保存连接可沿用保存的密码。"""
    saved = connections.get_connection(payload.id) if payload.id else None
    data = payload.model_dump(exclude_none=True)
    # 空字符串视为「未填写」,避免覆盖已保存连接的主机/库名/标签
    data = {key: value for key, value in data.items() if value != ""}
    runtime = {**(saved or {}), **data}
    password = payload.password
    if not password and payload.id:
        password = connections.get_password(payload.id)
    runtime["password"] = password or ""
    return runtime


@router.get("/api/databases")
async def list_databases():
    """列出全部连接(仅元数据 + password_set)。"""
    return connections.list_connections()


@router.post("/api/databases")
async def create_database(payload: DatabaseIn):
    """新建连接:元数据与密码分文件存储。"""
    try:
        record = connections.create_connection(payload.model_dump(), payload.password or "")
    except connections.DuplicateConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record


@router.put("/api/databases/{connection_id}")
async def update_database(connection_id: str, payload: DatabaseIn):
    """更新连接;password 留空表示不修改已保存的密码。"""
    _require_connection(connection_id)
    try:
        record = connections.update_connection(
            connection_id,
            payload.model_dump(),
            password=payload.password or None,
        )
    except connections.ConnectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except connections.DuplicateConnectionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record


@router.post("/api/databases/test")
async def test_database(payload: DatabaseTestIn):
    """测试连接:返回服务器版本与耗时;已保存连接会更新 last_status。"""
    runtime = _test_runtime(payload)
    if str(runtime.get("kind") or "mysql") == "mysql":
        if not runtime.get("host") or not runtime.get("user") or not runtime.get("database"):
            raise HTTPException(status_code=400, detail="主机、用户名、库名都必须填写")

    try:
        adapter = get_adapter(str(runtime.get("kind") or "mysql"))
        result = await run_in_threadpool(_locked, adapter.test, runtime)
    except Exception as exc:  # noqa: BLE001 - 连接失败必须转 400,不能 500
        if payload.id and connections.is_valid_id(payload.id):
            connections.mark_status(payload.id, "error")
        raise HTTPException(status_code=400, detail=_friendly_error(exc)) from exc

    if payload.id and connections.is_valid_id(payload.id):
        connections.mark_status(payload.id, "ok")
    return {**result, "label": runtime.get("label", "")}


@router.delete("/api/databases/{connection_id}")
async def delete_database(connection_id: str):
    """删除连接与密码;不存在返回 404。"""
    if not connections.is_valid_id(connection_id):
        raise HTTPException(status_code=400, detail="连接 id 格式非法")
    if not connections.delete_connection(connection_id):
        raise HTTPException(status_code=404, detail=f"连接不存在: {connection_id}")
    return {"ok": True}


@router.get("/api/db/queries")
async def list_db_queries(limit: int = 50, db_id: str | None = None):
    """数据库问答历史:按时间倒序返回,最多 limit 条(不含任何密码)。"""
    return await run_in_threadpool(connections.read_queries, limit, db_id)


@router.delete("/api/db/queries")
async def clear_db_queries():
    """清空数据库查询审计:只清查询历史,不删已生成的运行记录。"""
    removed = await run_in_threadpool(connections.clear_queries)
    try:
        DELETIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DELETIONS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "db_queries",
                        "removed": removed,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        # 审计写失败不影响清理结果,前端只需要知道删了多少
        pass
    return {"deleted": removed}


def _preview_source(run_id: str, csv_name: str) -> Path | None:
    """定位结果 CSV:优先运行归档的 source.csv,其次数据目录下的同名文件。

    Args:
        run_id: 运行 ID;为空或不合法时跳过归档路径。
        csv_name: 结果文件名;必须是纯文件名(不能含路径分隔符)。

    Returns:
        存在的 CSV 路径;两处都不存在时返回 None。
    """
    candidates: list[Path] = []
    if run_id and _RUN_ID_RE.match(run_id):
        candidates.append(RUNS_ROOT / run_id / "source.csv")
    if csv_name and Path(csv_name).name == csv_name and csv_name.lower().endswith(".csv"):
        candidates.append(Path(os.getenv("SANDBOX_DATA_DIR") or _DATA_DIR_DEFAULT) / csv_name)
    return next((path for path in candidates if path.is_file()), None)


@router.get("/api/db/queries/preview")
async def preview_db_query(run_id: str = "", csv_name: str = "", limit: int = 20):
    """返回数据库查询结果的前 N 行,供历史页展开详情时展示。"""
    safe_limit = max(1, min(int(limit or 20), _PREVIEW_LIMIT_MAX))
    target = _preview_source(run_id, csv_name)
    if target is None:
        return {"available": False, "columns": [], "rows": [], "source": ""}
    try:
        frame = pd.read_csv(target, nrows=safe_limit)
    except Exception as exc:  # noqa: BLE001 - 文件损坏 / 编码异常都转 400
        raise HTTPException(status_code=400, detail=f"结果表读取失败: {exc}") from exc
    # to_json 统一处理 NaN / numpy 类型,保证返回值是合法 JSON
    rows = json.loads(frame.to_json(orient="records", force_ascii=False))
    return {
        "available": True,
        "columns": [str(column) for column in frame.columns],
        "rows": rows,
        "source": target.name,
    }


@router.get("/api/databases/{connection_id}/tables")
async def list_tables(connection_id: str):
    """浏览连接下的表与视图(含列元数据)。"""
    record = _require_connection(connection_id)
    runtime = _runtime_config(record)
    try:
        adapter = get_adapter(record["kind"])
        tables = await run_in_threadpool(_locked, adapter.list_tables, runtime)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_friendly_error(exc)) from exc
    return {
        "id": connection_id,
        "label": record["label"],
        "kind": record["kind"],
        "tables": tables,
    }


@router.get("/api/databases/{connection_id}/tables/{name}")
async def describe_table(connection_id: str, name: str):
    """查看单表/视图的字段:名称、类型、是否可空、主键标记。"""
    record = _require_connection(connection_id)
    runtime = _runtime_config(record)
    try:
        adapter = get_adapter(record["kind"])
        table = await run_in_threadpool(_locked, adapter.describe_table, runtime, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_friendly_error(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_friendly_error(exc)) from exc
    return {"id": connection_id, "label": record["label"], "table": table}


@router.get("/api/databases/{connection_id}/semantics")
async def get_semantics(connection_id: str):
    """读取该连接下所有表 / 列的业务说明。"""
    _require_connection(connection_id)
    payload = await run_in_threadpool(db_semantics.load_semantics)
    database = (payload.get("databases") or {}).get(connection_id) or {}
    return {
        "id": connection_id,
        "updated_at": database.get("updated_at") or "",
        "tables": database.get("tables") or {},
    }


@router.put("/api/databases/{connection_id}/semantics/{table}")
async def put_semantics(connection_id: str, table: str, payload: TableSemanticsIn):
    """保存单表说明:表必须存在,列名必须属于该表。

    非法列一并以 400 返回并列出,便于前端直接提示哪些列写错了。
    """
    record = _require_connection(connection_id)
    runtime = _runtime_config(record)
    adapter = get_adapter(record["kind"])
    try:
        described = await run_in_threadpool(_locked, adapter.describe_table, runtime, table)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_friendly_error(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_friendly_error(exc)) from exc

    valid_columns = {str(item.get("name")) for item in described.get("columns") or []}
    illegal = sorted(
        {str(name) for name in payload.columns if str(name) not in valid_columns}
    )
    if illegal:
        raise HTTPException(
            status_code=400,
            detail=f"列不存在: {'、'.join(illegal)}",
        )

    saved = await run_in_threadpool(
        db_semantics.upsert_table_semantics,
        connection_id,
        table,
        payload.description,
        payload.columns,
    )
    return {"id": connection_id, "table": table, **saved}


@router.delete("/api/databases/{connection_id}/semantics/{table}")
async def delete_semantics(connection_id: str, table: str):
    """删除单表说明;连接不存在 404,表说明本就不存在时返回 deleted=false。"""
    _require_connection(connection_id)
    deleted = await run_in_threadpool(
        db_semantics.delete_table_semantics, connection_id, table
    )
    return {"id": connection_id, "table": table, "deleted": bool(deleted)}
