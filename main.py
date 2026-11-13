"""数据分析 Agent 入口: 组装 LangGraph、运行/恢复、进度回调与 CLI 冒烟。"""


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

from common import *
from archive import *
from nodes.planner import *
from nodes.review import *
from nodes.code_analyst import *
from nodes.summarize import *

def build_graph():
    """组装 LangGraph StateGraph(4 个节点):START -> planner -> review_plan -> code_analyst -> summarize。

    图结构：
        START → planner → review_plan → code_analyst → (条件路由)
                              │              ├─ summarize → summarize → END(成功)
                              │              ├─ retry     → code_analyst(自环,最多 3 次)
                              │              └─ end       → END
                              └─ reject / 审批非法 → END

    summarize 节点：
        只在 analyze 成功时进入,负责 sanity check、LLM 汇总(SUMMARY_ENABLED 控制)、
        运行归档(outputs/runs/<run_id>/)与 final_answer 渲染;节点本身始终存在。

    checkpointer：
        由 checkpoint.build_checkpointer() 装配,默认 SQLite
        (outputs/checkpoints.sqlite,CHECKPOINT_PATH 可覆盖),支持同 thread_id 续跑 /
        interrupt 恢复,以及进程退出后的跨进程恢复;缺包或无写权限时降级 MemorySaver。

    返回：
        已编译的可执行图对象,可调用 .invoke(init_state, config=...) 启动。
    """
    graph = StateGraph(AgentState)
    graph.add_node("planner", planner_node)
    graph.add_node("review_plan", review_plan_node)
    graph.add_node("code_analyst", code_analyst_node)
    graph.add_node("summarize", summarize_node)
    # 入口 → 规划 → 人工审批 → 分析(固定边 + 审批后条件边)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "review_plan")
    # 审批结果决定是否进入分析：reject / 非法输入直接结束,不执行代码
    graph.add_conditional_edges(
        "review_plan",
        route_after_review,
        {"analyze": "code_analyst", "end": END},
    )
    # 分析后按执行结果路由：成功进入汇总,失败自环重试(≤3 次),其余直接结束
    graph.add_conditional_edges(
        "code_analyst",
        route_after_code_analyst,
        {"retry": "code_analyst", "summarize": "summarize", "end": END},
    )
    # 汇总完成后结束本轮(汇总节点自身负责兜底,不会把 final_answer 留空)
    graph.add_edge("summarize", END)
    # checkpointer 装配(默认 SQLite,可降级 memory;规则 2.3 要求持久化)
    checkpointer = build_checkpointer()
    return graph.compile(checkpointer=checkpointer)


# 进程级图缓存：checkpointer 挂在编译后的图上,若每次 run_agent 都重新 build_graph,
# 同一 thread_id 的检查点会随旧图一起被丢弃,续跑 / 恢复将失效。
# 因此用惰性单例保证整个进程共享同一张图(同一个 checkpointer)。
_COMPILED_GRAPH = None


def _get_graph():
    """返回进程级共享的已编译图(首次调用时惰性构建)。

    返回:
        整个进程唯一的 CompiledGraph;之后的每次调用都返回同一对象。

    为什么必须共享同一张图:
        checkpointer 挂在编译后的图上,续跑 / 恢复要靠同一个 checkpointer 按
        thread_id 找回历史状态。若每次调用都重新 build_graph,旧检查点会随旧图
        一起被丢弃,「继续同一会话」会静默失效。

    为什么不加额外锁:
        当前约定单 worker、图执行串行(web 层用 Semaphore(1) 排队),首次构建只会
        在第一个请求上发生;若未来放开并发,需要补锁或在启动时预热这张图。
    """
    global _COMPILED_GRAPH  # 模块级变量:跨调用保持,构成进程内惰性单例
    if _COMPILED_GRAPH is None:
        # 首次调用才真正编译图:避免 import 阶段就去建 checkpointer / 读环境变量
        _COMPILED_GRAPH = build_graph()
    return _COMPILED_GRAPH


# 需要向前端推送进度的业务节点白名单:过滤 LangGraph / RunnableSequence 等包装层
_PROGRESS_NODES = ("planner", "review_plan", "code_analyst", "summarize")


