"""公共辅助: token 预算、问题/数据集解析、跨轮上下文、交付意图。"""


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
from llm import GeneratedCode, PlannerDecision, ReportSummary, get_llm

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



__all__ = [
    "_token_budget",
    "_used_tokens",
    "_budget_blocked",
    "_budget_error",
    "_reset_turn_fields",
    "_new_run_id",
    "_extract_question",
    "_extract_previous_turn_context",
    "_extract_dataset_name",
    "_is_delivery_intent",
    "_build_deliver_result",
    "DELIVERY_KEYWORDS",
    "_RUN_ID_RE",
    "OUTPUT_DIR",
    "RUNS_DIR",
    "FINAL_STDOUT_LIMIT",
    "LAST_TRACE_PATH",
    "DOCKER_UNAVAILABLE_KEYWORDS",
    "_FILENAME_RE",
    "_LEAD_VERBS",
    "SESSION_TOKEN_BUDGET_DEFAULT",
]

OUTPUT_DIR = Path("outputs")

RUNS_DIR = OUTPUT_DIR / "runs"

# 最终答案里「原始输出」附录的最大字数：保证数字可回溯,又不让 final_answer 无限膨胀

FINAL_STDOUT_LIMIT = 4000

# 最近一次图运行的 trace 文件路径：供 chat.py / eval 脚本读取,
# 不塞进 state(避免污染图状态与 TypedDict 约束),用模块级变量传递

LAST_TRACE_PATH: str = ""

# Docker 不可用的 stderr 特征：命中即判定「不可重试」,避免连重试 3 次空耗

DOCKER_UNAVAILABLE_KEYWORDS = (
    "docker",
    "failed to connect to the docker api",
    "dockererror",
    "daemon is running",
)

# 文件名兜底正则：允许文件名内部含空格(如「我的 数据.csv」),但排除路径分隔符与
# Windows 非法字符;后缀与真实能力对齐(沙箱只读 CSV,xlsx 由主进程先转 CSV),
# 靠边界校验避免吞下整句问题

_FILENAME_RE = re.compile(
    r"(?<![\w\u4e00-\u9fff.-])"
    r"([^\s\\/:*?\"<>|]+(?:[ \t]+[^\s\\/:*?\"<>|]+)*?\.(?:csv|xlsx))"
    r"(?![\w\u4e00-\u9fff])",
    re.IGNORECASE,
)

# 提问引导词：正则可能把「分析 我的 数据.csv」整体吞成文件名,剥掉引导词还原
# 顺序按长度从长到短,保证「分析一下」先于「分析」匹配

_LEAD_VERBS = (
    "帮我看看", "请帮我", "看一下", "看看", "分析一下", "统计一下",
    "计算一下", "汇总一下", "对比一下", "帮我", "我想", "需要",
    "分析", "统计", "计算", "汇总", "对比", "查询", "绘制", "生成", "画", "请",
)


# 每次会话的 token 预算(默认 0 = 不限):超过后不再发起新的 LLM 调用

SESSION_TOKEN_BUDGET_DEFAULT = 0



def _token_budget() -> int:
    """读取 SESSION_TOKEN_BUDGET(默认 0 表示不限);非法值按不限处理。"""
    raw = os.getenv("SESSION_TOKEN_BUDGET", str(SESSION_TOKEN_BUDGET_DEFAULT)).strip()
    if not raw:
        return SESSION_TOKEN_BUDGET_DEFAULT
    try:
        return int(raw)
    except ValueError:
        print(f"[main] 警告: SESSION_TOKEN_BUDGET={raw!r} 不是整数,按不限处理")
        return SESSION_TOKEN_BUDGET_DEFAULT



def _used_tokens(config: RunnableConfig | None) -> int:
    """从 config 里挂载的 TraceCallbackHandler 汇总本次会话已用 token。

    为什么从 handler 取而不是从 state 取:
        预算检查发生在「发起调用前」,本次调用的 token 还没产生,state 尚未更新;
        handler 记录的是已完成调用的真实用量,是唯一可靠的累计口径。

    为什么不能直接遍历 config["callbacks"]:
        LangGraph / LangChain 在节点内会把 callbacks 规范化为 CallbackManager 对象,
        直接 for 循环会报 TypeError;两种形态都要兼容(单测直接传 list)。
    """
    callbacks = (config or {}).get("callbacks")
    if isinstance(callbacks, list):
        handlers = callbacks
    elif callbacks is not None and hasattr(callbacks, "handlers"):
        # CallbackManager.handlers 包含继承与非继承的全部 handler
        handlers = callbacks.handlers
    else:
        handlers = []
    total = 0
    for handler in handlers:
        if isinstance(handler, TraceCallbackHandler):
            total += handler.total_tokens()
    return total



def _budget_blocked(config: RunnableConfig | None) -> bool:
    """判断是否已触达 token 预算上限(预算 <= 0 视为不限)。"""
    budget = _token_budget()
    if budget <= 0:
        return False
    return _used_tokens(config) >= budget



