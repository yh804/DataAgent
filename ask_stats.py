"""命令行入口：统计分析(方法选择 + 确定性计算 + 解读 + 报告)。

用法：
    python ask_stats.py demo.csv "测试组和对照组的收入差异显著吗"
    python ask_stats.py demo.csv "收入和访问时长有关系吗"
    python ask_stats.py demo.csv "用收入和访问时长预测是否购买"

铁律：
    统计量全部由 stats.py(scipy / statsmodels)在主进程计算;LLM 只负责
    「选方法 + 填参数」和「解读结果」,不生成任何计算代码,也不接触原始数据。

流程：
    1. 读文件(复用 analyze 的校验 / Excel 转 CSV / 编码探测);
    2. 生成 schema(列名 / 类型 / 缺失率 / 5 行样本);
    3. LLM 选 StatsPlan 并打印 method + params + reason;
    4. 调用 stats.py 对应函数做确定性计算;
    5. LLM 生成 StatsInterpretation;
    6. 组织报告并复用 analyze.write_report 落盘。
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import analyze
import stats
from llm import StatsInterpretation, StatsPlan, get_llm
from prompts import (
    STATS_INTERPRET_PROMPT,
    STATS_PLAN_PROMPT,
    build_stats_interpret_user_prompt,
    build_stats_plan_user_prompt,
)
from tools import _detect_encoding
from trace import TraceCallbackHandler, trace_enabled, write_trace

# 方法名 → 确定性函数：只允许这 7 个,LLM 无法越界调用其他实现
STATS_FUNCS: dict[str, Callable[..., dict]] = {
    "describe_stats": stats.describe_stats,
    "ttest": stats.ttest,
    "chi_square": stats.chi_square,
    "anova": stats.anova,
    "correlation": stats.correlation,
    "linear_regression": stats.linear_regression,
    "ab_sample_size": stats.ab_sample_size,
}

# schema 抽样行数：只用于让 LLM 识别列含义,不代表整体分布
SAMPLE_ROWS = 5


class StatsInputError(ValueError):
    """输入 / 方法 / 参数不合法,对应退出码 2。"""


def read_dataframe(source: Path, sheet: str | None) -> pd.DataFrame:
    """读取 CSV 或 Excel 为 DataFrame(Excel 复用 analyze 的转换逻辑)。

    参数：
        source: 已校验通过的源文件绝对路径。
        sheet: Excel 的 sheet 名或索引;None 表示第一个 sheet。

    返回：
        DataFrame。

    异常：
        analyze.InputError: Excel 读取失败或 sheet 不存在。
        ValueError: CSV 解析失败。
    """
    if source.suffix.lower() in analyze.EXCEL_SUFFIXES:
        work_dir = analyze.create_work_dir()
        try:
            csv_path, _, _ = analyze.convert_excel_to_csv(source, work_dir, sheet)
            return pd.read_csv(csv_path, encoding="utf-8")
        finally:
            analyze.cleanup_data_dir(work_dir)

    # CSV 保留探测到的编码读取,与沙箱侧 describe_dataset 的口径一致
    encoding = _detect_encoding(source)
    return pd.read_csv(source, encoding=encoding)


def describe_frame(df: pd.DataFrame) -> dict[str, Any]:
    """生成给 LLM 的 schema 摘要：列名 / 类型 / 缺失率 / 5 行样本 / 行数。

    只传结构与极少量抽样,遵守「全量数据不进 LLM 上下文」红线。
    """
    columns = [
        {
            "name": str(column),
            "dtype": str(df[column].dtype),
            "null_ratio": round(float(df[column].isna().mean()), 4),
        }
        for column in df.columns
    ]
    sample = json.loads(
        df.head(SAMPLE_ROWS).to_json(orient="records", date_format="iso", force_ascii=False)
    )
    return {"columns": columns, "sample": sample, "row_count": int(len(df))}


def plan_stats(question: str, schema_info: dict, config: dict | None = None) -> StatsPlan:
    """调用 LLM 选择统计方法(结构化输出,method 由 Literal 限界)。"""
    structured_llm = get_llm().with_structured_output(StatsPlan, method="function_calling")
    result = structured_llm.invoke(
        [
            ("system", STATS_PLAN_PROMPT),
            ("human", build_stats_plan_user_prompt(question, schema_info)),
        ],
        config=config,
    )
    if result is None:
        raise StatsInputError("LLM 未返回可解析的统计方法")
    return result


def _coerce_param(func: Callable[..., dict], name: str, value: Any) -> Any:
    """按函数签名把 LLM 传来的值轻量纠正为正确类型。

    为什么需要：
        LLM 有时把布尔参数写成 "false" 字符串,直接使用会被当成真值,
        导致 paired/welch 语义完全反转,必须在调用前确定性纠正。
    """
    annotation = inspect.signature(func).parameters[name].annotation
    if annotation in (bool, "bool") and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "是"):
            return True
        if lowered in ("false", "0", "no", "否"):
            return False
    return value


def call_stats(method: str, df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """按方法名调用确定性统计函数,并做参数签名校验。

    参数：
        method: STATS_FUNCS 的键。
        df: 数据表(主进程内存中)。
        params: LLM 给出的参数。

    返回：
        统计结果 dict。

    异常：
        StatsInputError: 方法不存在、参数缺失 / 多余 / 类型不符。
        ValueError: 列不存在、组不存在等数据问题(由 stats 函数抛出)。
    """
    func = STATS_FUNCS.get(method)
    if func is None:
        raise StatsInputError(f"不支持的统计方法: {method};可选: {list(STATS_FUNCS)}")

    signature = inspect.signature(func)
    allowed = {name for name in signature.parameters if name != "df"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise StatsInputError(f"{method} 不支持参数: {unknown};可用参数: {sorted(allowed)}")

    missing = [
        name
        for name, param in signature.parameters.items()
        if name != "df"
        and param.default is inspect.Parameter.empty
        and name not in params
    ]
    if missing:
        raise StatsInputError(f"{method} 缺少必需参数: {missing}")

    kwargs = {
        name: _coerce_param(func, name, value)
        for name, value in params.items()
        if name in allowed
    }
    return func(df, **kwargs)


def interpret_stats(
    question: str,
    method: str,
    params: dict[str, Any],
    result: dict[str, Any],
    config: dict | None = None,
) -> StatsInterpretation:
    """调用 LLM 解读统计结果(只解释,不计算)。"""
    structured_llm = get_llm().with_structured_output(
        StatsInterpretation, method="function_calling"
    )
    result_text = json.dumps(result, ensure_ascii=False, default=str)
    interpreted = structured_llm.invoke(
        [
            ("system", STATS_INTERPRET_PROMPT),
            ("human", build_stats_interpret_user_prompt(question, method, params, result_text)),
        ],
        config=config,
    )
    if interpreted is None:
        raise RuntimeError("LLM 未返回可解析的解读")
    return interpreted


def _format_value(value: Any) -> str:
    """把结果值格式化为报告里的单元格文本。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_rows(rows: list[dict[str, Any]]) -> str:
    """把 [{...}] 渲染成 Markdown 表格(键取首行的顺序)。"""
    if not rows:
        return "(无)"
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_format_value(row.get(key)) for key in headers) + " |")
    return "\n".join(lines)


