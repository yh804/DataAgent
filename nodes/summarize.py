"""summarize 节点: 报告汇总、模板降级、归档与索引。"""


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

__all__ = [
    "_summary_enabled",
    "_extract_explanation",
    "_legacy_answer",
    "_template_summary",
    "_template_followups",
    "_normalize_followups",
    "_call_summary",
    "_summarize_impl",
    "summarize_node",
]

def _summary_enabled() -> bool:
    """读取 SUMMARY_ENABLED(默认 true):只控制是否调 LLM 生成总结。

    为什么默认 true:汇总节点本身必须始终存在(写报告 / 归档),
    关闭开关只是不调 LLM,改用模板降级,而不是跳过整个节点。
    """
    raw = os.getenv("SUMMARY_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")



def _extract_explanation(state: AgentState) -> str:
    """从最近一条 [code_analyst] 消息中提取「分析思路」。

    为什么从 messages 取:explanation 只存在于 code_analyst 的局部变量,
    state 未单独持久化;messages 由 add_messages 追加,跨轮完整保留。

    参数:
        state: 图状态。

    返回:
        分析思路文本;解析不到时返回空串(调用方自行兜底)。
    """
    for msg in reversed(state.get("messages", [])):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if not content or not str(content).startswith("[code_analyst]"):
            continue
        text = str(content)
        marker = "分析思路："
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        return tail.split("\n结果：", 1)[0].strip()
    return ""



def _legacy_answer(explanation: str, stdout: str, chart_files: list[str]) -> str:
    """旧格式答案:分析思路 + 原始输出 + 图表清单。

    为什么保留:SUMMARY_ENABLED=false 时对外输出必须与旧版一致,
    不引入新章节,避免下游脚本 / 用户习惯被破坏。
    """
    charts_text = ""
    if chart_files:
        charts_text = "\n\n图表文件：\n" + "\n".join(
            f"- outputs/{chart}" for chart in chart_files
        )
    if explanation:
        return f"{explanation}\n\n{stdout}{charts_text}"
    return f"{stdout}{charts_text}".strip() or "(无输出)"



def _template_summary(explanation: str, stdout: str) -> dict:
    """LLM 汇总失败时的模板降级,保证 final_answer / report.md 都有内容。

    参数:
        explanation: code_analyst 的分析思路(作为 headline)。
        stdout: 沙箱原始输出(取前 10 行作为 findings,数字天然可回溯)。

    返回:
        与 ReportSummary.model_dump() 同结构的 dict。
    """
    finding_lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    return {
        "headline": explanation.strip(),
        "findings": finding_lines[:10],
        "recommendations": [],
        "caveats": ["LLM 汇总不可用,结论需人工复核"],
        "method": "",
        # 降级路径没有 LLM 推荐,置空由前端模板兜底
        "followups": [],
    }



def _template_followups(schema_info: dict, findings: list[str]) -> list[str]:
    """生成与前端同款的模板追问(最多 3 条),用于后端兜底补足。

    参数:
        schema_info: describe_dataset 的返回值(用列结构与 n_unique 挑选追问维度)。
        findings: 本次汇总的关键发现(非空说明有可深挖的差异)。

    返回:
        按「分组差异 → 异常值 → 显著性 → 通用」优先级排列的追问列表。

    为什么搬到后端:
        LLM 只返回 1~2 条追问时,需要补足到 3 条;模板逻辑放在后端,
        前端才能专注于「优先展示 LLM 推荐」,两处逻辑不再重复漂移。
    """
    columns = schema_info.get("columns") or []
    numeric = [
        str(column.get("name"))
        for column in columns
        if re.search(r"int|float", str(column.get("dtype", ""))) and column.get("name")
    ]
    categorical = [
        str(column.get("name"))
        for column in columns
        if not re.search(r"int|float", str(column.get("dtype", "")))
        and column.get("name")
        # 与前端口径一致:低基数分类列(1 < n_unique <= 20)适合作为分组维度
        and 1 < int(column.get("n_unique") or 0) <= 20
    ]
    questions: list[str] = []
    if categorical:
        questions.append(f"按「{categorical[0]}」拆分,看看各组差异?")
    if numeric:
        questions.append(f"「{numeric[0]}」的分布有没有异常值?")
    if findings:
        questions.append("这些差异在统计上显著吗?")
    fallback = ["换个维度再对比看看?", "能给我一份结论摘要吗?", "这些结论有哪些局限?"]
    for item in fallback:
        if len(questions) >= 3:
            break
        if item not in questions:
            questions.append(item)
    return questions[:3]