def _budget_error(config: RunnableConfig | None) -> str:
    """构造「预算超限」的结构化错误分析(标记不可重试,直接结束)。"""
    return (
        "错误类型：预算超限(不可重试)\n"
        f"触发点：LLM 调用前预算检查(SESSION_TOKEN_BUDGET={_token_budget()})\n"
        f"根因推断：本次会话已累计 {_used_tokens(config)} tokens,达到预算上限,不再发起新的 LLM 调用\n"
        "修复方向：调大 SESSION_TOKEN_BUDGET 或新开会话后重试"
    )



def _reset_turn_fields() -> dict:
    """返回每轮入口必须重置的「轮级字段」,保证跨轮不残留、重试额度从 0 开始。

    轮级字段(每轮独立,不应继承)：
        - iteration: 重试计数(新一轮必须从 0 开始,否则很快耗尽 3 次上限);
        - error_analysis: 上一轮的错误(成功轮必须无旧错误干扰路由);
        - final_answer: 上一轮的结论(新一轮应重新生成);
        - exec_result: 上一轮的沙箱执行结果;
        - code: 上一轮生成的代码;
        - plan_approved / plan_reason: 上一轮的审批结果与依据(新一轮必须重新审批);
        - summary / warnings / run_id / chart_files / dataset_fingerprint:
          批次 B 新增的轮级字段,新一轮必须重新生成,不能沿用旧结论。

    跨轮保留字段(不在此处重置,由 checkpointer 继承)：
        - data_sources: discover 登记的数据源清单(第二轮还要用);
        - dataset_name: 已锁定的数据集(支持「那对照组呢」这类指代);
        - messages: 对话历史(add_messages 追加式,不应覆盖)。
    """
    return {
        "iteration": 0,
        "error_analysis": "",
        "final_answer": "",
        "exec_result": {},
        "code": "",
        # 审批结果属于「本轮」结论：新一轮必须重新审批,不能被上一轮的 True 放行
        "plan_approved": False,
        "plan_reason": "",
        # 汇总与运行归档属于「本轮」产物
        "summary": {},
        "warnings": [],
        "run_id": "",
        "chart_files": [],
        "dataset_fingerprint": "",
        # clarify 来源标记属于轮级字段:新一轮必须清空,避免沿用上一轮的交付引导语
        "clarify_from": "",
        # token 用量属于「本轮」成本统计,新一轮从 0 开始累计
        "token_used": 0,
    }



def _extract_question(state: AgentState) -> str:
    """从图状态中提取用户最新的自然语言问题。

    参数：
        state: LangGraph 的完整状态字典,至少包含 question 字段或 messages 列表。

    返回：
        用户最新问题字符串。优先使用显式的 question 字段,
        若无则倒序遍历 messages 找最后一条 HumanMessage 的 content。
        若两者都缺失,返回空字符串。
    """
    question = state.get("question") or ""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            question = msg.content
            break
    return question



def _extract_previous_turn_context(state: AgentState) -> tuple[str, str]:
    """从 messages 历史提取上一轮的 (question, answer),用于跨轮指代消解。

    参数：
        state: LangGraph 的完整状态字典,使用 messages 字段。

    返回：
        (previous_question, previous_answer) 二元组：
        - previous_question: 上一条 HumanMessage.content(单轮时为空串);
        - previous_answer: 最近一条以 [code_analyst] 或 [summarize] 开头的
          AIMessage.content(discover / clarify 轮可能没有有效分析答案,返回空串)。

    为什么同时匹配两个前缀：
        批次 B 起成功分析由 summarize 节点收尾,最终结论文本挂在 [summarize] 消息上;
        只匹配 [code_analyst] 会让下一轮「那对照组呢」丢失上一轮的结论。

    为什么从 messages 而不是 state["final_answer"] 取：
        续跑时 run_agent 已在入口把 final_answer 等轮级字段清零(避免跨轮残留),
        上一轮的「答案」只能从追加式 messages 历史中倒推。messages 由
        add_messages reducer 追加,跨轮完整保留。
    """
    humans: list[str] = []  
    analyst_replies: list[str] = []  
    for msg in state.get("messages", []):   
        if isinstance(msg, HumanMessage):
            humans.append(msg.content)
        elif isinstance(msg, AIMessage) and msg.content.startswith(
            ("[code_analyst]", "[summarize]")
        ):
            # 只关心分析结论消息;planner 的决策消息不含分析结果
            analyst_replies.append(msg.content)
    # 最后一条 HumanMessage 是本轮问题,排除后取上一轮
    previous_question = humans[-2] if len(humans) >= 2 else ""
    previous_answer = analyst_replies[-1] if analyst_replies else ""
    return previous_question, previous_answer



