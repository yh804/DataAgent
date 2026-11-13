"""运行归档: 图表回收、JSON 安全序列化、result.json/report.md 落盘、index 追加。"""


# dotenv 必须先于 tools / sandbox 导入：后端配置在模块导入时读取环境变量
from dotenv import load_dotenv

load_dotenv()

import json
import math
import os
import re
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 轻量可观测性：记录节点 / LLM / 工具耗时,落盘 outputs/traces/
from trace import TraceCallbackHandler, trace_enabled, write_trace

# LangGraph 节点内使用标准消息类型承载对话历史
# BaseCallbackHandler:Web 进度推送用的轻量回调处理器(批次 D)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

# StateGraph 组装图结构;START / END 是图的虚拟起止节点
from langgraph.graph import END, START, StateGraph

# interrupt：在节点内暂停图并暴露 payload;Command：用 resume 值恢复暂停的执行
from langgraph.types import Command, interrupt

# 检查点后端装配：SQLite / memory 的选择与降级策略收敛在 checkpoint 模块
from checkpoint import build_checkpointer

# LLM 客户端与结构化输出模型
from llm import GeneratedCode, PlannerDecision, ReportSummary, get_llm, mask_secrets

# prompt 模板集中管理,节点内不手写提示词
from prompts import (
    PLANNER_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    build_codegen_system_prompt,
    build_codegen_user_prompt,
    build_planner_user_prompt,
    build_summary_user_prompt,
)
from reporting import render_report
from html_report import render_html_report

# 从 state 模块中导入 AgentState 状态定义类
from state import AgentState

# 工具白名单：发现数据源 / schema 摘要 / 沙箱执行 / 数据指纹
from tools import describe_dataset, execute_code, fingerprint_text, list_datasets

# 结果 sanity check：只报警告不阻断,警告写入 state["warnings"]
from validate import validate_report, validate_result

# 输出根目录与运行归档目录：所有产物(报告 / 运行归档 / trace)都在 outputs/ 下

from common import *
import db_connections

__all__ = [
    "_archive_charts",
    "json_safe",
    "_write_run_archive",
    "_append_run_index",
]

# 数据库结果 CSV 的命名形态:只有这种数据集才需要额外归档 source.csv
_DB_CSV_RE = re.compile(r"^db_.+_\d{8}_\d{6}(?:_\d{3})?\.csv$", re.IGNORECASE)
_DB_DATA_DIR_DEFAULT = "data/processed"


def _archive_db_source(run_id: str, dataset: str, run_dir: Path) -> dict | None:
    """把数据库结果 CSV 复制归档,并返回 result.json 用的 db_query 摘要。

    Args:
        run_id: 运行 ID(仅用于日志语义,目录由 run_dir 指定)。
        dataset: 本轮分析的数据集文件名,形如 db_<label>_<ts>.csv。
        run_dir: 本次运行的归档目录。

    Returns:
        {"db_label","sql","row_count","csv_name"} 或 None(不是数据库数据集)。
        源 CSV 不存在时仍返回摘要,只是没有 source.csv,保证 SQL 可回溯。
    """
    if not _DB_CSV_RE.match(str(dataset or "")):
        return None
    record = db_connections.find_query_by_csv(str(dataset)) or {}
    parsed = db_connections.parse_db_dataset(str(dataset)) or {}
    db_query = {
        "db_label": str(record.get("db_label") or parsed.get("label") or ""),
        "sql": str(record.get("sql") or ""),
        "row_count": record.get("row_count"),
        "csv_name": str(dataset),
    }
    source = Path(os.getenv("SANDBOX_DATA_DIR") or _DB_DATA_DIR_DEFAULT) / str(dataset)
    if source.is_file():
        try:
            # 复制而不是移动:data/processed 里的 CSV 仍可继续被其他分析复用
            shutil.copy2(source, run_dir / "source.csv")
        except OSError:
            # 归档失败不影响主流程,只是这条路运行无法回看原始结果
            pass
    return db_query