def _normalize_followups(
    followups: list | None, schema_info: dict, findings: list[str]
) -> list[str]:
    """清洗 LLM 产出的 followups,返回 0 条(前端兜底)或恰好 3 条。

    规则(与任务契约一致):
    - 去空白 / 去空串 / 去重;
    - 多于 3 条直接截断;
    - 1~2 条用后端模板补足到 3 条;
    - 一条都没有时返回 [],由前端回退到模板展示。
    """
    cleaned: list[str] = []
    for item in followups or []:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return []
    if len(cleaned) > 3:
        return cleaned[:3]
    for item in _template_followups(schema_info, findings):
        if len(cleaned) >= 3:
            break
        if item not in cleaned:
            cleaned.append(item)
    return cleaned



def _call_summary(
    question: str,
    plan: list[str],
    explanation: str,
    stdout: str,
    schema_info: dict,
    warnings: list[str],
    charts: list[str],
    fingerprint: str,
    config: RunnableConfig,
    quality_note: str = "",
) -> dict:
    """调用一次 LLM 生成 ReportSummary,返回可序列化 dict(最多 1 次调用)。

    参数:
        question / plan / explanation / stdout / schema_info / warnings /
        charts / fingerprint: 见 build_summary_user_prompt。
        config: 运行配置,透传后由 TraceCallbackHandler 记录耗时与 token。

    返回:
        ReportSummary.model_dump() 的 dict(只含可序列化字段,便于落检查点)。

    异常:
        Exception: LLM 调用或结构化解析失败,由调用方降级为模板。
    """
    result: ReportSummary = (
        get_llm()
        # 与项目其他结构化输出一致:DeepSeek 用 function_calling
        .with_structured_output(ReportSummary, method="function_calling")
        .invoke(
            [
                SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_summary_user_prompt(
                        question,
                        plan,
                        explanation,
                        stdout,
                        schema_info,
                        warnings,
                        charts,
                        fingerprint,
                        # 领域包 draft 口径提示,空串时旧行为逐字节不变
                        quality_note=quality_note,
                    )
                ),
            ],
            config=config,
        )
    )
    if result is None:
        raise RuntimeError("LLM 未返回可解析的汇总结果")
    dump = result.model_dump()
    # followups 在后端做一次规范化:空则交给前端模板兜底,1~2 条后端补足,
    # 超 3 条截断;不额外调用 LLM,与汇总同一次请求产出
    dump["followups"] = _normalize_followups(
        dump.get("followups"), schema_info, dump.get("findings") or []
    )
    return dump



