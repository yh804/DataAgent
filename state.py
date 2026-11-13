"""LangGraph 状态图的强类型状态定义。

字段对应规则第 2 节：问题、对话历史、数据源、计划、代码、
执行结果、错误分析、迭代计数、最终答案。
"""

from typing import Any, TypedDict

from langgraph.graph.message import add_messages
from typing_extensions import Annotated


class AgentState(TypedDict, total=False):
    """数据分析 Agent 的图状态。

    total=False 允许节点按需增量写入字段。
    """

    # 用户问题与对话历史(add_messages 提供追加式 reducer)
    question: str
    messages: Annotated[list, add_messages]

    # 当前分析的数据集名称(由 planner 从问题中解析,或用户显式指定)
    dataset_name: str

    # planner 决定的下一步动作,用于条件边路由(如 "discover" / "analyze" / "clarify")
    next_action: str

    # 已发现的数据源清单(文件名列表,与 tools.list_datasets 返回值对齐)
    data_sources: list[str]

    # 分析计划(计划节点产出,可经 HITL 审批)
    plan: list[str]
    # planner 给出该计划的依据,随 plan 一起送进审批 payload,便于用户判断
    plan_reason: str
    # 人工审批结果：True=批准或编辑后继续;False=用户取消 / 审批输入非法而终止
    plan_approved: bool

    # 当前生成的代码与沙箱执行结果
    code: str
    exec_result: dict[str, Any]

    # 错误原因分析与重试计数(硬性收敛边界用)
    error_analysis: str
    iteration: int

    # ---------- 批次 B:结论质量与可复现 ----------
    # LLM 归纳出的报告摘要(ReportSummary.model_dump() 的可序列化 dict)
    summary: dict
    # sanity check 警告(执行结果 / 汇总数字校验),报告单列一节展示
    warnings: list[str]
    # 当轮数据集 schema 摘要(含 size_bytes / mtime_iso / sha256,供汇总与归档)
    dataset_schema: dict
    # 数据指纹短串,如 "测试数据.csv@a1b2c3d4e5f6"
    dataset_fingerprint: str
    # 运行 ID(%Y%m%d_%H%M%S + 短 uuid),outputs/runs/<run_id>/ 的目录名
    run_id: str
    # 上一轮分析的运行 ID(Web 层从会话索引注入):
    # 「生成网页 / 导出报告」直接复用该运行的 report.html 与 result.csv,不重跑分析
    last_run_id: str
    # deliver 轮次实际复用的运行 ID:与 run_id 不同(本轮不产生新归档),
    # 前端据此渲染「已复用」卡片并提供打开归档 / 下载入口
    deliver_run_id: str
    # clarify 的来源标记:值为 "deliver" 时,引导语由 planner 组装(归档路径或
    # 「请先重新分析」提示),code_analyst 必须原样回显,不能被通用 clarify 文案覆盖
    clarify_from: str
    # 归档后的图表路径(相对 outputs/,如 runs/<run_id>/charts/x.png)
    chart_files: list[str]
    # 本次会话累计 token 用量(summarize 由 trace handler 汇总后写入)
    token_used: int

    # 领域包(planner 加载后写入,供 code_analyst / summarize 消费;未启用时为 None)。
    # 必须在此显式声明:LangGraph 会静默丢弃未声明的键,曾导致 metric_block 注入失效。
    domain_pack: dict | None
    # planner 按问题选中的领域指标列表,避免下游节点重复打分
    domain_selected_metrics: list[dict]

    # 最终答案(结论 + 图表引用 + 复现说明)
    final_answer: str