def render_result(result: dict[str, Any]) -> str:
    """把统计结果渲染成 Markdown：标量做指标表,列表 / 字典做子表或要点。"""
    scalars = {
        key: value
        for key, value in result.items()
        if not isinstance(value, (list, dict))
    }
    blocks = [
        "| 指标 | 值 |",
        "| --- | --- |",
        *[f"| {key} | {_format_value(value)} |" for key, value in scalars.items()],
    ]

    for key, value in result.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            blocks += ["", f"**{key}**", "", _render_rows(value)]
        elif isinstance(value, dict):
            blocks += ["", f"**{key}**", ""]
            blocks += [f"- {sub_key}: {_format_value(sub_value)}" for sub_key, sub_value in value.items()]
        elif isinstance(value, list):
            blocks += ["", f"**{key}**：{value}"]
    return "\n".join(blocks)


def _verdict(p_value: float | None, alpha: float = 0.05) -> str:
    """把 p 值转成「满足 / 不满足 / 无法判断」的结论文字。"""
    if p_value is None:
        return "无法判断(样本不足或未做该检验)"
    return "满足(p≥0.05)" if p_value >= alpha else "不满足(p<0.05)"


def render_assumptions(method: str, result: dict[str, Any]) -> str:
    """按方法列出假设检查结果与结论,作为报告的「假设检查」章节。"""
    lines: list[str] = []

    if method == "ttest":
        test_name = result.get("normality_test", "shapiro")
        lines += [
            f"- 正态性({test_name})：组 A p={_format_value(result.get('normality_p_a'))},"
            f"{_verdict(result.get('normality_p_a'))};"
            f"组 B p={_format_value(result.get('normality_p_b'))},"
            f"{_verdict(result.get('normality_p_b'))}",
        ]
        if "levene_p" in result:
            lines.append(
                f"- 方差齐性(Levene)：p={_format_value(result.get('levene_p'))},"
                f"{_verdict(result.get('levene_p'))}"
            )
        else:
            lines.append("- 配对设计：不做方差齐性检验;配对差值近似正态时 t 检验才可靠")
    elif method == "anova":
        lines += [
            f"- 正态性(各组最小 p)：{_format_value(result.get('normality_p'))},"
            f"{_verdict(result.get('normality_p'))}",
            f"- 方差齐性(Levene)：p={_format_value(result.get('levene_p'))},"
            f"{_verdict(result.get('levene_p'))}",
        ]
        by_group = result.get("normality_by_group") or {}
        if by_group:
            lines.append(
                "- 各组正态性 p：" + ";".join(f"{name}={_format_value(p)}" for name, p in by_group.items())
            )
    elif method == "chi_square":
        min_expected = result.get("min_expected")
        reliable = isinstance(min_expected, (int, float)) and min_expected >= 5
        lines.append(
            f"- 期望频数：最小期望频数={_format_value(min_expected)},"
            f"{'满足(≥5)' if reliable else '不满足(<5),卡方近似可能不可靠'}"
        )
        lines.append("- 观测独立性：要求每个样本只出现一次且互不影响,需结合数据采集方式判断")
    elif method == "correlation":
        lines.append("- 相关分析不要求变量正态;Pearson 对异常值敏感,Spearman 更稳健")
        lines.append("- 相关不等于因果,可能存在混杂变量或反向因果")
    elif method == "linear_regression":
        lines.append("- 线性、误差独立同方差、无强影响点等 OLS 假设本期未做残差诊断,需人工判断")
        lines.append(
            f"- 样本量 {result.get('n')} 与自变量个数相比应充足(经验上每自变量 ≥10~20 个样本)"
        )
    elif method == "describe_stats":
        lines.append("- 描述统计不做假设检验;均值受异常值影响,建议同时看中位数与四分位距")
    elif method == "ab_sample_size":
        lines.append("- 样本量按两比例 z 检验 + 正态近似估算,未做连续性校正")
        lines.append("- 假设：两组样本量相等、双侧检验、转化率不随时间漂移")

    return "\n".join(lines) if lines else "(该方法无额外假设检查)"


