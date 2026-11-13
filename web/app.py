"""FastAPI Web 层：单页应用的后端(批次 D)。

硬约束(与 README 一致)：
    - 单进程单 worker 运行(web/run.py 写死 workers=1):图与 SQLite 检查点是
      进程级单例,多 worker 会导致检查点与 index.jsonl 竞争;
    - 所有图执行串行:全局 asyncio.Semaphore(1),第二个请求排队并收到 queued 事件;
    - 图执行一律 run_in_threadpool,绝不阻塞事件循环;
    - 只绑 127.0.0.1,不做鉴权(本地单用户工具);
    - 所有路径拼接先 resolve 再校验父目录,禁止路径穿越。

只做加法：不改 run_agent / resume_agent 的返回结构,不改 eval 用例语义。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd

# web/ 是项目根的子目录:把根目录加入 sys.path 才能 import main / tools 等
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# main 是 Agent 核心;这里只做薄封装,不复制业务逻辑  # noqa: E402
import main as agent_main
from main import (  # noqa: E402  (供测试 monkeypatch,必须在本模块命名空间可见)
    get_pending_interrupt,
    json_safe,
    load_pending_interrupt,
    resume_agent,
    run_agent,
)

import checkpoint as checkpoint_mod  # noqa: E402

from fastapi import FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from tools import describe_dataset  # noqa: E402
import ask_db  # noqa: E402
import db_connections  # noqa: E402
import db_glossary  # noqa: E402
import db_guard  # noqa: E402
import db_semantics  # noqa: E402
from db import DEFAULT_MAX_ROWS  # noqa: E402
from db_adapter import AdapterError, QueryCancelled, get_adapter  # noqa: E402
from llm import GlossaryCheck, get_llm, mask_secrets  # noqa: E402
from prompts import GLOSSARY_CHECK_PROMPT, build_glossary_user_prompt  # noqa: E402
from web import approval_policy  # noqa: E402
from web.events import EventBridge, sse_stream  # noqa: E402
from web.routers.databases import DB_LOCK as DB_CONNECT_LOCK  # noqa: E402

logger = logging.getLogger("web.app")


class _SecretFilter(logging.Filter):
    """日志出口脱敏:任何日志文本里的 sk- 密钥都替换成 sk-***。

    BYOK 模式下密钥来自请求头,可能被第三方库写进异常文本;
    在 root logger 上挂一个过滤器,保证业务日志不会把密钥带进 stdout / 日志文件。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """就地改写日志记录,永远返回 True(只脱敏,不过滤日志)。"""
        record.msg = mask_secrets(record.getMessage())
        record.args = ()
        return True


# 只挂一次:模块被重复导入 / 测试隔离时避免同一过滤器叠加
if not any(isinstance(f, _SecretFilter) for f in logging.getLogger().filters):
    logging.getLogger().addFilter(_SecretFilter())


# ---- 路径与常量(全部 resolve 到项目根,避免依赖启动时的 cwd) ----
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSIONS_PATH = OUTPUTS_DIR / "sessions.jsonl"
APPROVALS_PATH = OUTPUTS_DIR / "approvals.jsonl"  # 审批审计:每次决策(策略/人工)一行
DELETIONS_PATH = OUTPUTS_DIR / "deletions.jsonl"  # 删除审计:会话/单次运行都只留 id 与计数
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 上传上限 20MB
# 上传支持的格式:统一在服务端转成 UTF-8 CSV 落盘,下游分析链路无需改动
UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xls", ".json"}
TRACE_SCAN_LIMIT = 50  # trace 回读最多扫最新 50 个文件,防止目录膨胀后变慢

# block 重写后的执行超时:仅用于「体检判定 block 且已自动重写一次」的那次查询
_DB_GUARD_TIMEOUT_DEFAULT = 120  # 秒,来源于 .env.example 的 DB_GUARD_TIMEOUT_SECONDS
_DB_GUARD_TIMEOUT_MAX = 600


def _guard_timeout_from_env() -> float:
    """读取 block 重写后的执行超时秒数,非法或越界值回退到默认 120。

    Returns:
        执行超时秒数(范围 1~600,默认 120)。
    """
    try:
        value = float(
            os.getenv("DB_GUARD_TIMEOUT_SECONDS", str(_DB_GUARD_TIMEOUT_DEFAULT))
        )
    except (TypeError, ValueError):
        return float(_DB_GUARD_TIMEOUT_DEFAULT)
    if not 1 <= value <= _DB_GUARD_TIMEOUT_MAX:
        return float(_DB_GUARD_TIMEOUT_DEFAULT)
    return value


DB_GUARD_TIMEOUT_SECONDS = _guard_timeout_from_env()

# Web 侧单条查询的硬超时:超过即取消并返回超时错误(默认 120 秒)
_DB_QUERY_TIMEOUT_DEFAULT = 120
_DB_QUERY_TIMEOUT_MAX = 600


def _db_query_timeout_from_env() -> float:
    """读取 Web 侧数据库查询硬超时秒数,非法或越界值回退到默认 120。

    Returns:
        查询超时秒数(范围 1~600,默认 120)。
    """
    try:
        value = float(
            os.getenv("DB_QUERY_TIMEOUT_SECONDS", str(_DB_QUERY_TIMEOUT_DEFAULT))
        )
    except (TypeError, ValueError):
        return float(_DB_QUERY_TIMEOUT_DEFAULT)
    if not 1 <= value <= _DB_QUERY_TIMEOUT_MAX:
        return float(_DB_QUERY_TIMEOUT_DEFAULT)
    return value


DB_QUERY_TIMEOUT_SECONDS = _db_query_timeout_from_env()

# run_id 合法形态:时间戳_短哈希(见 main._new_run_id),白名单防路径穿越
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_\-]+$")

# 会话删除:thread_id 与 run_id 的白名单(比 _RUN_ID_RE 更严,防止路径穿越)
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_DELETE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
TRACE_DELETE_SCAN_LIMIT = 200  # 删除时最多扫 200 个 trace 文件

# 上传文件名白名单:保留中文 / 字母 / 数字 / . _ -,其余一律替换
_FILENAME_BAD_RE = re.compile(r"[^\w一-鿿.\-]+")

# 全局执行串行锁:任何图执行都必须先拿到它,保证「同一时刻只有一个分析在跑」
_RUN_LOCK = asyncio.Semaphore(1)

# 正在执行的 thread_id 集合:供 GET /api/sessions/{id} 报告 running 状态
_RUNNING: set[str] = set()

# 数据库问答的附加字段(sql / sql_row_count / db_label):审批轮次恢复后仍能带回结果卡
_DB_CONTEXT: dict[str, dict] = {}

# 本轮数据库问答审计记录 id:run 成功后据此把 run_id 补写回审计行(每轮一个)
_DB_QUERY_IDS: dict[str, str] = {}

# CSV 文件名里的标签只保留中英文、数字、下划线与连字符,避免路径问题
_DB_LABEL_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")

app = FastAPI(title="数据分析 Agent", docs_url=None, redoc_url=None)


# ---------- 请求模型 ----------

class QuestionIn(BaseModel):
    question: str
    dataset_name: str | None = None  # 输入框附件选择的数据集:仅用于锁定 planner 的数据源
    database_id: str | None = None  # 选中的数据库连接:由服务端生成 SQL,浏览器不传 SQL


