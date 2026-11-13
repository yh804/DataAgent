"""数据库表 / 列业务说明的持久化存储。

为什么单独一个文件:
- 说明由人工在数据库详情页维护,属于「业务口径」而非连接元数据,不能写进
  databases.jsonl(那份文件只放可审计的连接配置,且会被整表重写);
- SQL 生成时把说明注入提示词,让 LLM 依据人工口径而不是猜测列名含义。

存储格式(outputs/db_semantics.json):
    {"version": 1, "databases": {"<conn_id>": {"updated_at": "...", "tables": {
        "<table>": {"description": "...", "columns": {"<col>": "..."}}}}}}

约束:
- 写文件统一「临时文件 + os.replace」原子替换,避免进程被强杀留下半截 JSON;
- 表说明截断到 200 字、列说明截断到 100 字,防止把大段文档塞进提示词;
- 空字符串等于删除:表没有说明且没有任何列说明时,该表整条移除。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 路径常量 ────────────────────────────────────────────────
# 本模块位于项目根目录,所以 __file__.parent 就是项目根
# 注意:不要写成 parent.parent,那会跑到上一级目录去
PROJECT_ROOT = Path(__file__).resolve().parent
SEMANTICS_PATH = PROJECT_ROOT / "outputs" / "db_semantics.json"

# ── 常量 ────────────────────────────────────────────────────
SCHEMA_VERSION = 1          # 存储结构版本号,将来改格式可以做迁移
TABLE_DESC_MAX = 200        # 表说明最多 200 字(防止 prompt 爆炸)
COLUMN_DESC_MAX = 100       # 列说明最多 100 字

# 全局锁:所有读写都串行化,防止并发写造成 JSON 损坏
# 用 RLock(可重入锁)而不是 Lock,是因为内部函数可能嵌套调用
# (比如 upsert 里调 _read_all,以后加逻辑也不会死锁)
LOCK = threading.RLock()


def _now() -> str:
    """本地时区 ISO 时间,写入 updated_at。

    示例输出: "2026-11-10T14:30:00+08:00"
    astimezone() 会带上时区偏移,比裸 datetime.now() 更标准。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    """返回空存储结构(文件不存在时用这个兜底)。"""
    return {"version": SCHEMA_VERSION, "databases": {}}


