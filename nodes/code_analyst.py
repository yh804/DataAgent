"""code_analyst 节点: LLM 生成 pandas 代码, 沙箱执行, 失败重试。"""


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
    "_describe_dataset_error",
    "code_analyst_node",
    "route_after_code_analyst",
]

def _describe_dataset_error(dataset_name: str, exc: Exception) -> str:
    """把 describe_dataset 的异常归因为结构化错误(全部标记「不可重试」)。

    为什么按类型细分根因：
        统一写「数据集不可用」会让用户无法判断到底是文件没放对、编码不对,
        还是格式不支持;细分后错误信息可以直接指导下一步修复。

    判定顺序说明：
        UnicodeDecodeError 是 ValueError 的子类,必须先于 ValueError 判断,
        否则编码错误会被误归为「格式不支持」。
    """
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, FileNotFoundError):
        root = "文件不存在或未登记"
        fix = "确认文件已放入当前数据目录,且文件名拼写与问题中一致后重试"
    elif isinstance(exc, UnicodeDecodeError) or "codec" in str(exc).lower():
        root = "文件编码无法解析,请检查文件编码"
        fix = "确认文件编码后转存为 UTF-8(或 GB18030)再重试"
    elif isinstance(exc, ValueError):
        root = "文件格式不支持(当前仅支持 CSV)"
        fix = "确认目标文件为 .csv 格式后重试"
    else:
        root = "数据集读取失败"
        fix = "检查文件是否损坏、分隔符是否规范后重试"

    return (
        "错误类型：数据集不可用(不可重试)\n"
        f"触发点：describe_dataset({dataset_name})\n"
        f"根因推断：{root}。{detail}\n"
        f"修复方向：{fix}"
    )