class ProgressCallbackHandler(BaseCallbackHandler):
    """把业务节点的开始 / 结束实时推给 progress_callback(Web SSE 进度用,批次 D)。

    为什么只报白名单节点:
        on_chain_start 会收到 LangGraph / RunnableSequence 等包装层事件,
        前端只关心 4 个业务节点,其余一律过滤,避免噪音。
    为什么吞掉回调异常:
        推送失败(如客户端已断开)绝不能反过来打断图执行。

    线程安全:
        图在线程池里执行,回调运行在 worker 线程;推送方(web.events.EventBridge)
        内部用 loop.call_soon_threadsafe 把事件交回事件循环,本类不做线程假设。
    """

    def __init__(self, callback: Callable[[dict], None]) -> None:
        super().__init__()
        self._callback = callback
        # on_chain_end 拿不到节点名,按 run_id 记住 on_chain_start 时登记的名字
        self._names: dict[str, str] = {}

    def _emit(self, name: str, phase: str) -> None:
        if name not in _PROGRESS_NODES:
            return
        try:
            self._callback({"node": name, "phase": phase})
        except Exception:  # noqa: BLE001 - 推送通道异常绝不能影响图执行
            pass

    def on_chain_start(
        self,
        serialized: dict | None,
        inputs: dict,
        *,
        run_id,
        parent_run_id=None,
        **kwargs,
    ) -> None:
        # LangGraph 把节点名放在 kwargs["name"],serialized["name"] 通常为空;
        # 与 trace._extract_name 同一优先级,兼容不同 LangChain 版本
        name = kwargs.get("name") or (serialized or {}).get("name") or ""
        self._names[str(run_id)] = name
        self._emit(name, "start")

    def on_chain_end(self, outputs, *, run_id, **kwargs) -> None:
        name = self._names.pop(str(run_id), "")
        self._emit(name, "end")