def _read_all() -> dict[str, Any]:
    """读取整个存储文件;不存在或损坏时返回空结构。

    容错策略:
    - 文件不存在 → 返回空结构(第一次用)
    - JSON 解析失败(文件被写坏了) → 返回空结构,而不是崩掉
    - 结构不对(不是 dict / 没有 databases 字段) → 返回空结构
    """
    if not SEMANTICS_PATH.is_file():
        return _empty()
    try:
        payload = json.loads(SEMANTICS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # JSONDecodeError: 文件内容不是合法 JSON(比如写到一半崩了)
        # OSError:  读文件时出错(权限不足、文件被删了等)
        return _empty()
    if not isinstance(payload, dict) or not isinstance(payload.get("databases"), dict):
        return _empty()
    # 不管旧文件是什么版本,统一覆盖成当前版本号
    payload["version"] = SCHEMA_VERSION
    return payload


def _write_all(payload: dict[str, Any]) -> None:
    """原子写入存储文件:先写临时文件,再 os.replace 覆盖。

    为什么要原子写:
    - 直接 open(path, "w") 写入时,如果进程在写一半被杀,
      文件就变成半截 JSON,下次读就崩了
    - 先写到 .tmp 临时文件,写完再 replace 覆盖,
      要么全成功、要么旧文件不动,不会出现半成品

    os.replace 在 Windows 上也是原子的(底层 MoveFileEx + MOVEFILE_REPLACE_EXISTING)。
    """
    SEMANTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件名带 uuid,避免多个进程同时写时互相覆盖
    temp_path = SEMANTICS_PATH.with_name(f".{SEMANTICS_PATH.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # 临时文件写完了,一次性替换正式文件
    os.replace(temp_path, SEMANTICS_PATH)


def _clean_description(value: Any, limit: int) -> str:
    """把说明规范成去掉首尾空白的字符串,并截断到上限。

    做三件事:
    1. 转成字符串(None → "",数字 → "123")
    2. 去掉首尾空白(strip)
    3. 截断到 limit 个字

    为什么不直接 [:limit]?因为要先 strip,否则用户输入 "  abc  "
    截断后可能还留着空格。
    """
    return str(value or "").strip()[:limit]


def load_semantics() -> dict[str, Any]:
    """读取全部说明(深拷贝)。

    Returns:
        {"version": 1, "databases": {conn_id: {updated_at, tables}}}。

    为什么要深拷贝(json.loads(json.dumps(...)))?
    - 如果直接返回 _read_all() 的结果,调用方修改返回的 dict
      会影响内部状态(共享引用)
    - 深拷贝后,调用方随便改,不影响本模块
    """
    with LOCK:
        return json.loads(json.dumps(_read_all(), ensure_ascii=False))


def get_table_semantics(connection_id: str, table: str) -> dict[str, Any] | None:
    """取单表说明。

    Args:
        connection_id: 连接 id(对应 db_store 里的连接)。
        table: 表名或视图名。

    Returns:
        {"description": str, "columns": {col: desc}};没有记录返回 None。

    示例:
        get_table_semantics("abc12345", "orders")
        → {"description": "电商订单主表", "columns": {"gmv": "成交总额"}}
    """
    with LOCK:
        # 一层层 .get(or {}) 兜底,任何一层缺失都不会 KeyError
        database = _read_all()["databases"].get(str(connection_id)) or {}
        record = (database.get("tables") or {}).get(str(table))
        if not isinstance(record, dict):
            return None
        return {
            "description": str(record.get("description") or ""),
            "columns": {
                str(key): str(value)
                for key, value in (record.get("columns") or {}).items()
            },
        }


def upsert_table_semantics(
    connection_id: str,
    table: str,
    description: str = "",
    columns: dict[str, str] | None = None,
) -> dict[str, Any]:
    """新增 / 更新单表说明;空说明等于删除。

    Args:
        connection_id: 连接 id。
        table: 表名或视图名,调用方需先校验表存在。
        description: 表说明,超长截断到 200 字;空串表示清空。
        columns: {列名: 列说明},超长截断到 100 字;值为空串的列会被移除。

    Returns:
        保存后的 {"description", "columns"};若整条被删除则返回空结构。

    逻辑:
    1. 先清洗输入(截断、去空白、过滤空列)
    2. 读整个文件 → 在内存里改 → 整文件写回
    3. 如果清洗后表说明和列说明都空了 → 删掉这条表记录
    4. 如果这个库的表都删光了 → 删掉这个库的条目
    """
    # ── 第一步:清洗输入 ──
    clean_desc = _clean_description(description, TABLE_DESC_MAX)
    clean_columns: dict[str, str] = {}
    for name, value in (columns or {}).items():
        text = _clean_description(value, COLUMN_DESC_MAX)
        if text:  # 空说明的列直接跳过(等于删除该列的说明)
            clean_columns[str(name)] = text

    # ── 第二步:读改写,全程加锁 ──
    with LOCK:
        payload = _read_all()
        databases = payload["databases"]
        # setdefault: key 不存在就设成默认值,存在就返回原值
        # 这里确保 connection_id 和 tables 字典一定存在
        database = databases.setdefault(str(connection_id), {"tables": {}})
        tables = database.setdefault("tables", {})

        if clean_desc or clean_columns:
            # 有内容 → 覆盖写入
            tables[str(table)] = {"description": clean_desc, "columns": clean_columns}
            database["updated_at"] = _now()
        else:
            # 都空了 → 删除这条表记录
            tables.pop(str(table), None)
            if not tables:
                # 这个库的表都删光了 → 删掉这个库的条目
                databases.pop(str(connection_id), None)
            else:
                database["updated_at"] = _now()
        _write_all(payload)

    return {"description": clean_desc, "columns": clean_columns}


def delete_table_semantics(connection_id: str, table: str) -> bool:
    """删除单表说明;返回是否命中。

    返回 True:确实删了一条
    返回 False:id 不存在 / 表不存在(没删任何东西)
    """
    with LOCK:
        payload = _read_all()
        databases = payload["databases"]
        database = databases.get(str(connection_id))
        if not isinstance(database, dict):
            return False
        tables = database.get("tables") or {}
        if str(table) not in tables:
            return False
        tables.pop(str(table), None)
        if tables:
            # 还有其他表 → 更新时间戳
            database["updated_at"] = _now()
        else:
            # 这个库的表都删光了 → 删掉这个库的条目
            databases.pop(str(connection_id), None)
        _write_all(payload)
        return True


def merge_semantics(
    connection_id: str, tables: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把说明合并进 list_tables 的返回结构(不修改入参)。

    这是本模块最核心的函数:LLM 生成 SQL 前,会调这个函数
    把人工写的业务说明叠到真实表结构上,一起塞进 prompt。

    Args:
        connection_id: 连接 id。
        tables: adapter.list_tables 的返回值,表 dict 含 name / type / columns。

    Returns:
        新列表;有说明的表补 description,有说明的列补 description。
        没有任何说明时内容与入参逐字段一致。

    示例:
        入参 tables = [{"name": "orders", "columns": [{"name": "gmv"}]}]
        存储里有:orders.description = "电商订单主表", gmv.description = "成交总额"
        出参 = [{"name": "orders", "description": "电商订单主表",
                 "columns": [{"name": "gmv", "description": "成交总额"}]}]
    """
    with LOCK:
        database = _read_all()["databases"].get(str(connection_id)) or {}
        stored = database.get("tables") or {}

    merged: list[dict[str, Any]] = []
    for table in tables:
        # 取这张表的人工说明(没有就空 dict)
        record = stored.get(str(table.get("name"))) or {}
        columns: list[dict[str, Any]] = []
        column_notes = record.get("columns") or {}

        for column in table.get("columns") or []:
            # dict(column) 浅拷贝,不修改入参
            item = dict(column)
            note = column_notes.get(str(column.get("name")))
            if note:
                item["description"] = note
            columns.append(item)

        item = dict(table)
        item["columns"] = columns
        if record.get("description"):
            item["description"] = record["description"]
        merged.append(item)
    return merged