def _summarize_impl(state: AgentState, config: RunnableConfig) -> dict:
    """summarize 节点的实现:校验 → 汇总 → 归档 → 渲染 final_answer。

    参数:
        state: 图状态(来自 analyze 成功分支)。
        config: 运行配置,读取 thread_id 写运行索引。

    返回:
        部分状态更新(summary / warnings / chart_files / final_answer / messages)。
    """
    config = config or {}
    thread_id = (config.get("configurable") or {}).get("thread_id", "")

    question = _extract_question(state)
    dataset_name = state.get("dataset_name", "")
    schema_info = state.get("dataset_schema") or {}
    fingerprint = state.get("dataset_fingerprint") or fingerprint_text(schema_info)
    run_id = state.get("run_id") or _new_run_id()
    exec_result = state.get("exec_result") or {}
    stdout = str(exec_result.get("stdout") or "")
    code = state.get("code") or ""
    iteration = int(state.get("iteration", 0) or 0)
    duration_ms = exec_result.get("duration_ms")
    explanation = _extract_explanation(state)
    plan = state.get("plan") or []

    # ---------- 1. sanity check:坏图就地剔除,行数 / 空结果记警告 ----------
    warnings = validate_result(exec_result, schema_info)
    charts = [str(item) for item in (exec_result.get("charts") or [])]

    # ---------- 2. 图表归档(移动),拿到报告里展示的新路径 ----------
    chart_files, move_warnings = _archive_charts(run_id, charts)
    warnings.extend(move_warnings)

    # ---------- 2.5 领域包 draft 口径提示:仅在 pack 启用且选中指标含 draft 时传入 ----------
    quality_note = ""
    pack = state.get("domain_pack")
    selected_metrics = state.get("domain_selected_metrics") or []
    if pack is not None:
        try:
            from domains import loader as domain_loader  # noqa: PLC0415 - 运行时按需导入

            quality_note = domain_loader.build_quality_note(pack, selected_metrics)
        except ImportError:
            quality_note = ""

    # ---------- 3. LLM 汇总(最多 1 次;失败 / 关闭开关 / 超预算都走模板降级) ----------
    summary: dict | None = None
    budget_error = ""
    if _summary_enabled():
        if _budget_blocked(config):
            # 超预算：不发起调用,降级为模板汇总并如实上报(不可重试)
            budget_error = _budget_error(config)
            warnings.append(f"预算超限,已跳过 LLM 汇总: {budget_error}")
            summary = _template_summary(explanation, stdout)
        else:
            try:
                summary = _call_summary(
                    question,
                    plan,
                    explanation,
                    stdout,
                    schema_info,
                    warnings,
                    chart_files,
                    fingerprint,
                    config,
                    # 领域包 draft 口径提示:空串时旧行为逐字节不变
                    quality_note=quality_note,
                )
                # 数字溯源:汇总里出现合法来源(stdout + schema 摘要)找不到的数字时记警告(不阻断)
                warnings.extend(validate_report(summary, stdout, schema_info))
                # 领域包 draft 口径溯源:pack 启用且选中 draft 指标时,报告必须提到口径待确认
                if pack is not None:
                    try:
                        from domains import loader as domain_loader  # noqa: PLC0415 - 运行时按需导入

                        domain_warnings = domain_loader.validate_domain_report(
                            summary, selected_metrics
                        )
                        warnings.extend(domain_warnings)
                    except Exception:  # noqa: BLE001 - 领域包校验是可选项,失败不阻断
                        pass
            except Exception as exc:
                summary = _template_summary(explanation, stdout)
                warnings.append(f"LLM 汇总不可用,已回退模板: {type(exc).__name__}: {exc}")

    warnings = list(dict.fromkeys(warnings))

    # 本次会话 token 用量(由 trace handler 记录的真实 LLM 用量汇总)
    token_used = _used_tokens(config)

    # ---------- 4. 组装运行上下文并渲染报告 ----------
    now_text = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    run = {
        "run_id": run_id,
        "question": question,
        "dataset": dataset_name,
        "fingerprint": fingerprint,
        "schema": schema_info,
        "summary": summary,
        "fallback_headline": explanation,
        "warnings": warnings,
        "charts": chart_files,
        "code": code,
        "duration_ms": duration_ms,
        "iteration": iteration,
        "status": "success",
        "now": now_text,
        # 成本统计:本次会话累计 token 用量(index.jsonl 与报告成本一节使用)
        "token_used": token_used,
        # 原始输出只落 result.json,供 grep 回溯;不进 LLM 上下文
        "stdout": stdout,
        "stderr": str(exec_result.get("stderr") or ""),
    }
    # ---------- 4.5 结果表 CSV:沙箱把真实 DataFrame 写到 /outputs/result.csv,
    # 这里把它移进归档目录;只是附加产物,失败只记 warning ----------
    result_csv_path = OUTPUT_DIR / "result.csv"
    run["result_csv"] = False
    if result_csv_path.is_file():
        try:
            target_dir = RUNS_DIR / run_id
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(result_csv_path), str(target_dir / "result.csv"))
            run["result_csv"] = True
        except OSError as exc:
            warnings.append(f"结果表归档失败: {type(exc).__name__}: {exc}")

    report_text = render_report(run)

    # ---------- 5. 落盘:report.md 永远生成(SUMMARY_ENABLED=false 也不例外) ----------
    _write_run_archive(run_id, run, report_text, code)
    _append_run_index(run_id, thread_id, run)

    # ---------- 6. final_answer:报告正文 + 原始输出附录(关闭汇总时用旧格式) ----------
    if summary is None:
        # SUMMARY_ENABLED=false:对外仍是旧格式,不含新章节
        final_answer = _legacy_answer(explanation, stdout, chart_files)
        reply_text = f"[summarize] {final_answer}"
    else:
        raw_stdout = stdout
        if len(raw_stdout) > FINAL_STDOUT_LIMIT:
            raw_stdout = raw_stdout[:FINAL_STDOUT_LIMIT] + "\n…(输出过长,已截断)"
        final_answer = (
            f"{report_text}\n\n## 附录：原始输出\n\n```text\n{raw_stdout}\n```"
        )
        headline = (summary.get("headline") or explanation or "").strip()
        findings = summary.get("findings") or []
        lines = [f"[summarize] 结论：{headline or '(见报告正文)'}"]
        if findings:
            lines.append("关键发现：")
            lines.extend(f"- {item}" for item in findings[:4])
        reply_text = "\n".join(lines)

    return {
        "summary": summary or {},
        "warnings": warnings,
        "chart_files": chart_files,
        "run_id": run_id,
        "token_used": token_used,
        # 超预算时把结构化错误(不可重试)也写回 state;正常成功轮为空串
        "error_analysis": budget_error,
        "final_answer": final_answer,
        "messages": [AIMessage(content=reply_text)],
    }