def _invoke_with_trace(
    init_state: dict,
    config: RunnableConfig,
    thread_id: str,
    question: str,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    """执行图并按需生成 trace;是 run_agent / resume_agent 的统一执行入口。

    参数：
        init_state: 传给 graph.invoke 的初始状态。
        config: 运行配置(含 configurable.thread_id / auto_approve)。
        thread_id: 会话标识,用于 trace 文件名与内容。
        question: 本轮问题(resume 时可能为空串)。
        progress_callback: 可选的节点进度回调(批次 D,Web SSE 用);
            签名 fn({"node": name, "phase": "start"|"end"}),在 worker 线程被调用。

    返回：
        图执行后的最终状态字典。

    副作用：
        - TRACE_ENABLED=true 时创建 TraceCallbackHandler 并在执行后写 trace 文件;
        - 把 trace 文件路径写入模块级 LAST_TRACE_PATH,供 chat.py / eval 读取。
    trace 写失败只打印警告,绝不阻断主流程。
    """
    global LAST_TRACE_PATH
    LAST_TRACE_PATH = ""

    # 合并调用方已注入的 callbacks:旧实现直接覆盖,会把外层(Web)注入的
    # 进度回调静默丢掉——这里必须保留它们再追加自己的 handler
    existing = config.get("callbacks") or []
    if not isinstance(existing, list):
        existing = [existing]
    callbacks: list = [*existing]
    if progress_callback is not None:
        callbacks.append(ProgressCallbackHandler(progress_callback))

    if not trace_enabled():
        if callbacks:
            config = {**config, "callbacks": callbacks}
        return _get_graph().invoke(init_state, config=config)

    handler = TraceCallbackHandler()
    # 浅拷贝 config 并注入 callbacks,避免污染调用方传入的原始 config
    config = {**config, "callbacks": [*callbacks, handler]}
    started_at = datetime.now()
    final_state = _get_graph().invoke(init_state, config=config)
    trace_path = write_trace(handler, thread_id, question, started_at)
    LAST_TRACE_PATH = trace_path or ""
    return final_state


def get_pending_interrupt(state: dict) -> dict | None:
    """从图返回的状态中取出待审批的 interrupt 载荷。

    参数：
        state: run_agent / resume_agent 返回的完整状态字典。

    返回：
        暂停在 review_plan 时返回审批 payload(question / dataset_name / plan / reason);
        图已执行结束时返回 None。调用方据此区分「已结束」与「等待审批」。

    为什么不用 (state, pending) 二元组：
        保持 run_agent 的返回值仍是单一 state 字典(向后兼容既有调用方),
        暂停信息由 LangGraph 原生挂在 state["__interrupt__"] 上,这里只做安全解包。
    """
    interrupts = state.get("__interrupt__") or ()
    if not interrupts:
        return None
    first = interrupts[0]
    # LangGraph 的 Interrupt 对象带 .value;兼容 dict 形态的旧版本 / 序列化结果
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value")
    return value if isinstance(value, dict) else {}


def load_pending_interrupt(thread_id: str) -> dict | None:
    """从 SQLite 检查点读取指定会话的待审批项(跨进程恢复入口)。

    参数：
        thread_id: run_agent 启动时使用的会话标识。

    返回：
        暂停在 review_plan 时返回审批 payload(question / dataset_name / plan / reason);
        该会话没有检查点、已执行结束、或从未暂停时返回 None。

    为什么单独提供这个函数：
        跨进程恢复时,新进程手里只有 thread_id,没有 run_agent 返回的 state,
        必须先回读检查点拿到计划再让用户确认,避免盲批。

    异常：
        ValueError: thread_id 为空(调用方编程错误,无法定位检查点)。
    """
    if not thread_id:
        raise ValueError("load_pending_interrupt 需要 thread_id：请传入会话标识")

    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    # get_state 只读最新检查点,不会推进图执行
    snapshot = _get_graph().get_state(config)
    if snapshot is None:
        return None
    # 待审批信息挂在 tasks[*].interrupts[*].value 上;遍历而不是只看第一个,
    # 兼容并行任务 / 多个中断的版本差异
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return value
    return None


def run_agent(
    question: str,
    thread_id: str = "",
    continue_session: bool = False,
    auto_approve: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
    last_run_id: str = "",
) -> dict:
    """初始化状态并启动图,返回最终状态(可能暂停在人工审批)。

    参数：
        question: 本轮用户的自然语言问题,如 "帮我看看 processed 目录下有哪些文件"。
        thread_id: 会话线程标识(可选)。留空则自动生成 uuid4(独立新会话);
                   传入相同 thread_id 可关联到同一检查点(配合 continue_session)。
        continue_session: 是否「续跑」已有会话：
            - False(默认)：完整初始化(含 data_sources=[]),覆盖旧检查点,
                             行为等同开新会话;
            - True：只传本轮新内容 + 轮级字段重置,data_sources / dataset_name
                    由 checkpointer 旧值继承,支持多轮追问。
        auto_approve: 是否跳过人工审批：
            - False(默认)：analyze 意图会在 review_plan 暂停,需 resume_agent 恢复;
            - True：review_plan 直接放行,行为与未引入审批时一致
                    (供 __main__ 单测与批量脚本使用)。
            注：discover / clarify 意图没有分析计划,任何取值都不暂停。
        progress_callback: 可选的节点进度回调(批次 D,Web SSE 用),签名见
            _invoke_with_trace;默认 None,CLI / eval 行为完全不变。
        last_run_id: 上一轮分析的运行 ID(可选)。Web 层从会话索引读取后注入,
            供 planner 识别「生成网页 / 导出报告」时直接复用归档产物;
            默认空串表示没有可复用的上一轮运行(CLI / 单测保持旧行为)。

    返回：
        图当前状态的完整字典。如何区分「已结束」与「暂停等待审批」：
            - get_pending_interrupt(state) 返回 None → 本轮已结束(成功 / 失败 / 取消);
            - get_pending_interrupt(state) 返回 payload → 暂停在 review_plan,
              必须调用 resume_agent(thread_id, decision) 继续。
        未结束时 state 通常包含：
            - messages: 完整对话历史(每条消息对应一个节点的输出);
            - data_sources: discover 分支登记的数据集列表;
            - dataset_name: analyze 分支锁定的数据集名;
            - plan / plan_approved: 分析计划与审批结果;
            - code / exec_result: analyze 分支 LLM 生成的代码与沙箱执行结果;
            - iteration: analyze 分支的尝试次数(含重试,成功通常为 1);
            - final_answer: analyze 成功时的结论文本(失败 / 取消时另有约定);
            - error_analysis: 任何失败路径的结构化错误分析(成功时为空)。

    持久化：
        checkpointer 由 checkpoint.build_checkpointer() 装配,默认 SQLite
        (outputs/checkpoints.sqlite,CHECKPOINT_PATH 可覆盖),检查点落盘后进程退出不丢;
        暂停的审批可在新进程通过 load_pending_interrupt(thread_id) 回读,
        再用 resume_agent 恢复(chat.py --resume 即此路径)。

    用法：
        单轮(独立新会话,免审批)：
            run_agent("统计一下 orders.csv 的行数", auto_approve=True)
        单轮(需要人工审批)：必须显式传 thread_id,否则拿不到可恢复的检查点：
            tid = uuid.uuid4().hex
            state = run_agent("测试数据.csv 的收入分布", thread_id=tid)
            pending = get_pending_interrupt(state)
            if pending:
                state = resume_agent(tid, {"action": "approve", "plan": pending["plan"]})
        多轮(同一 thread_id 续跑)：
            tid = uuid.uuid4().hex
            run_agent("测试数据.csv 的收入均值", thread_id=tid, auto_approve=True)
            run_agent("那对照组呢", thread_id=tid, continue_session=True, auto_approve=True)
    """
    if not thread_id:
        # 全新会话：随机 thread_id 保证不与其他会话冲突
        thread_id = uuid.uuid4().hex
        init_state: AgentState = {
            "question": question,
            "messages": [HumanMessage(content=question)],
            # 「先发现,后读取」：初始数据源为空,由 discover 分支填充
            "data_sources": [],
            # 上一轮运行 ID 只用于交付类意图,与本轮 run_id 互不影响
            "last_run_id": last_run_id,
            **_reset_turn_fields(),
        }
    elif continue_session:
        # 续跑：只传本轮新内容 + 轮级字段重置,data_sources / dataset_name
        # 由 checkpointer 旧值继承(不要在 init_state 里写它们,否则会被覆盖)
        init_state = {
            "question": question,
            "messages": [HumanMessage(content=question)],
            # 续跑时 last_run_id 由调用方(Web 层)按会话索引传入,不能从旧 state 猜
            "last_run_id": last_run_id,
            **_reset_turn_fields(),
        }
    else:
        # 指定 thread_id 的新会话：完整初始化,覆盖旧检查点中的所有字段
        init_state = {
            "question": question,
            "messages": [HumanMessage(content=question)],
            "data_sources": [],
            "last_run_id": last_run_id,
            **_reset_turn_fields(),
        }

    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            # 通过 config 传递审批开关：节点在 interrupt 前读取,避免污染 state
            "auto_approve": auto_approve,
        }
    }
    final_state: dict = _invoke_with_trace(
        init_state, config, thread_id, question, progress_callback=progress_callback
    )
    return final_state


