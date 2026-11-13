"""review_plan 节点: HITL 审批暂停与恢复路由。"""


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

__all__ = [
    "_invalid_approval",
    "review_plan_node",
    "route_after_review",
]

def _invalid_approval(root_cause: str) -> dict:
    """构造「审批输入非法」的结构化终止结果(不可重试,直接结束)。

    参数：
        root_cause: 校验失败的具体原因,原样写入「根因推断」。

    返回：
        部分状态更新：plan_approved=False + 结构化 error_analysis + AIMessage。
        路由函数看到 plan_approved=False 即走 end,不会执行任何 AI 代码。
    """
    error_analysis = (
        "错误类型：审批输入非法(不可重试)\n触发点：review_plan 校验恢复值\n"
        f"根因推断：{root_cause}\n"
        '修复方向：resume_agent 的 decision 必须是 '
        '{"action": "approve" | "edit" | "reject", "plan": ["步骤", ...]}'
    )
    return {
        "plan_approved": False,
        "error_analysis": error_analysis,
        "messages": [AIMessage(content=f"[review_plan] {error_analysis}")],
    }



def review_plan_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """计划审批节点(HITL)：用 interrupt 暂停图,等待用户批准 / 编辑 / 取消计划。

    参数：
        state: LangGraph 状态,读取字段：
            - next_action: 只有 analyze 需要审批,discover / clarify 直接放行;
            - question / dataset_name / plan / plan_reason: 组成审批 payload。
        config: 运行配置,读取 configurable.auto_approve：
            - True：跳过人工审批直接放行(批量脚本 / 单测用,保持旧行为);
            - False(默认)：调用 interrupt 暂停图,等待 resume_agent 恢复。

    返回：
        - {"plan_approved": True, "plan": [...]}：批准,或编辑后批准(plan 被覆盖);
        - {"plan_approved": False, "final_answer": "..."}：用户取消;
        - {"plan_approved": False, "error_analysis": "..."}：恢复值非法。

    为什么这个节点只做「暂停 + 校验」,不做任何分析：
        interrupt 恢复时 LangGraph 会从头重新执行本节点,节点必须无副作用;
        代码生成与沙箱执行仍然全部收敛在 code_analyst 中。
    """
    # discover / clarify 没有可审批的分析计划,直接放行,避免无意义打断
    if state.get("next_action") != "analyze":   
        return {"plan_approved": True}  

    configurable = (config or {}).get("configurable", {}) or {}
    if configurable.get("auto_approve"):
        # auto_approve 由 run_agent 通过 config 传入：单测 / 批量脚本免人工等待
        return {"plan_approved": True}

    plan = state.get("plan", []) or []  # 分析计划
    # 暂停图并把计划暴露给调用方;resume_agent 传入的值会成为 interrupt() 的返回值
    decision = interrupt(
        {
            "question": _extract_question(state),    #用户问题
            "dataset_name": state.get("dataset_name", ""),      #数据集文件名
            "plan": plan,    #分析计划
            "reason": state.get("plan_reason", ""),    #判断的理由
        }
    )

    # ---------- 校验恢复值：任何非法输入都必须结构化终止,不能静默放行 ----------
    if not isinstance(decision, dict):
        return _invalid_approval(f"恢复值应为 dict,实际为 {type(decision).__name__}")

    action = str(decision.get("action", "")).strip().lower()

    if action == "approve":
        return {
            "plan_approved": True,
            "messages": [AIMessage(content="[review_plan] 用户批准了分析计划")],
        }

    if action == "edit":
        edited = decision.get("plan")
        if not isinstance(edited, list):
            return _invalid_approval("action=edit 但 plan 不是列表")
        # 逐条去空白并丢掉空步骤,保证计划不会带着空字符串进入代码生成
        steps = [str(step).strip() for step in edited if str(step).strip()]
        if not steps:
            return _invalid_approval("action=edit 但 plan 为空")
        return {
            # 用恢复值里的计划覆盖 state["plan"],后续代码生成以它为准
            "plan": steps,
            "plan_approved": True,
            "messages": [
                AIMessage(
                    content="[review_plan] 用户编辑了分析计划：\n"
                    + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
                )
            ],
        }

    if action == "reject":
        # 取消：不生成代码、不调沙箱,直接把本轮结论写成「已取消」
        return {
            "plan_approved": False,
            "final_answer": "用户取消了本次分析,未执行任何代码。",
            "messages": [
                AIMessage(content="[review_plan] 用户取消了本次分析,未执行任何代码")
            ],
        }

    return _invalid_approval(f"未知 action={action!r},可选 approve / edit / reject")



def route_after_review(state: AgentState) -> str:
    """review_plan 之后的条件路由：批准 → 进入分析;取消 / 非法 → 结束。

    参数：
        state: LangGraph 状态,读取 plan_approved。

    返回：
        "analyze"：批准(含编辑后批准),或 discover / clarify 直接放行,
                   交给 code_analyst 按 next_action 分派;
        "end"：用户取消或审批输入非法,直接到 END,不执行任何代码。
    """
    return "analyze" if state.get("plan_approved") else "end"