def summarize_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """汇总节点:把「print 出来的 DataFrame」升级为结论先行的报告。

    进入条件:只有在 analyze 成功(exit_status == "success")时才会被路由进来。

    行为:
        1. sanity check(validate_result)与汇总数字溯源(validate_report);
        2. SUMMARY_ENABLED=true 时调用 1 次 LLM 生成 ReportSummary;
           关闭或 LLM 失败时用模板降级(headline=分析思路, findings=stdout 前 10 行);
        3. 图表从 outputs/ 移动到 outputs/runs/<run_id>/charts/;
        4. 写 result.json / report.md / code.py 与 outputs/index.jsonl;
        5. final_answer = 报告正文 + 「附录：原始输出」(保证数字可回溯)。

    参数:
        state: LangGraph 状态字典。
        config: 运行配置(用于读取 thread_id)。

    返回:
        部分状态更新。整个节点包在 try/except 里:即使渲染 / 归档失败,
        final_answer 也回退为旧格式答案,绝不留空(硬约束 3)。
    """
    try:
        return _summarize_impl(state, config or {})
    except Exception as exc:
        # 兜底:归档 / 渲染任何一步失败都不能让 final_answer 为空
        exec_result = state.get("exec_result") or {}
        stdout = str(exec_result.get("stdout") or "")
        fallback = _legacy_answer(_extract_explanation(state), stdout, [])
        note = f"\n\n[summarize] 报告生成失败({type(exc).__name__}: {exc}),已回退到原始输出。"
        warnings = list(state.get("warnings") or [])
        warnings.append(f"报告生成失败: {type(exc).__name__}: {exc}")
        return {
            "final_answer": f"{fallback}{note}",
            "warnings": warnings,
            "messages": [
                AIMessage(content=f"[summarize] 报告生成失败,已回退到原始输出: {exc}")
            ],
        }