def resume_agent(
    thread_id: str,
    decision: dict,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    """恢复暂停在 review_plan 的会话,用 decision 作为 interrupt 的恢复值。

    参数：
        thread_id: run_agent 返回时使用的会话标识(必须非空,且图确实处于暂停态)。
        decision: 审批决定,格式为 {"action": "approve" | "edit" | "reject", "plan": [str]}：
            - approve: 按原计划继续;
            - edit: 用 decision["plan"] 覆盖 state["plan"] 后继续;
            - reject: 直接结束,不生成 / 执行任何代码。
        progress_callback: 可选的节点进度回调(批次 D,Web SSE 用),默认 None。

    返回：
        恢复执行后的完整状态字典,约定与 run_agent 相同：
        get_pending_interrupt 返回 None 表示已结束。
        若 decision 非法,review_plan 会写入结构化 error_analysis 并直接结束
        (不抛异常、不静默放行)。

    异常：
        ValueError: thread_id 为空(属于调用方编程错误,无法定位检查点)。
    """
    if not thread_id:
        raise ValueError("resume_agent 需要 thread_id：请传入 run_agent 返回时所用会话标识")

    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            # 恢复阶段不再自动批准：后续节点不再经过 review_plan,
            # 这里显式置 False 以免语义混淆
            "auto_approve": False,
        }
    }
    # Command(resume=...) 把 decision 送回 interrupt() 调用处,
    # 从暂停点继续执行 review_plan 及其后续节点;resume 没有原始 question,传空串
    final_state: dict = _invoke_with_trace(
        Command(resume=decision), config, thread_id, "", progress_callback=progress_callback
    )
    return final_state


# 当脚本作为主程序直接运行时,执行端到端测试用例
if __name__ == "__main__":
    # 本文件是单轮冒烟测试：auto_approve=True 跳过人工审批,保持旧行为可直接跑完
    # 用例 1：数据发现路径(不依赖 LLM 与 Docker,任何环境都应跑通)
    final_state = run_agent("帮我看看 processed 目录下有哪些文件", auto_approve=True)
    print("=" * 50)
    # 逐条打印对话轨迹,便于观察每个节点写入的消息
    for msg in final_state["messages"]:
        print(f"[{type(msg).__name__}] {msg.content}")
    print("=" * 50)
    print("最终数据源:", final_state.get("data_sources"))

    # 用例 2：分析路径(LLM 或沙箱未配置时会结构化报错,不会崩溃)
    datasets = final_state.get("data_sources", [])
    if datasets:
        analyze_state = run_agent(f"统计一下 {datasets[0]} 的行数", auto_approve=True)
        print("=" * 50)
        for msg in analyze_state["messages"]:
            print(f"[{type(msg).__name__}] {msg.content}")
        print("=" * 50)
        print("final_answer:", analyze_state.get("final_answer", "(无)"))
        print("error_analysis:", analyze_state.get("error_analysis", "(无)"))
        print("iteration:", analyze_state.get("iteration", "(无)"))