class ApprovalIn(BaseModel):
    action: str
    plan: list[str] | None = None
    trust: bool = False  # True 时批准的同时把该数据集加入本会话信任列表


class TrustIn(BaseModel):
    dataset: str
    trusted: bool


# ---------- 通用小工具 ----------

def _data_dir() -> Path:
    """当前数据目录(每次调用读环境变量,与 tools.py 口径一致)。"""
    return Path(os.getenv("SANDBOX_DATA_DIR") or "data/processed")


def _session_exists(thread_id: str) -> bool:
    """该 thread_id 是否已有检查点(决定 run_agent 用续跑还是全新初始化)。

    参数:
        thread_id: 会话标识,由前端生成并随每条消息回传。

    返回:
        True = 检查点里有非空状态,按续跑处理;False = 全新会话。

    为什么用 values 判空,而不是「检查点记录是否存在」:
        检查点里可能只有占位记录(例如 thread 刚创建就中断),只有非空 values
        才代表真的产出了状态;这样能避免把空壳会话当成可续跑的历史。
    """
    try:
        # get_state 是 LangGraph 的只读快照接口:只读当前状态,不推进图、不触发节点
        snapshot = agent_main._get_graph().get_state(
            {"configurable": {"thread_id": thread_id}}
        )
    except Exception:  # noqa: BLE001 - 检查点读取失败按「不存在」处理,走全新会话
        # SQLite 被占用 / 库文件损坏等异常不该让整个提问失败,降级为全新会话更安全
        return False
    # values 为空说明没有可续跑的历史状态,按新会话处理
    return bool(getattr(snapshot, "values", None) or {})


def _read_session_records() -> list[dict]:
    """读 sessions.jsonl 全部记录,返回原始字典列表(不做去重与排序)。

    返回:
        按写入顺序排列的记录;文件不存在、IO 失败或全为坏行时返回 []。

    为什么逐行容错而不是整体解析:
        sessions.jsonl 是追加式索引,进程被强杀时末行可能只写了一半;
        坏行只跳过并记日志,不能让一条残行导致整张会话列表不可用。
    """
    if not SESSIONS_PATH.is_file():
        # 新克隆 / 尚未产生任何会话时文件还不存在,视为「没有会话」而不是异常
        return []
    records: list[dict] = []
    try:
        for line in SESSIONS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                # 文件末尾换行或手工编辑留下的空行,跳过即可
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # 只截取前 80 字符记日志:避免半行内容过长污染日志
                logger.warning("sessions.jsonl 跳过坏行: %s", line[:80])
    except OSError as exc:
        # 权限不足 / 文件被占用等 IO 问题降级为「读不到」,调用方按空会话处理
        logger.warning("读 sessions.jsonl 失败: %s", exc)
    return records


def _load_trusted(thread_id: str) -> list[str]:
    """该会话的信任数据集列表:取 sessions.jsonl 中该 thread 最后一条记录的
    trusted_datasets;没有记录或字段缺失返回 []。"""
    trusted: list[str] = []
    for record in _read_session_records():
        if record.get("thread_id") != thread_id:
            continue
        value = record.get("trusted_datasets")
        if isinstance(value, list):
            trusted = [str(x) for x in value]
    return trusted


def _session_last_run_id(thread_id: str) -> str:
    """读取该会话上一轮分析的运行 ID(取 sessions.jsonl 中该 thread 最后一条)。

    Args:
        thread_id: 会话标识。

    Returns:
        上一轮 run_id;没有记录或字段为空时返回 ""。

    为什么单独抽函数:planner 需要用它判断「生成网页 / 导出报告」能否复用归档,
    而 sessions.jsonl 是按 thread 追加的,必须取最后一条才是最新状态。
    """
    last = ""
    for record in _read_session_records():
        if record.get("thread_id") == thread_id:
            last = record.get("last_run_id") or ""
    return str(last)


def _record_session(thread_id: str, question: str, run_id: str) -> None:
    """每轮结束追加一条会话索引;同一 thread_id 只保留最新一条(读取时去重)。

    追加式 jsonl 而不是整表重写:多轮对话只加一行,写失败只记日志不影响主流程。
    信任列表随记录携带:保证刷新 / 跨轮后信任关系不丢。

    run_id 为空(如交付 / 澄清轮没有执行分析)时沿用上一轮的 last_run_id,
    否则「先分析、再说生成网页」的交付轮会把指向报告的引用清空。
    """
    try:
        SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not run_id:
            # 本轮没有产生新运行:保留上一轮的归档引用,供后续交付意图继续使用
            run_id = _session_last_run_id(thread_id)
        record = {
            "thread_id": thread_id,
            # 标题来自用户问题,若误贴了 key 也不能落盘
            "title": mask_secrets(question or "")[:30],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_run_id": run_id or "",
            "trusted_datasets": _load_trusted(thread_id),
        }
        with SESSIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(mask_secrets(json.dumps(record, ensure_ascii=False)) + "\n")
    except OSError as exc:
        logger.warning("写 sessions.jsonl 失败: %s", mask_secrets(exc))


def _set_trusted(thread_id: str, dataset: str, is_trusted: bool) -> None:
    """更新本会话对某数据集的信任状态:追加一条带最新信任列表的记录。

    保留该 thread 已有记录的标题与 last_run_id;读取侧取最后一条即最新信任状态。
    """
    dataset = (dataset or "").strip()
    if not dataset:
        return
    latest: dict = {}
    for record in _read_session_records():
        if record.get("thread_id") == thread_id:
            latest = record
    trusted = list(dict.fromkeys(_load_trusted(thread_id)))  # 保序去重
    if is_trusted and dataset not in trusted:
        trusted.append(dataset)
    if not is_trusted and dataset in trusted:
        trusted.remove(dataset)
    try:
        SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "thread_id": thread_id,
            "title": mask_secrets(latest.get("title") or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_run_id": latest.get("last_run_id") or "",
            "trusted_datasets": trusted,
        }
        with SESSIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(mask_secrets(json.dumps(record, ensure_ascii=False)) + "\n")
    except OSError as exc:
        logger.warning("写 sessions.jsonl 失败: %s", mask_secrets(exc))


def _audit_approval(
    thread_id: str,
    dataset: str,
    question: str,
    level: str,
    reasons: list[str],
    decided_by: str,
    action: str,
) -> None:
    """审批审计:每次决策(策略自动/策略转人工/人工动作)追加一行。

    只记录「为什么」需要的字段,不写计划全文;写失败只记日志,不阻断主流程。
    """
    try:
        APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "thread_id": thread_id,
            "dataset": dataset or "",
            "question": mask_secrets(question or ""),
            "level": level,
            "reasons": list(reasons or []),
            "decided_by": decided_by,
            "action": action,
        }
        with APPROVALS_PATH.open("a", encoding="utf-8") as f:
            f.write(mask_secrets(json.dumps(record, ensure_ascii=False)) + "\n")
    except OSError as exc:
        logger.warning("写 approvals.jsonl 失败: %s", mask_secrets(exc))