def build_stats_report(
    source: Path,
    question: str,
    method: str,
    params: dict[str, Any],
    reason: str,
    result: dict[str, Any],
    interpretation: StatsInterpretation | None,
    now: datetime,
) -> str:
    """组装统计分析报告：方法 / 计算结果 / 假设检查 / 解读 / 生成代码。"""
    interpretation_lines = ["(LLM 解读生成失败,请直接参考上方计算结果)"]
    if interpretation is not None:
        interpretation_lines = [
            f"**结论**：{interpretation.conclusion}",
            "",
            f"**假设判断**：{interpretation.assumptions}",
            "",
            f"**注意事项**：{interpretation.caveats}",
        ]
        if interpretation.next_steps:
            interpretation_lines += ["", "**建议下一步**", ""]
            interpretation_lines += [
                f"{index}. {step}" for index, step in enumerate(interpretation.next_steps, 1)
            ]

    return "\n".join(
        [
            "# 统计分析报告",
            "",
            f"- 文件：{source}",
            f"- 问题：{question}",
            f"- 时间：{now:%Y-%m-%d %H:%M:%S}",
            "",
            "## 统计方法",
            "",
            f"- 方法：`{method}`",
            f"- 参数：`{json.dumps(params, ensure_ascii=False)}`",
            f"- 选择理由：{reason or '(未提供)'}",
            "",
            "## 计算结果",
            "",
            render_result(result),
            "",
            "## 假设检查",
            "",
            render_assumptions(method, result),
            "",
            "## 解读",
            "",
            *interpretation_lines,
            "",
            "## 生成代码",
            "",
            "本期统计量由主进程的确定性函数(stats.py,scipy / statsmodels)计算,"
            "未使用 LLM 生成统计代码。",
            "",
        ]
    )