def code_analyst_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """代码分析节点：discover 登记数据源;analyze 由 LLM 生成代码并沙箱执行。

    参数：
        state: LangGraph 的完整状态字典,使用到的字段包括：
            - next_action: 由 planner 决定的路由动作(deliver / discover / analyze / clarify);
            - question: 用户问题字符串(analyze 分支需要,用于组装代码生成 prompt);
            - messages: 对话历史(同 question 兜底);
            - dataset_name: 当前锁定的数据集文件名(analyze 分支必须有);
            - data_sources: 已登记的数据集列表(discover 分支写入)。
        config: 运行配置。透传给 LLM.invoke 与工具 .invoke,使 TraceCallbackHandler
            能捕获 LLM 与工具调用的耗时;为 None 时走默认回调上下文。

    返回：
        部分状态更新字典,按 next_action 分支不同：
            deliver:    原样回显 planner 已组装好的归档访问说明(不执行代码、不产生新 run);
            discover:   data_sources 新值 + AIMessage;
            analyze 成功: code + exec_result + final_answer + AIMessage;
            analyze 失败: code + exec_result + error_analysis + AIMessage;
            analyze 数据集不可用 / 代码生成失败: error_analysis + AIMessage;
            clarify:    只有 AIMessage,引导用户补充。
    """
    action = state.get("next_action", "clarify")

    if action == "deliver":
        # 交付分支:planner 已把归档访问路径写进 final_answer,这里原样回显。
        # 不调用 LLM、不写 run_id、不执行任何代码,因此不会产生新的运行记录。
        return {"final_answer": state.get("final_answer") or "已基于上一轮运行生成,未重新执行代码。"}

    if action == "clarify" and state.get("clarify_from") == "deliver":
        # 交付类引导语(含归档路径或「请先重新分析」)由 planner 组装,
        # 不能落到下面的通用 clarify 文案,否则用户看不到真正的原因。
        return {
            "final_answer": state.get("final_answer") or "该运行没有可导出的报告,请先重新分析。"
        }

    if action == "discover":
        # 「先发现,后读取」：发现阶段只调用白名单发现工具,不执行任何 AI 生成代码
        # 用 .invoke({}, config=config) 而非直接调用,触发 on_tool_start/end 回调
        datasets = list_datasets.invoke({}, config=config)
        reply = (
            f"[code_analyst] 已登记 {len(datasets)} 个数据集: {datasets}"
            if datasets
            else "[code_analyst] data/processed/ 目录下暂无 .csv 数据集"
        )
        # 同时写入 final_answer：让调用方(含回归脚本)可以统一从 final_answer 取结论,
        # 而不必区分 discover / analyze 两种分支
        return {
            "data_sources": datasets,
            "final_answer": reply,
            "messages": [AIMessage(content=reply)],
        }

    if action == "analyze":
        question = _extract_question(state)
        dataset_name = state.get("dataset_name", "")
        # 每次进入 analyze 分支即计一次尝试(含首次与每次重试),供硬性收敛边界判断
        iteration = state.get("iteration", 0) + 1

        # 未锁定数据集时先引导发现,避免盲读文件(先发现,后读取)
        # 这属于「不可重试」错误：路由函数识别「不可重试」标记后直接 end,不做无意义重试
        if not dataset_name:
            return {
                "iteration": iteration,
                "error_analysis": (
                    "错误类型：未锁定数据集(不可重试)\n触发点：analyze 分支入口\n"
                    "根因推断：用户问题中没有可识别的数据集文件名\n"
                    "修复方向：请在问题中明确指出要分析的数据集文件名"
                ),
                "messages": [
                    AIMessage(content="[code_analyst] 尚未锁定数据集,请先指定要分析的文件名")
                ],
            }

        # ---------- 1. 取 schema 与抽样：只把摘要交给 LLM,全量数据不进上下文 ----------
        # 每次尝试(含第 2/3 次重试)都重新调用 describe_dataset,而不是复用上一轮的
        # state["dataset_schema"]:文件可能在两轮之间被替换(如上游补上表头),
        # 旧 schema 会让重试永远撞同一个 KeyError。失败分支会一并回写最新 schema。
        try:
            # 用 .invoke 触发 on_tool 回调;异常(FileNotFoundError)原样抛出,由 except 捕获
            schema_info = describe_dataset.invoke({"name": dataset_name}, config=config)
        except Exception as exc:
            # 按异常类型细分根因(文件不存在 / 编码错误 / 格式不支持 / 读取失败),
            # 全部标记「不可重试」：重试不会改变文件本身的属性
            error_analysis = _describe_dataset_error(dataset_name, exc)
            return {
                "iteration": iteration,
                "error_analysis": error_analysis,
                "messages": [AIMessage(content=f"[code_analyst] {error_analysis}")],
            }

        # ---------- 1.5 空数据集：只有表头 / 0 行时直接终止,不浪费 LLM 生成代码 ----------
        if not schema_info.get("columns") or schema_info.get("row_count", 0) == 0:
            error_analysis = (
                "错误类型：数据集为空(不可重试)\n"
                f"触发点：describe_dataset({dataset_name}) 返回 0 列或 0 行\n"
                "根因推断：文件只有表头或没有任何数据行,无法进行统计分析\n"
                "修复方向：确认文件包含数据行后重新提问"
            )
            return {
                "iteration": iteration,
                "error_analysis": error_analysis,
                "messages": [AIMessage(content=f"[code_analyst] {error_analysis}")],
            }

        # ---------- 2. LLM 动态生成 pandas 分析代码(带着上次报错重写,不是盲猜) ----------
        # 预算守卫：超预算不再发起调用,标记不可重试让路由直接 end
        if _budget_blocked(config):
            error_analysis = _budget_error(config)
            return {
                "iteration": iteration,
                "error_analysis": error_analysis,
                "messages": [AIMessage(content=f"[code_analyst] {error_analysis}")],
            }

        # 领域包可选注入:不启用或 pack 未加载时 metric_block 为空串,旧行为逐字节不变
        pack = state.get("domain_pack") or None
        metric_block = ""
        if pack is not None:
            domain_loader = None
            try:
                from domains import loader as domain_loader  # noqa: PLC0415 - 模块级 import 已存在
            except ImportError:
                domain_loader = None
            if domain_loader is not None:
                metric_block = domain_loader.build_metric_block(
                    pack, question, state.get("plan", [])
                )
        try:
            generated: GeneratedCode = (
                get_llm()
                # DeepSeek 不支持 json_schema 形式的 response_format,改用 function calling
                .with_structured_output(GeneratedCode, method="function_calling")
                .invoke(
                    [
                        SystemMessage(
                            content=build_codegen_system_prompt(
                                question,
                                schema_info,
                                state.get("plan", []),
                            )
                        ),
                        HumanMessage(
                            content=build_codegen_user_prompt(
                                question,
                                dataset_name,
                                schema_info,
                                # 重试时把上一轮的根因分析回喂,首次为空不影响
                                previous_error=state.get("error_analysis", ""),
                                # 经人工确认(可能被编辑)的计划,作为代码生成的强约束
                                plan=state.get("plan", []),
                                # 领域包指标口径块,空串时旧行为逐字节不变
                                metric_block=metric_block,
                            )
                        ),
                    ],
                    # 透传 config：捕获本次代码生成 LLM 调用的耗时与 token
                    config=config,
                )
            )
            code = generated.code
            explanation = generated.explanation
        except Exception as exc:
            # 代码生成是核心能力,无法降级为固定模板(否则会退化成模板执行器),
            # 因此如实上报失败原因,把现场留给用户排查 LLM 配置
            error_analysis = (
                f"错误类型：代码生成失败(不可重试)\n触发点：LLM with_structured_output 调用\n"
                f"根因推断：{type(exc).__name__}: {exc}\n"
                f"修复方向：检查 .env 中 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 配置"
            )
            return {
                "iteration": iteration,
                "error_analysis": error_analysis,
                "messages": [AIMessage(content=f"[code_analyst] {error_analysis}")],
            }

        # ---------- 3. 沙箱执行：AI 生成代码的唯一执行入口 ----------
        # 用 .invoke 触发 on_tool 回调,便于 trace 记录 execute_code 耗时
        result = execute_code.invoke({"code": code}, config=config)
        if result["exit_status"] != "success":
            # 失败：写入结构化错误分析(错误类型/触发点/根因推断/修复方向),供条件边
            # 判断重试;final_answer 保持为空,禁止编造结果
            stderr_text = (result.get("stderr") or "").strip()
            if any(k in stderr_text.lower() for k in DOCKER_UNAVAILABLE_KEYWORDS):
                # Docker 未启动 / 守护进程不可用：重试不会改变结果,标记不可重试节省 3 次空耗
                error_analysis = (
                    "错误类型：沙箱不可用(不可重试)\n"
                    "触发点：execute_code 启动 Docker 容器\n"
                    f"根因推断：{stderr_text}\n"
                    "修复方向：启动 Docker Desktop 后重试(docker info 可用即可)"
                )
            else:
                # 代码层面的可修复错误：保留「第 N/3 次失败」驱动反思式重试
                error_analysis = (
                    f"第 {iteration}/3 次失败。\n"
                    f"错误类型：沙箱执行失败\n触发点：execute_code 执行 LLM 生成的 pandas 代码\n"
                    f"根因推断：{stderr_text}\n"
                    f"修复方向：对照列结构修正列名 / 类型 / 空值处理(代码思路：{explanation})"
                )
            if iteration >= 3 and "不可重试" not in error_analysis:
                # 三点收敛:第三次仍失败时不再甩一条生硬的错误,
                # 转 clarify 说明最可能的根因,并把失败摘要留在 warnings 里可追溯。
                message = (
                    "文件列结构与 schema 不一致,可能是无表头文件或表头被数据行顶掉,"
                    "请确认后再试。"
                )
                warnings = list(state.get("warnings") or [])
                warnings.append(f"连续 3 次执行失败:{stderr_text[:300]}")
                return {
                    "code": code,
                    "exec_result": result,
                    "dataset_schema": schema_info,
                    "iteration": iteration,
                    "next_action": "clarify",
                    "final_answer": message,
                    # 清空 error_analysis:让前端按 clarify 渲染,而不是 error 卡片
                    "error_analysis": "",
                    "warnings": warnings,
                    "messages": [AIMessage(content=f"[code_analyst] {message}")],
                }
            return {
                "code": code,
                "exec_result": result,
                # 失败分支也回写最新 schema:下一轮基于「当前文件」重试,不用旧列结构
                "dataset_schema": schema_info,
                "iteration": iteration,
                "error_analysis": error_analysis,
                "messages": [AIMessage(content=f"[code_analyst] {error_analysis}")],
            }

        # 成功：结论 = LLM 思路说明 + 沙箱 stdout,写入 final_answer
        # 图表以文件路径引用拼进结论(不把图片本体塞进上下文)
        charts = result.get("charts", [])
        charts_text = ""
        if charts:
            # charts 是相对 outputs/ 的路径,拼上目录前缀让用户可直接定位文件
            charts_text = "\n\n图表文件：\n" + "\n".join(
                f"- outputs/{c}" for c in charts
            )
        final_answer = f"{explanation}\n\n{result['stdout']}{charts_text}"
        reply = (
            f"[code_analyst] 执行成功({result['duration_ms']}ms,第 {iteration} 次尝试)。\n"
            f"分析思路：{explanation}\n结果：\n{result['stdout']}{charts_text}"
        )

        # ---------- 4. 运行标识与数据版本：真正的归档移到 summarize 节点 ----------
        # 这里只准备「可复现所需的最小上下文」(schema + 指纹 + run_id),
        # result.json / report.md / 图表归档统一由 summarize 落盘,
        # 避免分析节点与汇总节点各写一半导致口径分裂
        run_id = _new_run_id()
        dataset_fingerprint = fingerprint_text(schema_info)

        return {
            "code": code,
            "exec_result": result,
            "iteration": iteration,
            # 先给一份旧格式答案兜底;summarize 成功后会用报告正文覆盖
            "final_answer": final_answer,
            "dataset_schema": schema_info,
            "dataset_fingerprint": dataset_fingerprint,
            "run_id": run_id,
            # 成功必须显式清空上一轮遗留的旧错误,否则最终状态仍带历史 error_analysis
            "error_analysis": "",
            "messages": [AIMessage(content=reply)],
        }

    # clarify：意图不明,交还用户(HITL),不硬扛。
    # 为什么要把话写全：这一轮没有执行结果,回答本身就是唯一产出;
    # 只回一句「意图不明确」,前端只能渲染成一张空卡片,用户不知道下一步该做什么。
    try:
        available = list_datasets.invoke({}, config=config)
    except Exception:  # noqa: BLE001 - 列目录失败也要把「缺什么」讲清楚
        available = []
    lines = ["这一轮意图不明确，需要你补充一点信息，我没有执行任何代码。"]
    steps = state.get("plan") or []
    if steps:
        lines.append("")
        lines.append("我对问题的理解是：")
        lines.extend(f"- {step}" for step in steps)
    lines.append("")
    if available:
        lines.append(f"当前可用的数据集：{'、'.join(available)}")
        lines.append(
            "你可以直接说「分析 <文件名>：<想看什么>」，"
            "例如「分析 demo_sales.csv：各区域销售额占比」。"
        )
    else:
        lines.append("当前还没有已登记的数据集，请先在输入框点 📎 上传 CSV，再说想分析什么。")
    reply = "\n".join(lines)
    return {
        "next_action": "clarify",
        "final_answer": reply,
        "messages": [AIMessage(content=reply)],
    }



