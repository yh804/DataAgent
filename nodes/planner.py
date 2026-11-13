"""planner 节点: LLM 意图识别 + 数据集锁定, 不可用时关键词降级。"""


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

# 领域包加载器：仅 DOMAIN_PACK 启用时才被调用,模块级 import 安全
# (loader 内部用白名单校验,目录不会因包不存在在 import 阶段炸掉)
from domains import loader as domain_loader

# 输出根目录与运行归档目录：所有产物(报告 / 运行归档 / trace)都在 outputs/ 下

from common import *

__all__ = [
    "planner_node",
]

def planner_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """规划节点：LLM 意图识别 + 数据集锁定(含跨轮上下文继承);LLM 失败时降级。

    参数：
        state: LangGraph 的完整状态字典,使用到的字段包括：
            - question: 用户问题字符串(可选);
            - messages: 对话历史(HumanMessage / AIMessage 列表);
            - data_sources: 已登记的数据集文件名列表;
            - dataset_name: 上一轮已锁定的数据集名(跨轮继承的关键)。
        config: 运行配置。透传给 LLM.invoke,使 TraceCallbackHandler 能捕获
            LLM 调用的耗时与 token 用量;为 None 时 LLM 走默认回调上下文。

    返回：
        部分状态更新字典,LangGraph 负责合并回全局状态：
            - next_action: 路由动作(discover / analyze / clarify);
            - dataset_name: 锁定的数据集文件名(可能为空);
            - plan: 分析步骤清单(自然语言);
            - messages: 追加一条 AIMessage,记录规划决定(含降级原因)。

    LangGraph 节点约定：接收完整状态,返回「部分状态更新」字典,
    由 LangGraph 负责合并回全局状态,节点自身不修改入参。
    """
    question = _extract_question(state)     #获取用户问题
    known_datasets = state.get("data_sources", [])     #获取已登记的数据集文件名列表
    # 跨轮上下文快照：在 LLM 调用前提取,供 prompt 与 dataset_name 回退链使用
    previous_question, previous_answer = _extract_previous_turn_context(state)       #获取上一轮和这一轮的问题和回答
    current_dataset = state.get("dataset_name", "")     #获取上一轮锁定的数据集名

    # ---------- 交付类意图:直接复用上一轮归档,不重新执行分析 ----------
    # 放在 LLM 调用之前:该意图不需要理解数据语义,也避免重跑一遍完整分析。
    # 图结构不变,deliver 在 review_plan 直接放行、在 code_analyst 只回显答案。
    if _is_delivery_intent(question):
        last_run_id = str(state.get("last_run_id") or "")
        if not last_run_id:
            reply = (
                "还没有可导出的分析结果。请先完成一次分析"
                "(例如「分析 demo_sales.csv：各区域销售额占比」),"
                "再说「生成网页」或「导出报告」。"
            )
            return {
                "next_action": "clarify",
                "clarify_from": "deliver",
                "final_answer": reply,
                "messages": [AIMessage(content=f"[planner] {reply}")],
            }
        return _build_deliver_result(last_run_id)

    # ---------- 预算守卫：已用 token 达上限就不再发起新的 LLM 调用 ----------
    # 直接以 clarify 收尾,避免 review_plan 的审批分支继续放行并进入代码生成
    if _budget_blocked(config):
        error_analysis = _budget_error(config)
        return {
            "next_action": "clarify",
            "plan_approved": False,
            "error_analysis": error_analysis,
            "final_answer": error_analysis,
            "messages": [AIMessage(content=f"[planner] {error_analysis}")],
        }
    
    # ---------- 主路径：LLM 结构化决策 ----------
    # with_structured_output 用 pydantic 约束返回格式,非法 JSON 会抛错并走降级分支
    # 领域包可选注入:不启用时 domain_block 为空串,build_planner_user_prompt 旧行为逐字节不变
    domain_block = ""
    pack = None
    domain_selected_metrics: list[dict] = []
    if domain_loader.domain_enabled():
        pack = domain_loader.load_domain_pack()
        domain_block = domain_loader.build_planner_block(pack)
        domain_selected_metrics = domain_loader.select_metrics(pack, question)
    try:
        decision: PlannerDecision = (
            get_llm()   #封装好的实例
            # DeepSeek 不支持 json_schema 形式的 response_format,改用 function calling
            .with_structured_output(PlannerDecision, method="function_calling")  # 结构化输出
            .invoke(
                [
                    SystemMessage(content=PLANNER_SYSTEM_PROMPT),   #系统提示词
                    HumanMessage(    #用户问题
                        content=build_planner_user_prompt(   #用户问题提示词
                            question,    #用户问题
                            known_datasets,    #已登记的数据集文件名列表
                            previous_question=previous_question,    #上一轮的问题
                            previous_answer=previous_answer,    #上一轮的回答   
                            current_dataset=current_dataset,    #当前锁定的数据集名
                            domain_block=domain_block,          #领域包块,空串时旧行为逐字节不变
                        )
                    ),
                ],
                # 透传 config：让 TraceCallbackHandler 捕获本次 LLM 调用的耗时与 token
                config=config,
            )
        )
        next_action = decision.intent   #下一步环节
        plan = decision.plan        #步骤清单
        reason = decision.reason    #判断的理由
        # 四级回退链：已登记 > LLM > state > 正则
        dataset_name = _extract_dataset_name(
            question,    #用户问题
            known_datasets,    #已登记的数据集文件名列表
            llm_suggestion=decision.dataset_name,    #LLM 给出的建议
            state_dataset=current_dataset,    #当前锁定的数据集名
        )
        decision_text = f"[planner] 下一步动作: {next_action}({reason})"
    except Exception as exc:
        # ---------- 降级路径：LLM 不可用 / 返回非法,退回关键词规则 ----------
        # 为什么降级而不是报错：意图识别属于「可增强但非必需」的能力,
        # 关键词规则足以保证最小闭环可用;降级原因写入消息便于审计
        if any(kw in question for kw in ("有哪些", "什么数据", "数据源", "列表")):
            next_action = "discover"
        elif any(kw in question for kw in ("统计", "分析", "对比", "趋势", "占比", "均值", "分布")):
            next_action = "analyze"
        else:
            # 意图不明时反问用户,而不是盲猜执行(避免不可复现的结论)
            next_action = "clarify"
        plan = []
        # 降级路径没有 LLM 给出的依据,置空避免引用未定义变量
        reason = ""
        # 降级路径也走完整回退链(无 LLM 建议,靠 state 与正则)
        dataset_name = _extract_dataset_name(   
            question,    #用户问题
            known_datasets,    #已登记的数据集文件名列表
            state_dataset=current_dataset,    #当前锁定的数据集名
        )
        decision_text = (
            f"[planner] LLM 不可用,已降级为规则路由({type(exc).__name__})。"
            f"下一步动作: {next_action}"
        )

    if dataset_name:
        decision_text += f",数据集: {dataset_name}"

    # 向对话历史追加一条 assistant 消息,记录本次规划决定(可观测、可回放)
    return {
        # 路由动作,供LangGraph条件边判断下一个要执行的节点：discover / analyze / clarify
        "next_action": next_action,
        # 经过四级回退链校正后的数据集文件名(可能为空字符串)
        "dataset_name": dataset_name,
        # LLM输出的分析步骤清单,传给后续代码生成节点作为参考
        "plan": plan,
        # 计划依据：随 plan 一起进入审批 payload,帮助用户判断是否放行
        "plan_reason": reason,
        # 领域包状态：None=未启用,非 None=已加载;不启用时旧行为逐字节不变
        "domain_pack": pack,
        # 领域包选中的指标：供 code_analyst 和 summarize 直接用,避免重复打分
        "domain_selected_metrics": domain_selected_metrics,
        # 追加对话消息：把本次planner的决策说明加入对话历史,messages有add_messages自动追加reducer
        "messages": [AIMessage(content=decision_text)],
    }