def _extract_dataset_name(
    question: str,
    known_datasets: list[str],
    llm_suggestion: str = "",
    state_dataset: str = "",
) -> str:
    """四级回退链锁定数据集：已登记 > LLM 建议 > 上一轮 state > 正则兜底。

    参数：
        question: 本轮用户问题文本。
        known_datasets: 当前已登记的数据集文件名列表(由 discover 分支填充)。
        llm_suggestion: LLM 在 PlannerDecision 中给出的 dataset_name(可能为空)。
        state_dataset: 上一轮 state 中已锁定的 dataset_name(跨轮继承,可能为空)。

    返回：
        锁定的数据集文件名(可能为空字符串)。优先级从高到低：
        1. known_datasets 中任一文件名出现在 question 中(用户明确提及,最可靠);
        2. llm_suggestion 非空(LLM 根据上下文给出的判断);
        3. state_dataset 非空(跨轮继承,支持「那对照组呢」这类指代);
        4. 正则从 question 提取 <文件名>.<后缀>(数据源尚未登记时的兜底)。
    """
    # 1. 已登记的数据集名出现在问题中 -> 直接锁定(最可靠)
    for name in known_datasets:
        if name and name in question:
            return name
    # 2. LLM 根据上下文给出的建议
    if llm_suggestion:
        return llm_suggestion
    # 3. 上一轮 state 中已锁定的数据集(跨轮继承)
    if state_dataset:
        return state_dataset
    # 4. 正则兜底：允许文件名含空格,但用前后边界校验避免把整句问题当成文件名
    match = _FILENAME_RE.search(question)
    if not match:
        return ""
    name = match.group(1).strip()
    # 剥掉紧贴文件名前的提问引导词(如「分析 我的 数据.csv」→「我的 数据.csv」)
    for verb in _LEAD_VERBS:
        prefix = verb + " "
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
            break
    return name


# 交付类意图关键词:命中任意一个即视为「要上一轮的结果产物」,不重新执行分析。
# 统一用小写比较,兼顾「给我 HTML」这类大小写混写。

DELIVERY_KEYWORDS = (
    "生成网页",
    "做个网页",
    "网页",
    "生成报告",
    "导出报告",
    "下载报告",
    "给我 html",
    "给我html",
    "html",
    "导出",
)

# 归档 run_id 白名单:last_run_id 来自会话索引,拼接路径前仍要做防穿越校验

_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_\-]+$")



def _is_delivery_intent(question: str) -> bool:
    """判断问题是否是「生成网页 / 导出报告」这类交付意图。

    Args:
        question: 用户本轮问题文本。

    Returns:
        True 表示命中 DELIVERY_KEYWORDS 中任一关键词。

    为什么用确定性关键词而不是 LLM:
        交付意图只需要走已有归档,不需要理解数据语义;
        关键词判定更稳定、零 token 成本,也避免 LLM 不可用时退化为 clarify。
    """
    text = (question or "").lower()
    return any(keyword in text for keyword in DELIVERY_KEYWORDS)



def _build_deliver_result(last_run_id: str) -> dict:
    """基于上一轮运行归档生成交付说明,不重新执行任何代码。

    Args:
        last_run_id: 上一轮分析的运行 ID(来自会话索引 last_run_id)。

    Returns:
        部分状态更新字典:
        - 两个产物都存在/部分存在时:next_action="deliver",final_answer 给出
          /api/runs/<id>/report.html 与 /api/runs/<id>/result.csv 的访问路径;
        - 两个文件都不存在(旧运行):next_action="clarify",提示先重新分析。

    为什么直接给接口路径而不是文件路径:
        Web 前端点击即下载;CLI 也能按同一路径规则定位 outputs/runs/<id>/ 下的文件。
    """
    # 只允许白名单形态的 run_id,避免把外部输入拼进路径造成穿越
    if not _RUN_ID_RE.match(last_run_id or ""):
        reply = "上一轮运行 ID 无效,无法定位归档产物,请先重新分析。"
        return {
            "next_action": "clarify",
            "clarify_from": "deliver",
            "final_answer": reply,
            "messages": [AIMessage(content=f"[planner] {reply}")],
        }

    run_dir = RUNS_DIR / last_run_id
    links: list[str] = []
    if (run_dir / "report.html").is_file():
        links.append(f"- HTML 报告: /api/runs/{last_run_id}/report.html")
    if (run_dir / "result.csv").is_file():
        links.append(f"- 结果表 CSV: /api/runs/{last_run_id}/result.csv")

    if not links:
        # 旧运行还没归档 HTML/CSV:如实说明,不重新跑一遍分析
        reply = "该运行没有可导出的报告,请先重新分析。"
        return {
            "next_action": "clarify",
            "clarify_from": "deliver",
            "final_answer": reply,
            "messages": [AIMessage(content=f"[planner] {reply}")],
        }

    reply = (
        "已基于上一轮运行生成,未重新执行代码。\n"
        f"运行 ID: {last_run_id}\n" + "\n".join(links)
    )
    return {
        "next_action": "deliver",
        # 让前端知道复用的是哪一次运行:run_id 为空(本轮无新归档),
        # 单独传 deliver_run_id,避免被渲染成「未完成」
        "deliver_run_id": last_run_id,
        "final_answer": reply,
        "messages": [AIMessage(content=f"[planner] {reply}")],
    }



def _new_run_id() -> str:
    """生成运行 ID:%Y%m%d_%H%M%S + 短 uuid。

    为什么加短 uuid:同一秒内可能跑多次分析,时间戳单独不够唯一,
    追加 6 位 uuid 既保持可读顺序,又避免 runs 目录撞名。
    """
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