def route_after_code_analyst(state: AgentState) -> str:
    """code_analyst 执行后的条件路由：决定「汇总」「重试」还是「结束」。

    参数：
        state: LangGraph 的完整状态字典,读取字段：
            - next_action: 本次动作类型;
            - exec_result: 沙箱执行结果(含 exit_status);
            - error_analysis: 结构化错误分析(含「不可重试」标记);
            - iteration: 已尝试次数(硬性收敛边界用)。

    返回：
        "summarize"：analyze 分支执行成功,交给汇总节点出报告;
        "retry"：analyze 分支执行失败且未超上限,自环回 code_analyst 重试;
        "end"：其余所有情况(discover / clarify / 不可重试 / 超限)直接到 END。

    判断顺序(先短路非 analyze,再看成功与不可重试,最后看上限)：
        1. 非 analyze(discover / clarify)→ end,不汇总也不重试;
        2. analyze 成功(exit_status == "success")→ summarize;
        3. 错误分析含「不可重试」→ end(重试不会改变结果);
        4. analyze 失败且 iteration >= 3 → end(超限停止,不得无限重试);
        5. analyze 失败且 iteration < 3 → retry。
    """
    if state.get("next_action") != "analyze":
        return "end"
    if state.get("exec_result", {}).get("exit_status") == "success":
        return "summarize"
    # 「不可重试」错误(数据集不可用 / 未锁定数据集 / LLM 配置错误)：
    # 重试不会改变结果,直接结束,避免无意义循环空耗
    if "不可重试" in state.get("error_analysis", ""):
        return "end"
    # 硬性收敛边界：同一问题最多重试 3 次,超限如实上报不硬扛
    if state.get("iteration", 0) >= 3:
        return "end"
    return "retry"


# ---------- 汇总节点：结论质量与可复现 ----------