def _run(args: argparse.Namespace, config: dict | None) -> int:
    """解析参数后的执行主体;config 只含 trace 回调,透传给每次 LLM 调用。"""
    source = Path(args.input).expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    source = source.resolve()

    try:
        analyze.validate_input(source)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ask_stats] {exc}", file=sys.stderr)
        return 2

    try:
        df = read_dataframe(source, args.sheet)
    except (analyze.InputError, ValueError, OSError) as exc:
        print(f"[ask_stats] 读取数据失败: {exc}", file=sys.stderr)
        return 2

    if df.empty or len(df.columns) == 0:
        print("[ask_stats] 数据集为空(0 行或 0 列),无法做统计检验", file=sys.stderr)
        return 2

    schema_info = describe_frame(df)
    print(f"[ask_stats] 数据: {len(df)} 行 × {len(df.columns)} 列")

    # ---------- 1. LLM 选方法(失败 → 退出码 2) ----------
    try:
        plan = plan_stats(args.question, schema_info, config)
    except Exception as exc:
        print(f"[ask_stats] 统计方法选择失败: {exc}", file=sys.stderr)
        return 2

    print(f"\n[ask_stats] 统计方法：{plan.method}")
    print(f"[ask_stats] 参数：{json.dumps(plan.params, ensure_ascii=False)}")
    print(f"[ask_stats] 选择理由：{plan.reason}\n")

    if plan.method == "clarify":
        print(f"[ask_stats] 需要补充信息：{plan.reason or '问题不够明确'}", file=sys.stderr)
        return 2

    # ---------- 2. 确定性计算(输入问题 → 2,计算异常 → 1) ----------
    try:
        result = call_stats(plan.method, df, plan.params)
    except (StatsInputError, ValueError, KeyError) as exc:
        print(f"[ask_stats] 输入的列 / 组 / 参数不合法: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ask_stats] 统计计算失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("[ask_stats] 计算结果：")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # ---------- 3. LLM 解读(失败不丢结果,仍出报告但返回 1) ----------
    interpretation: StatsInterpretation | None = None
    try:
        interpretation = interpret_stats(
            args.question, plan.method, plan.params, result, config
        )
    except Exception as exc:
        print(f"[ask_stats] 解读生成失败(计算结果仍会写入报告): {exc}", file=sys.stderr)

    # ---------- 4. 报告落盘(复用 analyze.write_report) ----------
    now = datetime.now()
    report_text = build_stats_report(
        source,
        args.question,
        plan.method,
        plan.params,
        plan.reason,
        result,
        interpretation,
        now,
    )
    report_path = analyze.write_report(report_text, now)
    print(f"[ask_stats] 报告: {report_path}")

    return 0 if interpretation is not None else 1


def main() -> int:
    """命令行入口：解析参数 → 挂 trace 回调执行 → 落 trace。

    返回：
        0 = 成功;1 = 计算失败(stats 函数或解读异常);
        2 = 输入错误 / 方法选择失败 / 参数不合法。
    """
    parser = argparse.ArgumentParser(description="统计分析：自动选方法、确定性计算并生成报告")
    parser.add_argument("input", help="待分析的文件路径(.csv / .xlsx / .xls)")
    parser.add_argument("question", help="自然语言统计问题")
    parser.add_argument("--sheet", default=None, help="Excel 的 sheet 名或索引,默认第一个")
    args = parser.parse_args()

    # 本入口此前完全没有观测:挂上 TraceCallbackHandler,把方法选择 / 解读的 LLM 调用记入 trace
    handler = TraceCallbackHandler() if trace_enabled() else None
    config = {"callbacks": [handler]} if handler else None
    started_at = datetime.now()
    try:
        return _run(args, config)
    finally:
        if handler is not None:
            write_trace(handler, "ask_stats", args.question, started_at)


if __name__ == "__main__":
    sys.exit(main())