def _archive_charts(run_id: str, charts: list[str]) -> tuple[list[str], list[str]]:
    """把本次图表「移动」到 outputs/runs/<run_id>/charts/,返回新路径与警告。

    参数:
        run_id: 运行 ID(目录名)。
        charts: exec_result["charts"],相对 outputs/ 的路径。

    返回:
        (chart_files, warnings)。chart_files 形如 "runs/<run_id>/charts/x.png";
        源文件缺失 / 移动失败时记警告并跳过该图。

    为什么移动而不是复制:
        复制会让同一张图在 outputs/ 里出现两份,历史目录持续膨胀;
        移动后 exec_result["charts"] 仍保留原值(eval 的 charts_min 依赖),
        chart_files 记录归档后的新路径。
    """
    if not charts:
        return [], []

    chart_dir = RUNS_DIR / run_id / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    warnings: list[str] = []
    for index, chart in enumerate(charts):
        source = OUTPUT_DIR / chart
        if not source.is_file():
            warnings.append(f"图表归档跳过(文件不存在): {chart}")
            continue
        dest = chart_dir / source.name
        # 同名冲突(多次分析生成同名图)时加序号,避免 move 覆盖
        if dest.exists():
            dest = chart_dir / f"{source.stem}_{index}{source.suffix}"
        shutil.move(str(source), str(dest))
        moved.append(f"runs/{run_id}/charts/{dest.name}")
    return moved, warnings



def json_safe(value):
    """递归把 NaN / Infinity 替换成 None,让写出的 JSON 能被浏览器 JSON.parse 解析。

    为什么需要:Python 的 json.dumps 默认 allow_nan=True,会把 NaN 原样写成
    裸 NaN 字面量;这不是合法 JSON,浏览器 fetch().json() 会直接抛错,
    表现为前端"运行归档读取失败"。
    """
    if isinstance(value, float):
        # NaN 与正负无穷都无法用标准 JSON 表示
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value



def _write_run_archive(
    run_id: str,
    run: dict,
    report_text: str,
    code: str,
) -> None:
    """写 outputs/runs/<run_id>/{result.json, report.md, code.py, report.html}。

    参数:
        run_id: 运行 ID。
        run: 归档用上下文字典(可 JSON 序列化的 dict)。
        report_text: 渲染好的 Markdown 报告。
        code: 本次生成的代码(空串时跳过 code.py)。

    异常:
        OSError: 目录不可写;由 summarize 的外层 try/except 兜底,
        保证 final_answer 不因归档失败而留空。
    """
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # 数据库来源的运行额外归档 source.csv 与 db_query,data/processed 被清理后仍可回溯
    db_query = _archive_db_source(run_id, str(run.get("dataset") or ""), run_dir)
    if db_query:
        run["db_query"] = db_query
        run["db_label"] = db_query["db_label"]
    if code:
        (run_dir / "code.py").write_text(code, encoding="utf-8")
    (run_dir / "report.md").write_text(report_text, encoding="utf-8")
    # HTML 报告是附加产物:渲染/写盘失败只记 warning,绝不抛出拖垮整轮
    try:
        html_text = render_html_report(run, list(run.get("charts") or []), OUTPUT_DIR)
        (run_dir / "report.html").write_text(html_text, encoding="utf-8")
    except Exception as exc:
        warnings = run.setdefault("warnings", [])
        warnings.append(f"HTML 报告生成失败: {type(exc).__name__}: {exc}")
    (run_dir / "result.json").write_text(
        # 序列化后整体脱敏:error / warnings 里可能带 sk- 密钥
        mask_secrets(
            json.dumps(json_safe(run), ensure_ascii=False, indent=2, default=str)
        ),
        encoding="utf-8",
    )



def _append_run_index(run_id: str, thread_id: str, run: dict) -> None:
    """向 outputs/index.jsonl 追加一行运行索引(供总览 / 后续批次读取)。

    参数:
        run_id: 运行 ID。
        thread_id: 会话 ID(跨进程 / 多轮关联用)。
        run: 与 result.json 相同的上下文字典。

    为什么用 jsonl:追加写不需要读全文件,单行损坏也不影响其他行。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 数据库运行的附加信息:列表页据此显示「库名: 行数」而不是原始文件名
    db_query = run.get("db_query") or {}
    entry = {
        "run_id": run_id,
        "thread_id": thread_id,
        "question": run.get("question", ""),
        "dataset": run.get("dataset", ""),
        "fingerprint": run.get("fingerprint", ""),
        "status": run.get("status", ""),
        "duration_ms": run.get("duration_ms"),
        "iteration": run.get("iteration"),
        "chart_count": len(run.get("charts") or []),
        # 本次会话 token 用量(summarize 由 trace handler 汇总后写入)
        "token_used": run.get("token_used"),
        # 数据库来源:旧行没有这些字段,前端会尝试从 dataset 文件名反推库名
        "db_label": db_query.get("db_label", ""),
        "db_row_count": db_query.get("row_count"),
        "sql_available": bool(db_query.get("sql")),
        "ts": run.get("now", ""),
    }
    with (OUTPUT_DIR / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(mask_secrets(json.dumps(entry, ensure_ascii=False)) + "\n")