def _strip_dataset_prefix(question: str, dataset: str) -> str:
    """去掉 post_message 拼给 Agent 的「分析 <数据集>：」前缀,还原用户原始问题。

    前缀只用于让 planner 锁定数据源;会话标题与审批审计必须保留用户原话,
    因此在读回检查点里的 pending.question 时统一剥掉。

    Args:
        question: 可能被 post_message 拼过前缀的问题文本(也可能就是用户原话)。
        dataset: 前端附件锁定的数据集名(空字符串表示没有附件)。

    Returns:
        去掉前缀后的用户原始问题;如果本来就没有前缀 或 dataset 为空,
        原样返回 question(不做任何改动)。
    """
    # dataset 为空时根本不可能有这种前缀,直接返回,避免构造字符串
    prefix = f"分析 {dataset}：" if dataset else ""
    # 只有当前缀存在 且 question 真的以它开头时才剥离
    if prefix and question.startswith(prefix):
        # 切片跳过前缀本身,再 strip() 去掉可能残留的空白
        return question[len(prefix):].strip()
    # 不匹配的场景(无前缀 或 用户原话里恰好包含同名数据集但没走附件逻辑)不动
    return question


def _dataset_meta(name: str) -> dict:
    """describe_dataset 的结果(供策略判定与前端列名自检);异常返回 {}。

    策略对异常回退 confirm,因此这里绝不向上抛。
    """
    if not name:
        return {}
    try:
        result = describe_dataset.invoke({"name": name})
        return result if isinstance(result, dict) else {}
    except Exception:  # noqa: BLE001 - 元数据缺失只是少几个判定依据,不该搞挂主流程
        logger.warning("读取数据集元信息失败: %s", name)
        return {}


def _meta_columns(meta: dict) -> list[str]:
    """从 describe_dataset 结果里提取列名列表(前端计划自检用)。"""
    columns = meta.get("columns") or []
    return [str(c.get("name") or "") for c in columns if isinstance(c, dict)]


def _session_dataset(thread_id: str) -> str:
    """该会话检查点里已锁定的数据集名;读不到(新会话/异常)返回 ""。"""
    try:
        snapshot = agent_main._get_graph().get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        values = getattr(snapshot, "values", None) or {}
        return str(values.get("dataset_name") or "")
    except Exception:  # noqa: BLE001
        return ""


def _question_datasets(question: str) -> list[str]:
    """问题中点名了数据目录里的哪些 csv(按文件名或 stem 匹配)。

    用途:pre-run 预判时确认「问题里没点名其他未信任数据集」,
    避免某数据集被信任后,一句跨表问题把未信任数据集也自动放行。
    """
    data_dir = _data_dir()
    hits: list[str] = []
    if not data_dir.is_dir():
        return hits
    for path in data_dir.glob("*.csv"):
        if path.name in question or (path.stem and path.stem in question):
            hits.append(path.name)
    return hits


def _enrich_pending(pending: dict, values: dict, trusted: list[str]) -> dict:
    """给待审批 payload 补策略理由与数据集列名(会话快照恢复审批卡用)。"""
    enriched = dict(pending)
    dataset = enriched.get("dataset_name") or values.get("dataset_name") or ""
    question = enriched.get("question") or values.get("question") or ""
    meta = _dataset_meta(dataset)
    policy = approval_policy.decide(question, dataset, meta, trusted)
    enriched["reasons"] = policy["reasons"]
    enriched["columns"] = _meta_columns(meta)
    return enriched


def _atomic_rewrite_jsonl(path: Path, transform) -> tuple[int, int]:
    """原子重写 jsonl 文件。

    transform(record) -> record | None:
        - 返回 dict(可同对象或修改后的副本):保留该行(用修改后的内容重写);
        - 返回 None:删除该行。
    解析失败的坏行原样保留(不破坏其他数据)。

    返回 (保留行数, 删除/修改行数?其实是处理过的记录数减去保留数):
        这里返回 (kept, removed),其中 removed = 解析成功且被 transform 判为删除的行数。
    用临时文件 + os.replace 保证原子:要么整盘替换,要么原文件不动。
    """
    if not path.is_file():
        return 0, 0
    kept = 0
    removed = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
            for line in src:
                stripped = line.strip()
                if not stripped:
                    dst.write(line)
                    kept += 1
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    # 坏行原样保留:不要因为一条坏数据影响整份索引
                    dst.write(line)
                    kept += 1
                    continue
                result = transform(record)
                if result is None:
                    removed += 1
                else:
                    dst.write(json.dumps(result, ensure_ascii=False) + "\n")
                    kept += 1
        os.replace(tmp, path)
    except Exception:
        # 替换失败时清理临时文件,原文件不动
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return kept, removed


def _thread_run_ids(thread_id: str) -> list[str]:
    """从 index.jsonl 找出该 thread 产生的全部 run_id(按白名单过滤,防路径穿越)。"""
    index_path = OUTPUTS_DIR / "index.jsonl"
    run_ids: list[str] = []
    if not index_path.is_file():
        return run_ids
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("thread_id") == thread_id:
            rid = str(record.get("run_id") or "")
            if _DELETE_RUN_ID_RE.match(rid):
                run_ids.append(rid)
    return run_ids


def _checkpoint_thread_exists(thread_id: str) -> bool:
    """检查该 thread 是否在检查点里有状态(用于 404 判定)。"""
    try:
        snapshot = agent_main._get_graph().get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        if getattr(snapshot, "values", None):
            return True
        # 有些检查点实现把状态放在 tasks/next 上,但 LangGraph get_state.values 是主入口
    except Exception:  # noqa: BLE001 - 检查点读取异常按「不存在」处理
        return False
    return False


def _is_corrupted_title(s: str) -> bool:
    """历史数据里因编码损坏出现的乱码标题判定:含 2 个以上连续 ? 或含 U+FFFD 时,
    列表展示用「会话 <thread_id 前 6 位>」兜底(不修改库里存的原值)。"""
    if not s:
        return False
    return "??" in s or "\ufffd" in s


def _answer_payload(
    thread_id: str,
    state: dict,
    *,
    approved_by: str = "none",
    reasons: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    """把图最终状态整理成 answer 事件载荷;错误原样取 state.error_analysis。

    approved_by: "policy"(策略自动放行)/ "human"(人工批准)/ "none"(无需审批)。
    extra: 数据库问答附加字段(sql / sql_row_count / db_label / warnings),
        普通提问传 None;warnings 与图状态里的警告合并,不覆盖。
    """
    error_analysis = state.get("error_analysis") or ""
    final_answer = state.get("final_answer") or ""
    # clarify 轮次有回答但没执行:单独给一个状态,前端据此渲染「待补充」而不是「未完成」
    if error_analysis:
        status = "error"
    elif state.get("next_action") == "clarify":
        status = "clarify"
    elif state.get("next_action") == "deliver":
        # 交付轮次:复用上一轮归档,本轮没有新 run_id,前端渲染「已复用」而不是「未完成」
        status = "deliver"
    else:
        status = "success"
    payload = {
        "thread_id": thread_id,
        "run_id": state.get("run_id") or "",
        "deliver_run_id": state.get("deliver_run_id") or "",
        "status": status,
        "final_answer": final_answer,
        "error_analysis": error_analysis,
        "warnings": state.get("warnings") or [],
        "plan": state.get("plan") or [],
        "approved_by": approved_by,
        "reasons": list(reasons or []),
    }
    if extra:
        merged_extra = dict(extra)
        extra_warnings = merged_extra.pop("warnings", None)
        if extra_warnings:
            # 图状态里可能已有分析阶段警告,体检原因追加而不是覆盖
            payload["warnings"] = list(payload["warnings"]) + [
                str(item) for item in extra_warnings
            ]
        payload.update(merged_extra)
    return payload


# ---------- 图执行 worker(全部跑在线程池) ----------

def _status_events(bridge: EventBridge):
    """把 main 的进度回调 {"node","phase"} 包成 SSE 事件 {"event":"status","data":...}。

    为什么在这里转换而不是改 main 的回调协议:
        progress_callback 的协议是 CLI 也能复用的纯进度字典;
        SSE 事件名是 Web 层概念,转换归 Web 层。
    """
    def _cb(progress: dict) -> None:
        bridge.push_threadsafe({"event": "status", "data": progress})

    return _cb


def _push_plan_auto(
    bridge: EventBridge, thread_id: str, dataset: str, plan: list[str], reasons: list[str]
) -> None:
    """策略自动放行时推 plan_auto 事件:前端不弹审批卡,只在进度区标记「自动通过」。"""
    bridge.push_threadsafe(
        {
            "event": "plan_auto",
            "data": {
                "thread_id": thread_id,
                "dataset_name": dataset,
                "plan": plan,
                "reasons": list(reasons),
                "approved_by": "policy",
            },
        }
    )


def _db_result_filename(label: str) -> str:
    """生成数据库结果 CSV 文件名:db_<label>_<ts>.csv(标签做安全清洗)。"""
    safe_label = _DB_LABEL_RE.sub("_", str(label or "")).strip("_") or "db"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"db_{safe_label}_{timestamp}.csv"
    if (_data_dir() / filename).exists():
        filename = f"db_{safe_label}_{timestamp}_{datetime.now().strftime('%f')[:3]}.csv"
    return filename


def _friendly_db_error(exc: BaseException) -> str:
    """把数据库/凭据异常转成可直接展示的中文,不暴露 traceback。"""
    if isinstance(exc, QueryCancelled):
        return "查询已取消"
    if isinstance(exc, (AdapterError, ValueError, FileNotFoundError, TimeoutError)):
        return str(exc)
    return f"数据库查询失败: {type(exc).__name__}: {exc}"


def _db_failure_status(exc: BaseException) -> str:
    """把数据库异常映射成审计状态:超时单独标记,其余(含取消)一律 failed。

    Args:
        exc: 本轮数据库问答抛出的异常。

    Returns:
        审计字段 status,取值 timeout 或 failed。
    """
    return "timeout" if isinstance(exc, TimeoutError) else "failed"


def _backfill_db_run_id(thread_id: str, run_id: str) -> None:
    """把本轮 run_id 补写进数据库查询审计;同一个 run 只补一次。

    Args:
        thread_id: 会话 id,用于找到本轮审计记录。
        run_id: 图执行结束后的运行 ID;空串表示本轮没有产生运行记录。
    """
    query_id = _DB_QUERY_IDS.get(thread_id) or ""
    if not query_id or not run_id:
        return
    try:
        db_connections.attach_run_id(query_id, run_id)
    except Exception as exc:  # noqa: BLE001 - 审计补写失败不能影响主流程
        logger.warning("补写数据库审计 run_id 失败: %s", exc)


# 单轮最多澄清 3 个术语:再多就说明问题本身太模糊,应先让用户改写
MAX_CLARIFY_TERMS = 3


def _detect_unknown_terms(
    question: str, tables: list[dict], glossary: list[dict]
) -> list[dict]:
    """SQL 生成前检查无法确定口径的业务词。

    Args:
        question: 用户原始问题。
        tables: 已合并人工说明的表结构。
        glossary: 已确认的术语口径。

    Returns:
        [{"term", "reason", "candidates"}] 最多 3 条;没有或检查失败时返回空列表。
        检查失败只记日志并按「无未知词」继续,不能因为术语检查把问答卡死。
    """
    try:
        structured_llm = get_llm().with_structured_output(
            GlossaryCheck, method="function_calling"
        )
        result = structured_llm.invoke(
            [
                ("system", GLOSSARY_CHECK_PROMPT),
                ("human", build_glossary_user_prompt(question, tables, glossary)),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - LLM 抖动不应阻断数据库问答
        logger.warning("术语检查失败,按无未知词继续: %s", exc)
        return []
    if result is None:
        return []

    terms: list[dict] = []
    for item in result.unknown_terms:
        term = str(item.term or "").strip()
        if not term:
            continue
        candidates = [
            str(value).strip()
            for value in item.candidates
            if str(value).strip()
        ][:MAX_CLARIFY_TERMS]
        terms.append(
            {
                "term": term,
                "reason": str(item.reason or "").strip(),
                "candidates": candidates,
            }
        )
        if len(terms) >= MAX_CLARIFY_TERMS:
            break
    return terms


def _explain_sql(adapter, runtime: dict, sql: str) -> str:
    """执行前用 EXPLAIN 校验 SQL 引用的表 / 列是否存在。

    Args:
        adapter: 数据库适配器。
        runtime: 运行时连接配置。
        sql: 待校验 SQL。

    Returns:
        空串表示通过;否则返回错误文本。适配器没有 explain 能力时按通过处理。
    """
    explain = getattr(adapter, "explain", None)
    if explain is None:
        return ""
    try:
        with DB_CONNECT_LOCK:
            return str(explain(runtime, sql) or "").strip()
    except Exception as exc:  # noqa: BLE001 - 校验异常按错误文本返回,由上层决定是否修复
        return _friendly_db_error(exc)


def _build_repair_question(question: str, error: str) -> str:
    """把计划校验错误拼成一次性的修复提示,只允许再生成一次 SQL。"""
    return (
        f"{question}\n\n"
        f"【修复要求】上一次生成的 SQL 未通过执行计划校验:{error}\n"
        "请严格只使用给定的表与列(含说明)重新生成一条只读 SQL,不要使用不存在的表或列。"
    )


def _database_table_stats(adapter, runtime: dict, tables: list[dict]) -> dict:
    """读取表规模与文本列,供执行前体检;失败时返回空统计(只做提示,不阻断)。

    Args:
        adapter: 数据库适配器,需实现 table_stats(conn, tables)。
        runtime: 运行时连接配置。
        tables: 已合并语义说明的表结构。

    Returns:
        {"rows", "size_bytes", "text_columns"};适配器不支持或查询失败时返回 {}。
    """
    getter = getattr(adapter, "table_stats", None)
    if getter is None:
        return {}
    try:
        with DB_CONNECT_LOCK:
            return dict(getter(runtime, tables) or {})
    except Exception as exc:  # noqa: BLE001 - 体检是增强能力,失败不能挡住查询
        logger.info("读取表规模失败,跳过执行前体检: %s", exc)
        return {}


def _run_select(
    adapter,
    runtime: dict,
    sql: str,
    timeout_seconds: float | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """执行只读查询;适配器不支持按调用超时 / 取消时自动降级。

    Args:
        adapter: 数据库适配器。
        runtime: 运行时连接配置。
        sql: 已通过安全校验与体检的 SQL。
        timeout_seconds: 本次查询超时秒数;None 表示使用适配器默认值。
        cancel_event: 取消信号;None 表示不可取消(测试替身场景)。

    Returns:
        adapter.run_select 的结果字典。
    """
    parameters = inspect.signature(adapter.run_select).parameters
    kwargs: dict = {}
    if timeout_seconds is not None and "timeout_seconds" in parameters:
        kwargs["timeout_seconds"] = timeout_seconds
    if cancel_event is not None and "cancel_event" in parameters:
        kwargs["cancel_event"] = cancel_event
    return adapter.run_select(runtime, sql, max_rows=DEFAULT_MAX_ROWS, **kwargs)


def _run_db_query_with_progress(
    thread_id: str,
    database_id: str,
    adapter,
    runtime: dict,
    sql: str,
    timeout_seconds: float,
    bridge: EventBridge,
) -> dict:
    """把只读查询放进独立线程执行,按秒推进度,支持取消与超时。

    进度事件由当前 worker 线程推送(查询本身在 executor 线程里跑);
    客户端断开时 bridge 会被标记取消,这里在 1 秒内感知并置位 cancel_event。

    Args:
        thread_id: 会话 id,用于前端定位结果卡。
        database_id: 已保存的连接 id。
        adapter: 数据库适配器。
        runtime: 运行时连接配置。
        sql: 已通过校验与体检的 SQL。
        timeout_seconds: 本次查询的硬超时秒数。
        bridge: SSE 事件桥。

    Returns:
        adapter.run_select 的结果字典。

    Raises:
        TimeoutError: 超过 timeout_seconds 仍未完成。
        db_adapter.QueryCancelled: 客户端断开,查询被取消。
        Exception: 查询本身失败时原样上抛。
    """
    cancel_event = threading.Event()
    bridge.push_threadsafe(
        {
            "event": "db_start",
            "data": {"thread_id": thread_id, "database_id": database_id, "sql": sql},
        }
    )
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="db-query"
    )
    future = executor.submit(
        _run_select, adapter, runtime, sql, timeout_seconds, cancel_event
    )
    started = time.monotonic()
    try:
        while True:
            if getattr(bridge, "cancelled", False):
                cancel_event.set()
                future.cancel()
                raise QueryCancelled("用户已取消查询")
            # wait 只等完成状态,不用 future.result(timeout=...):
            # Python 3.11+ 下后者与内置 TimeoutError 同义,会把查询超时误当成轮询超时
            done, _ = concurrent.futures.wait([future], timeout=1.0)
            if done:
                return future.result()
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                cancel_event.set()
                future.cancel()
                raise TimeoutError(
                    f"查询超过 {timeout_seconds:g} 秒未完成,已取消;"
                    "可加 WHERE 过滤或改用采样"
                )
            bridge.push_threadsafe(
                {
                    "event": "db_progress",
                    "data": {"thread_id": thread_id, "elapsed": int(elapsed)},
                }
            )
    except BaseException:
        # 生成器关闭 / 任务取消 / 任一异常:确保数据库侧收到取消信号
        cancel_event.set()
        future.cancel()
        raise
    finally:
        executor.shutdown(wait=False)


def _prepare_database_question(
    thread_id: str,
    question: str,
    database_id: str,
    bridge: EventBridge,
    extra_terms: list[dict] | None = None,
    skip_check: bool = False,
) -> tuple[str, dict] | None:
    """主进程完成「生成 SQL → 校验执行 → 导出 CSV」,失败时推 sql/answer 事件。

    Args:
        thread_id: 会话 id。
        question: 用户原始问题(术语澄清后可能带「按默认口径」前缀)。
        database_id: 已保存的连接 id。
        bridge: SSE 事件桥。
        extra_terms: 本轮临时生效的术语口径(remember=false 的确认结果)。
        skip_check: True 时不重新检测未知术语(澄清后的重跑,避免二次追问)。

    Returns:
        成功时返回 (交给 Agent 的分析问题, 结果卡附加字段);
        需要澄清时推 clarify 事件并返回 None(本轮不生成 SQL、不执行);
        其他失败返回 None(sql 事件与错误 answer 已推送,审计已落盘)。
    """
    label = ""
    sql = ""
    # 只统计「真实查询」耗时:SQL 生成 / EXPLAIN / 体检不计入 elapsed_ms
    query_started: float | None = None
    try:
        record = db_connections.get_connection(database_id)
        if record is None:
            raise ValueError(f"数据库连接不存在: {database_id}")
        label = str(record.get("label") or "")
        runtime = {**record, "password": db_connections.get_password(database_id)}
        adapter = get_adapter(record["kind"])

        # 元数据查询与真实查询各自串行建连,避免瞬时打满数据库
        with DB_CONNECT_LOCK:
            tables = adapter.list_tables(runtime)
        if not tables:
            raise ValueError("库中没有可查询的表或视图")
        # 合并人工填写的表 / 列说明,SQL 生成时按业务口径而不是列名猜测
        tables = db_semantics.merge_semantics(database_id, tables)

        # 术语表 + 本轮临时口径:用户确认过的口径优先于模型猜测
        glossary_terms = db_glossary.terms_for_prompt(database_id)
        if extra_terms:
            merged_terms = {str(item.get("term")): item for item in glossary_terms}
            for item in extra_terms:
                term = str(item.get("term") or "").strip()
                if term:
                    merged_terms[term] = item
            glossary_terms = [merged_terms[key] for key in sorted(merged_terms)]

        # 已有待澄清记录时跳过检测,避免同一轮重复追问
        if not skip_check and db_glossary.load_pending(thread_id) is None:
            unknown_terms = _detect_unknown_terms(question, tables, glossary_terms)
            if unknown_terms:
                # 待澄清也留一条历史:否则这类轮次只存在于会话里,无法在历史页回溯
                db_connections.append_query_audit(
                    {
                        "db_id": database_id,
                        "db_label": label,
                        "question": question,
                        "sql": "",
                        "ok": False,
                        "status": "clarify",
                        "row_count": 0,
                        "elapsed_ms": 0,
                        "error": "",
                        "csv_name": "",
                    }
                )
                _DB_QUERY_IDS.pop(thread_id, None)
                db_glossary.save_pending(
                    {
                        "thread_id": thread_id,
                        "database_id": database_id,
                        "question": question,
                        "terms": unknown_terms,
                    }
                )
                bridge.push_threadsafe(
                    {
                        "event": "clarify",
                        "data": {
                            "thread_id": thread_id,
                            "database_id": database_id,
                            "question": question,
                            "terms": unknown_terms,
                        },
                    }
                )
                return None

        generated = ask_db.generate_sql(
            question, tables, dialect=record["kind"], glossary=glossary_terms
        )
        sql = str(generated.sql or "").strip()
        if not sql:
            raise ValueError("LLM 未生成 SQL,请换个问法重试")

        # 执行前确定性校验:EXPLAIN 只解析 SQL,不会取数;失败最多重生成一次
        explain_error = _explain_sql(adapter, runtime, sql)
        if explain_error:
            logger.info("SQL 执行计划校验失败,尝试修复一次: %s", explain_error)
            repaired = ask_db.generate_sql(
                _build_repair_question(question, explain_error),
                tables,
                dialect=record["kind"],
                glossary=glossary_terms,
            )
            sql = str(repaired.sql or "").strip()
            explain_error = (
                _explain_sql(adapter, runtime, sql) if sql else "LLM 未返回 SQL"
            )
            if explain_error:
                # 二次仍失败:停止执行,绝不把错误 SQL 发给数据库
                raise ValueError(
                    f"生成的 SQL 引用了不存在的表/列,已停止执行。{explain_error}"
                )

        # 执行前体检:静态识别「大表 + 重查询」,只读 SQL 与表规模,不触发真实取数
        table_stats = _database_table_stats(adapter, runtime, tables)
        guard = db_guard.assess(sql, table_stats)
        warnings = list(guard["reasons"])
        timeout_seconds = None
        if guard["level"] == "block":
            logger.info("SQL 体检判定 block,自动重写一次: %s", warnings)
            rewritten = ask_db.generate_sql(
                question,
                tables,
                dialect=record["kind"],
                glossary=glossary_terms,
                feedback=warnings,  # 把体检原因交给模型,要求先过滤/采样再聚合
            )
            rewritten_sql = str(rewritten.sql or "").strip()
            if rewritten_sql:
                sql = rewritten_sql
                rewritten_error = _explain_sql(adapter, runtime, sql)
                if rewritten_error:
                    raise ValueError(
                        f"重写后的 SQL 未通过执行计划校验,已停止执行。{rewritten_error}"
                    )
                guard = db_guard.assess(sql, table_stats)
                warnings = list(guard["reasons"])
            if not rewritten_sql or guard["level"] == "block":
                # 重写后仍不合格:按长超时执行一次,并如实告知已重写
                timeout_seconds = DB_GUARD_TIMEOUT_SECONDS
                warnings.append("已自动重写一次")

        # 本次查询的硬超时:block 重写后沿用更长超时,否则取 DB_QUERY_TIMEOUT_SECONDS
        query_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else DB_QUERY_TIMEOUT_SECONDS
        )
        query_started = time.monotonic()
        with DB_CONNECT_LOCK:
            result = _run_db_query_with_progress(
                thread_id,
                database_id,
                adapter,
                runtime,
                sql,
                timeout_seconds=query_timeout,
                bridge=bridge,
            )
        elapsed_ms = int((time.monotonic() - query_started) * 1000)
        frame = result["dataframe"]
        row_count = int(result.get("row_count") or len(frame))

        filename = _db_result_filename(label)
        data_dir = _data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        frame.to_csv(data_dir / filename, index=False, encoding="utf-8-sig")

        query_id = str(
            db_connections.append_query_audit(
                {
                    "db_id": database_id,
                    "db_label": label,
                    "question": question,
                    "sql": sql,
                    "ok": True,
                    "status": "ok",
                    "row_count": row_count,
                    "elapsed_ms": elapsed_ms,
                    "error": "",
                    "csv_name": filename,
                    "cached": bool(result.get("cached")),
                    "warnings": warnings,
                }
            )
            or ""
        )
        # 本轮审计 id:Agent 跑完后据此把 run_id 补写回这条记录
        _DB_QUERY_IDS[thread_id] = query_id
        # sql 事件必须在 status 事件之前推给前端,结果卡据此立即显示 SQL
        cached = bool(result.get("cached"))
        bridge.push_threadsafe(
            {
                "event": "sql",
                "data": {
                    "thread_id": thread_id,
                    "sql": sql,
                    "row_count": row_count,
                    "db_label": label,
                    "ok": True,
                    "truncated": bool(result.get("truncated")),
                    "cached": cached,
                },
            }
        )
        agent_question = f"分析 {filename}：{question}"
        return agent_question, {
            "sql": sql,
            "sql_row_count": row_count,
            "db_label": label,
            "cached": cached,
            "warnings": warnings,
        }
    except Exception as exc:  # noqa: BLE001 - 所有失败都要变成可读事件,不能静默
        logger.warning("数据库问答失败: %s", exc)
        message = _friendly_db_error(exc)
        elapsed_ms = (
            int((time.monotonic() - query_started) * 1000)
            if query_started is not None
            else 0
        )
        db_connections.append_query_audit(
            {
                "db_id": database_id,
                "db_label": label,
                "question": question,
                "sql": sql,
                "ok": False,
                "status": _db_failure_status(exc),
                "row_count": 0,
                "elapsed_ms": elapsed_ms,
                "error": message,
                "csv_name": "",
            }
        )
        _DB_QUERY_IDS.pop(thread_id, None)
        bridge.push_threadsafe(
            {
                "event": "sql",
                "data": {
                    "thread_id": thread_id,
                    "sql": sql,
                    "row_count": 0,
                    "db_label": label,
                    "ok": False,
                    "error": message,
                },
            }
        )
        bridge.push_threadsafe(
            {
                "event": "answer",
                "data": {
                    "thread_id": thread_id,
                    "run_id": "",
                    "status": "error",
                    "final_answer": "",
                    "error_analysis": message,
                    "warnings": [],
                    "plan": [],
                    "approved_by": "none",
                    "reasons": [],
                    "sql": sql,
                    "sql_row_count": 0,
                    "db_label": label,
                },
            }
        )
        _DB_CONTEXT.pop(thread_id, None)
        return None


def _execute_question(
    thread_id: str, question: str, continue_session: bool, bridge: EventBridge,
    display_question: str | None = None,
    database_id: str | None = None,
    extra_terms: list[dict] | None = None,
    skip_check: bool = False,
) -> None:
    """worker 线程里跑完整一轮提问:策略分级 → 执行图 → 推 plan/plan_auto/answer。

    两级判定:
        pre-run:能确定数据集且已信任、问题没点名其他未信任数据集时,先判一次;
                 auto 则用 auto_approve=True 一次跑完,图根本不产生中断。
        post-plan:图暂停在 review_plan 后用真实 dataset_name 再判一次;
                  auto 则推 plan_auto 并立即 resume(approve),confirm 才弹卡。

    display_question:用户原始问题(不含附件的「分析 xxx：」前缀),用于会话标题与审计。
    database_id:选中的数据库连接;非空时先在主进程查库并导出 CSV,再复用分析链路。
    extra_terms:本轮临时生效的术语口径(remember=false 的确认结果)。
    skip_check:澄清后重跑时为 True,不再重复检测未知术语。
    """
    title = display_question or question
    # 上一轮分析运行 ID:交给 planner 判断「生成网页 / 导出报告」能否直接复用归档
    last_run_id = _session_last_run_id(thread_id)
    db_extra: dict = {}
    try:
        # 标记当前会话正在执行,防止同一会话并发多个请求覆盖检查点
        _RUNNING.add(thread_id)
        if database_id:
            prepared = _prepare_database_question(
                thread_id, question, database_id, bridge,
                extra_terms=extra_terms, skip_check=skip_check,
            )
            if prepared is None:
                _record_session(thread_id, title, "")
                return
            question, db_extra = prepared
            _DB_CONTEXT[thread_id] = db_extra
        else:
            _DB_CONTEXT.pop(thread_id, None)
            _DB_QUERY_IDS.pop(thread_id, None)
        # 加载本会话已经信任的数据集集合(持久化在 trusted.json 里)
        trusted = _load_trusted(thread_id)
        # 从问题里抽取显式提到的数据集(支持多个,例如「同时看 A 和 B」)
        mentioned = _question_datasets(question)
        # 候选数据集:优先复用本会话已锁定的 session_dataset,
        # 否则取问题里第一个被提到的数据集(planner 没判出来之前先借这个做预判)
        candidate = _session_dataset(thread_id) or (mentioned[0] if mentioned else "")
        # pre-run 阶段的策略判定结果,None 表示走图内中断路径
        pre = None  
        # 只有当候选数据集 以及 问题里提到的所有数据集 都在 trusted 里时,
        # 才值得做 pre-run 预判;否则一定需要审批(要么 confirm、要么 auto 但图内再判)
        if candidate and candidate in trusted and all(m in trusted for m in mentioned):
            pre_decision = approval_policy.decide(
                question, candidate, _dataset_meta(candidate), trusted
            )
            # pre-run 预判只有 auto 才有用:confirm 时我们还是得让图跑到 review_plan 暂停,
            # 因为图里 plan 出来的 dataset_name 才是最终真值
            if pre_decision["level"] == "auto":
                pre = pre_decision

        # ---------- pre-run auto 路径:全程不产生中断 ----------
        if pre:
            # 策略自动放行(免中断路径):auto_approve 语义不变,只是由策略决定传入 True
            state = run_agent(
                question,
                thread_id=thread_id,
                continue_session=continue_session,
                auto_approve=True,
                progress_callback=_status_events(bridge),
                last_run_id=last_run_id,
            )
            # planner 可能修正数据集名(问题没说或说错了),取最终真值
            dataset = state.get("dataset_name") or candidate
            # 数据库问答:运行 ID 归档后回填到查询历史,便于「打开运行」
            _backfill_db_run_id(thread_id, state.get("run_id") or "")
            # 先推 plan_auto 让前端知道这一步被策略自动批准了(展示用)
            _push_plan_auto(bridge, thread_id, dataset, state.get("plan") or [], pre["reasons"])
            # 审计留痕:记录这次自动放行是策略判定的、为什么放行
            _audit_approval(thread_id, dataset, title, "auto", pre["reasons"], "policy", "auto")
            bridge.push_threadsafe(
                {"event": "answer",
                 "data": _answer_payload(thread_id, state, approved_by="policy", reasons=pre["reasons"], extra=db_extra)}
            )
            # 会话记录:标题用 display_question,不要暴露内部拼的前缀
            _record_session(thread_id, title, state.get("run_id") or "")
            return

        # ---------- 进入图内路径:让图自己跑到 review_plan 暂停 ----------
        # 这里不传 auto_approve=True,图会在 review_plan 节点自然暂停
        state = run_agent(
            question,
            thread_id=thread_id,
            continue_session=continue_session,
            progress_callback=_status_events(bridge),
            last_run_id=last_run_id,
        )
        # 先查待审批:有计划就走策略分级,否则直接发 answer。
        # 不用异常判断是否暂停——暂停是正常分支,不是错误。
        pending = get_pending_interrupt(state)
        if pending and pending.get("plan"):
            # 以 planner 在 plan 里实际写出的 dataset_name 为真值(覆盖候选)
            dataset = pending.get("dataset_name") or state.get("dataset_name") or ""
            # 拉一下 schema 元信息,给策略判定列名风险 以及 前端计划自检用
            meta = _dataset_meta(dataset)
            # post-plan 阶段再做一次策略判定,这次基于真实 plan + 真实 dataset
            decision = approval_policy.decide(question, dataset, meta, _load_trusted(thread_id))
            if decision["level"] == "auto":
                # ---------- post-plan auto 路径:中断后立即 resume,用户无感知 ----------
                # 策略自动放行(中断路径):告知前端后立刻按原计划恢复,用户无感知
                _push_plan_auto(bridge, thread_id, dataset, pending.get("plan") or [], decision["reasons"])
                _audit_approval(thread_id, dataset, title, "auto", decision["reasons"], "policy", "auto")
                # 用 pending 里的原计划直接 approve 并恢复执行,不再让 planner 重新规划
                state = resume_agent(
                    thread_id,
                    {"action": "approve", "plan": pending.get("plan") or []},
                    progress_callback=_status_events(bridge),
                )
                _backfill_db_run_id(thread_id, state.get("run_id") or "")
                bridge.push_threadsafe(
                    {"event": "answer",
                     "data": _answer_payload(thread_id, state, approved_by="policy", reasons=decision["reasons"], extra=db_extra)}
                )
            else:
                # ---------- post-plan confirm 路径:弹审批卡等待用户操作 ----------
                bridge.push_threadsafe(
                    {
                        "event": "plan",
                        "data": {
                            "thread_id": thread_id,
                            # 检查点里的 question 可能带附件前缀,前端展示时剥掉
                            "question": pending.get("question") or question,
                            "dataset_name": dataset,
                            "plan": pending.get("plan") or [],
                            "reason": pending.get("reason") or "",
                            # 策略理由 + 列名:前端展示「为什么被问」并做计划自检
                            "reasons": decision["reasons"],
                            "columns": _meta_columns(meta),
                        },
                    }
                )
                # 审计里 confirm 级别表示「已产生审批卡,等待用户」
                _audit_approval(
                    thread_id, dataset, title, "confirm", decision["reasons"], "policy", "confirm"
                )
        else:
            # 图没暂停在 review_plan(例如数据集已存在、planner 不需要审批、或纯计算),
            # 直接把 answer 推给前端即可
            _backfill_db_run_id(thread_id, state.get("run_id") or "")
            bridge.push_threadsafe({"event": "answer", "data": _answer_payload(thread_id, state, extra=db_extra)})
        _record_session(thread_id, title, state.get("run_id") or "")
    except Exception as exc:  # noqa: BLE001 - 编程错误也要让前端看到,而不是 SSE 无声结束
        # 编程错误(lint/网络/依赖)也要让前端看到,否则 SSE 无声结束用户会卡住
        logger.exception("执行提问时出错")
        bridge.push_threadsafe(
            {
                "event": "answer",
                "data": {
                    "thread_id": thread_id,
                    "run_id": "",
                    "status": "error",
                    "final_answer": "",
                    "error_analysis": f"服务内部错误: {type(exc).__name__}: {exc}",
                    "warnings": [],
                    "plan": [],
                    "approved_by": "none",
                    "reasons": [],
                    **db_extra,
                },
            }
        )
    finally:
        # 无论成功/失败/异常都要清掉 _RUNNING 标记,并推 done 让前端解锁发送按钮
        _RUNNING.discard(thread_id)
        bridge.push_threadsafe({"event": "done", "data": {"thread_id": thread_id}})


def _execute_approval(
    thread_id: str, decision: dict, trust: bool, bridge: EventBridge
) -> None:
    """worker 线程里恢复审批:resume_agent → 审计 → (可选)登记信任 → 推 answer。"""
    try:
        _RUNNING.add(thread_id)
        # resume 前先取 pending:dataset / question 供审计与信任登记(resume 后中断就消失了)
        pending = load_pending_interrupt(thread_id) or {}
        dataset = pending.get("dataset_name") or _session_dataset(thread_id)
        # pending.question 来自图中状态,可能带附件前缀;审计与策略都用用户原话
        question = _strip_dataset_prefix(pending.get("question") or "", dataset)
        state = resume_agent(thread_id, decision, progress_callback=_status_events(bridge))
        # 数据库问答审批恢复后同样把 run_id 回填进查询历史
        _backfill_db_run_id(thread_id, state.get("run_id") or "")
        # 人工动作也记审计:reasons 重算一次策略理由,讲清楚「当时为什么被问」
        policy = approval_policy.decide(
            question, dataset, _dataset_meta(dataset), _load_trusted(thread_id)
        )
        _audit_approval(
            thread_id, dataset, question, policy["level"], policy["reasons"],
            "human", str(decision.get("action") or ""),
        )
        # 「批准 + 信任」:后续同数据集轮次走策略自动放行
        if trust and decision.get("action") in ("approve", "edit") and dataset:
            _set_trusted(thread_id, dataset, True)
        bridge.push_threadsafe(
            {"event": "answer",
             "data": _answer_payload(thread_id, state, approved_by="human", extra=_DB_CONTEXT.get(thread_id, {}))}
        )
        # 审批轮没有新问题:标题沿用该会话已有记录(由读取侧去重保证最新)
        # 检查点里的 question 可能带附件前缀,落库前剥掉,避免覆盖成内部标题
        _record_session(
            thread_id,
            _strip_dataset_prefix(state.get("question") or "", dataset),
            state.get("run_id") or "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("恢复审批时出错")
        bridge.push_threadsafe(
            {
                "event": "answer",
                "data": {
                    "thread_id": thread_id,
                    "run_id": "",
                    "status": "error",
                    "final_answer": "",
                    "error_analysis": f"服务内部错误: {type(exc).__name__}: {exc}",
                    "warnings": [],
                    "plan": [],
                    "approved_by": "none",
                    "reasons": [],
                },
            }
        )
    finally:
        _RUNNING.discard(thread_id)
        bridge.push_threadsafe({"event": "done", "data": {"thread_id": thread_id}})


async def _run_serialized(bridge: EventBridge, work) -> None:
    """监督任务:排队拿锁 → 线程池执行 → 释放锁。

    与 SSE 连接解耦:客户端断开后 worker 照常跑完(状态照常落库),
    锁也只随 worker 结束释放,保证任意时刻只有一个图在执行。
    """
    if _RUN_LOCK.locked():
        bridge.push_local({"event": "queued", "data": {"position": 1}})
    await _RUN_LOCK.acquire()
    try:
        # 排队期间客户端已断开:不再启动图,直接放锁
        if bridge.cancelled:
            return
        await run_in_threadpool(work)
    finally:
        _RUN_LOCK.release()


def _sse_response(bridge: EventBridge) -> StreamingResponse:
    """统一的 SSE 响应:客户端断开时取消 bridge(worker 不受影响)。"""
    async def gen():
        try:
            async for chunk in sse_stream(bridge):
                yield chunk
        finally:
            bridge.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        # 禁用缓存与反向代理缓冲,保证事件实时到达
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- 路由:静态页 ----------



app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------- 路由:健康与数据源 ----------

def _docker_ok() -> bool:
    """快速探测 Docker 可用性;任何异常都返回 False,绝不向上抛。"""
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "version"], capture_output=True, timeout=2, check=False
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - 超时 / 权限 / 守护进程异常都按不可用处理
        return False


def _checkpointer_kind() -> str:
    """报告配置的检查点后端;sqlite 包缺失时如实报告降级为 memory。"""
    backend = os.getenv("CHECKPOINT_BACKEND", "sqlite").strip().lower()
    if backend == "memory":
        return "memory"
    try:
        import langgraph.checkpoint.sqlite  # noqa: F401
    except ImportError:
        return "memory"
    return "sqlite"






def _sanitize_stem(raw: str) -> tuple[str, str]:
    """清洗上传文件名,返回 (stem, suffix)。

    - 只取最后一段,去掉客户端可能带来的目录部分;
    - 非法字符替换为下划线,空名兜底为 data;
    - suffix 统一小写,是否允许由调用方按 UPLOAD_SUFFIXES 判断。
    """
    name = Path(raw.replace("\\", "/")).name
    name = _FILENAME_BAD_RE.sub("_", name).strip("._ ")
    if not name:
        return "data", ""
    stem, suffix = os.path.splitext(name)
    return (stem.strip("._ ") or "data"), suffix.lower()


def _dedup_path(directory: Path, name: str) -> Path:
    """重名自动加后缀:data.csv → data_2.csv → data_3.csv ..."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    for i in range(2, 1000):
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=400, detail="重名文件过多,请手动清理数据目录")


def _excel_to_frames(content: bytes, suffix: str) -> dict[str, pd.DataFrame]:
    """把 Excel 读成 {sheet_name: DataFrame};.xlsx 用 openpyxl,.xls 用 xlrd。"""
    engine = "openpyxl" if suffix == ".xlsx" else "xlrd"
    return pd.read_excel(BytesIO(content), sheet_name=None, engine=engine)


def _json_to_frame(content: bytes) -> pd.DataFrame:
    """把 JSON 转成 DataFrame:先按 records 读,失败再 json_normalize。"""
    try:
        return pd.read_json(BytesIO(content))
    except ValueError:
        return pd.json_normalize(json.loads(content.decode("utf-8-sig")))


def _write_upload_as_csv(
    content: bytes, stem: str, suffix: str, data_dir: Path
) -> list[str]:
    """把上传内容统一转成 UTF-8 CSV 落盘,返回实际写入的文件名列表。

    为什么统一转 CSV:
        下游 describe_dataset / 沙箱 / 代码生成只认 CSV;
        在入口做一次转换,后续链路零改动,也不会把解析库带进沙箱镜像。
    """
    written: list[str] = []

    def _save(frame: pd.DataFrame, name: str) -> None:
        target = _dedup_path(data_dir, name)
        frame.to_csv(target, index=False, encoding="utf-8")
        written.append(target.name)

    if suffix == ".csv":
        # CSV 原样落盘:编码交给 tools.describe_dataset 的编码探测处理
        target = _dedup_path(data_dir, f"{stem}.csv")
        target.write_bytes(content)
        return [target.name]

    if suffix in (".xlsx", ".xls"):
        sheets = _excel_to_frames(content, suffix)
        # 单 sheet 用原文件名;多 sheet 拆成 <stem>__<sheet>.csv,避免互相覆盖
        for sheet_name, frame in sheets.items():
            safe_sheet = _FILENAME_BAD_RE.sub("_", str(sheet_name)).strip("._ ") or "sheet"
            name = f"{stem}.csv" if len(sheets) == 1 else f"{stem}__{safe_sheet}.csv"
            _save(frame, name)
        return written

    # .json
    _save(_json_to_frame(content), f"{stem}.csv")
    return written

def _resolve_run_dir(run_id: str) -> Path:
    """把 run_id 解析成 runs 下的目录并做穿越校验;非法一律 404。"""
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=404, detail="运行记录不存在")
    runs_root = (OUTPUTS_DIR / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if run_dir.parent != runs_root or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return run_dir


# ---- 路由注册: 端点实现按资源拆到 web/routers/,这里只负责挂载 ----
from web.routers.datasets import router as datasets_router  # noqa: E402
from web.routers.databases import router as databases_router  # noqa: E402
from web.routers.sessions import router as sessions_router  # noqa: E402
from web.routers.runs import router as runs_router  # noqa: E402

app.include_router(datasets_router)
app.include_router(databases_router)
app.include_router(sessions_router)
app.include_router(runs_router)
