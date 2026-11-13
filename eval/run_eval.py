"""数据分析 Agent 一键回归评估脚本。

逐条执行 eval/cases.json 中的用例,按 checks 判定 pass/fail,输出表格,
并把完整报告落到 outputs/eval/eval_report_<时间戳>.json。全部通过 exit 0,
任一失败 exit 1(可接 CI)。

用法：
    python eval/run_eval.py                  # 跑全部用例
    python eval/run_eval.py --only case_count_rows   # 只跑指定用例
    python eval/run_eval.py --verbose         # 失败时打印 final_answer 便于排查

前置条件：
    1. .env 已配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL(deepseek-chat);
    2. Docker Desktop 在运行,且已执行 `docker build -t data-agent-sandbox:py311 sandbox/`;
    3. data/processed/ 下有 测试数据.csv(800 行：用户ID/分组/收入/是否购买/访问时长);
    4. 联网可用(调用 LLM)。

预期：8 条全绿,总耗时约 2 分钟(LLM + Docker 冷启动各占一部分)。

支持的检查项(见 eval/cases.json,缺省即不检查)：
    - final_contains: [str]        final_answer 必须包含这些子串
    - final_contains_any: [str]    final_answer 至少包含其中一个子串
    - final_not_contains: [str]    final_answer 不能包含这些子串
    - final_not_empty: bool        True=必须非空;False=必须为空
    - error_contains: [str]        error_analysis 必须包含这些子串
    - charts_min: int              本次新增图表数量下限
    - messages_contains: [str]     messages 拼接后必须包含这些子串
    - max_duration_ms: int         单用例墙钟耗时上限(毫秒)
    - report_sections: [str]       按 state["run_id"] 读 outputs/runs/<id>/report.md,
                                   断言这些章节标题全部存在(批次 B 新增)
    - summary_contains: [str]      读 report.md 的汇总正文(「## 一句话结论」到「## 图表」),
                                   断言这些子串全部出现(批次 C 新增:不再被附录兜底)
    - summary_not_contains: [str]  汇总正文不能包含这些子串
    - metric_tolerance: {metric_id, value, rel_tol}
                                   数字裁判:从 exec_result.stdout 解析
                                   「METRIC <metric_id>=<数值>」行,与期望值比相对误差,
                                   超 rel_tol 判 FAIL;来源优先 checks.metric_tolerance,
                                   缺省回落到用例顶层 tolerance 字段(兼容 metric/metric_id 键名)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# 把项目根目录加入 sys.path,使 `from main import run_agent` 在 eval/ 子目录下可用
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
# eval/ 目录也加入 sys.path,保证 `from judge import judge_report` 在 -m 运行时同样可用
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import run_agent  # noqa: E402  (sys.path 调整后再导入)
# 引用 main 模块以读取 LAST_TRACE_PATH(本文件也有 main() 函数,故用别名避免遮蔽)
import main as main_module

# 评估用例与报告输出路径
CASES_PATH = Path(__file__).resolve().parent / "cases.json"
OUTPUT_DIR = PROJECT_ROOT / "outputs"


def load_cases(cases_path: Path | None = None, only_id: str | None = None) -> list[dict]:
    """加载评估用例,支持 --cases 传入任意 JSON 路径与 --only 过滤单条。

    参数：
        cases_path: 用例文件路径;None 时用默认 CASES_PATH(eval/cases.json)。
                    传入 domains/ecommerce/eval/cases.json 时,顶层的
                    domain / version / note / metrics / datasets / tolerance
                    等扩展字段会被自然忽略,只取 cases 数组,不会报错。
        only_id: 仅运行该 id 的用例;None 表示运行全部。

    返回：
        用例字典列表。

    异常：
        FileNotFoundError: 指定的 cases 路径不存在;
        ValueError: --only 指定的 id 不存在。
    """
    path = cases_path or CASES_PATH
    if not path.is_file():
        raise FileNotFoundError(f"评估用例文件不存在: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if only_id:
        filtered = [c for c in cases if c.get("id") == only_id]
        if not filtered:
            raise ValueError(f"--only 指定的用例 id 不存在: {only_id}")
        return filtered
    return cases


def _stage_case_datasets(case: dict, pack_dir: Path | None) -> list[Path]:
    """把领域包用例引用的数据集复制到 data/processed,仅在缺失时复制。

    参数：
        case: 单条用例;读取其中的 datasets 字段(如 ["orders.csv"])。
        pack_dir: 领域包根目录(如 domains/ecommerce);None 时不做任何事。

    返回：
        本次实际复制进去的目标文件路径列表,供评测结束后清理;
        已存在而跳过的文件不在列表里(那是用户自己的数据,绝不动)。

    为什么复制而不是直接读领域包目录：
        沙箱只挂载 data/processed,agent 也只认这个目录;复制一份让
        业务数据和演示数据走同一条路径,避免为 eval 单独开沙箱挂载。
    """
    staged: list[Path] = []
    if pack_dir is None:
        return staged
    examples = pack_dir / "examples"
    for name in case.get("datasets") or []:
        source = examples / name
        target = PROJECT_ROOT / "data" / "processed" / name
        # 已存在就不覆盖:避免 eval 把用户手改过的同名数据冲掉
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            staged.append(target)
    return staged


def _cleanup_staged_datasets(staged: list[Path]) -> None:
    """评测结束后删除本次复制进去的数据文件,只删自己复制的。

    参数：
        staged: _stage_case_datasets 返回的路径列表。
                已存在而被跳过的文件不在其中,因此不会被误删。
    """
    for path in staged:
        try:
            path.unlink()
        except OSError:
            # 文件可能已被用户或上一次运行挪走,清理失败不影响评测结论
            pass


def _messages_text(state: dict) -> str:
    """把 state["messages"] 里所有消息的 content 拼成一段文本,供 messages_contains 检查。"""
    parts: list[str] = []
    for msg in state.get("messages", []):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if content:
            parts.append(str(content))
    return "\n".join(parts)


def _report_text(state: dict) -> str:
    """读取本次运行的 report.md 正文;找不到时返回空串。

    为什么读文件而不是用 final_answer:
        final_answer 末尾带「附录：原始输出」,汇总写崩也能命中数字;
        只有 report.md 的正文才能真实反映汇总质量。
    """
    run_id = state.get("run_id") or ""
    if not run_id:
        return ""
    report_path = OUTPUT_DIR / "runs" / run_id / "report.md"
    if not report_path.is_file():
        return ""
    return report_path.read_text(encoding="utf-8")


def _summary_body(report_text: str) -> str:
    """取报告「## 一句话结论」到「## 图表」之间的汇总正文。

    这段包含结论 / 发现 / 建议 / 局限(以及自动警告),不包含
    「口径与数据版本」「生成代码」「附录」,避免把非汇总内容当成断言目标。
    """
    start_marker = "## 一句话结论"
    end_marker = "## 图表"
    start = report_text.find(start_marker)
    if start == -1:
        return ""
    end = report_text.find(end_marker, start)
    if end == -1:
        end = len(report_text)
    return report_text[start:end]


def _parse_metric_lines(stdout: str) -> dict[str, float]:
    """从 stdout 解析「METRIC <metric_id>=<数值>」行,返回 {metric_id: float}。

    参数：
        stdout: 沙箱执行结果的标准输出;每行一个指标,
                格式固定为 METRIC <id>=<数值>(float() 可解析)。

    返回：
        解析成功的指标字典;格式不符或数值无法解析的行直接跳过。
    """
    metrics: dict[str, float] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("METRIC "):
            continue
        body = stripped[len("METRIC "):]
        if "=" not in body:
            continue
        metric_id, _, raw_value = body.partition("=")
        metric_id = metric_id.strip()
        if not metric_id:
            continue
        try:
            metrics[metric_id] = float(raw_value.strip())
        except ValueError:
            continue
    return metrics


def check_case(case: dict, state: dict, duration_ms: int) -> list[str]:
    """按用例的 checks 逐项校验,返回失败原因列表(空列表表示全部通过)。

    参数：
        case: 单条用例字典,含 question / checks。
        state: run_agent 返回的最终状态。
        duration_ms: 本用例的墙钟耗时(毫秒)。

    返回：
        失败原因字符串列表。每个元素描述一项未通过的检查。
    """
    checks = case.get("checks", {}) or {}
    failures: list[str] = []

    final = state.get("final_answer", "") or ""
    err = state.get("error_analysis", "") or ""
    charts = state.get("exec_result", {}).get("charts", []) or []
    msgs = _messages_text(state)

    # ---- final_contains ----
    for expected in checks.get("final_contains", []):
        if expected not in final:
            failures.append(f"final_contains 缺失: {expected!r}")

    # ---- final_contains_any(至少命中一个,用于同义表述的场景)----
    any_expected = checks.get("final_contains_any", [])
    if any_expected and not any(token in final for token in any_expected):
        failures.append(f"final_contains_any 均未出现: {any_expected!r}")

    # ---- final_not_contains ----
    for forbidden in checks.get("final_not_contains", []):
        if forbidden in final:
            failures.append(f"final_not_contains 出现: {forbidden!r}")

    # ---- final_not_empty (bool) ----
    if "final_not_empty" in checks:
        if checks["final_not_empty"]:
            if not final:
                failures.append("final_not_empty=True 但 final_answer 为空")
        else:
            if final:
                failures.append("final_not_empty=False 但 final_answer 非空")

    # ---- error_contains ----
    for expected in checks.get("error_contains", []):
        if expected not in err:
            failures.append(f"error_contains 缺失: {expected!r}")

    # ---- charts_min ----
    if "charts_min" in checks:
        if len(charts) < checks["charts_min"]:
            failures.append(
                f"charts_min={checks['charts_min']} 但实际生成 {len(charts)} 个图表"
            )

    # ---- messages_contains ----
    for expected in checks.get("messages_contains", []):
        if expected not in msgs:
            failures.append(f"messages_contains 缺失: {expected!r}")

    # ---- max_duration_ms ----
    if "max_duration_ms" in checks:
        if duration_ms > checks["max_duration_ms"]:
            failures.append(
                f"max_duration_ms={checks['max_duration_ms']} 但实际 {duration_ms}ms"
            )

    # ---- report_sections ----
    # 断言运行归档里的 report.md 章节齐全:验证 summarize 节点真的落了盘,
    # 而不只是 final_answer 文本里出现过这些标题
    sections = checks.get("report_sections", [])
    if sections:
        run_id = state.get("run_id") or ""
        report_path = OUTPUT_DIR / "runs" / run_id / "report.md" if run_id else None
        if not report_path or not report_path.is_file():
            failures.append(f"report_sections 检查失败: 找不到运行报告(run_id={run_id!r})")
        else:
            report_text = report_path.read_text(encoding="utf-8")
            for section in sections:
                if section not in report_text:
                    failures.append(f"report_sections 缺失章节: {section!r}")

    # ---- summary_contains / summary_not_contains ----
    # 断言对象是 report.md 的汇总正文(不含附录 / 生成代码 / 数据版本),
    # 保证「汇总写崩」时用例真的会红,而不是被 final_answer 附录兜底
    need_summary = checks.get("summary_contains") or checks.get("summary_not_contains")
    if need_summary:
        report_text = _report_text(state)
        if not report_text:
            failures.append(
                f"summary 检查失败: 找不到运行报告(run_id={state.get('run_id')!r})"
            )
        else:
            body = _summary_body(report_text)
            if not body:
                failures.append("summary 检查失败: report.md 缺少「## 一句话结论」汇总正文")
            for expected in checks.get("summary_contains", []):
                if expected not in body:
                    failures.append(f"summary_contains 缺失(汇总正文): {expected!r}")
            for forbidden in checks.get("summary_not_contains", []):
                if forbidden in body:
                    failures.append(f"summary_not_contains 出现(汇总正文): {forbidden!r}")

    # ---- metric_tolerance(数字裁判)----
    # 来源优先 checks.metric_tolerance,缺省回落到用例顶层 tolerance 字段;
    # 兼容 metric / metric_id 两种键名
    tolerance = checks.get("metric_tolerance") or case.get("tolerance")
    if tolerance:
        metric_id = tolerance.get("metric_id") or tolerance.get("metric") or ""
        expected = tolerance.get("value")
        rel_tol = tolerance.get("rel_tol")
        stdout = state.get("exec_result", {}).get("stdout", "") or ""
        parsed_metrics = _parse_metric_lines(stdout)
        if metric_id not in parsed_metrics:
            failures.append(f"metric_tolerance 检查失败: 未输出 METRIC {metric_id}")
        elif expected is not None and rel_tol is not None:
            actual = parsed_metrics[metric_id]
            rel_err = abs(actual - expected) / max(abs(expected), 1e-12)
            if rel_err > rel_tol:
                failures.append(
                    f"metric_tolerance 超差: {metric_id} 期望 {expected},"
                    f"实际 {actual},相对误差 {rel_err:.4f} > 容差 {rel_tol}"
                )

    return failures


def run_one(case: dict, verbose: bool) -> dict:
    """执行单条用例并返回结构化结果。

    参数：
        case: 用例字典。
        verbose: 是否在失败时打印 final_answer。

    返回：
        {id, question, result, duration_ms, iteration, charts,
         final_answer, error_analysis, trace_path, failures}

    异常处理：
        run_agent 抛出的任何异常都被捕获,记为失败并附带 traceback,
        保证单条用例出错不会中断整个评估。
    """
    question = case["question"]
    case_id = case["id"]

    start = time.perf_counter()
    try:
        # auto_approve=True 跳过 HITL 审批,避免评估卡在人工交互;
        # 不传 thread_id → run_agent 内部生成独立 uuid,用例之间互不影响
        state = run_agent(question, auto_approve=True)
    except Exception:  # 图执行或依赖异常：结构化记失败,不吞掉现场
        duration_ms = int((time.perf_counter() - start) * 1000)
        failures = ["run_agent 抛出异常:\n" + traceback.format_exc()]
        result = {
            "id": case_id,
            "question": question,
            "result": "FAIL",
            "duration_ms": duration_ms,
            "iteration": None,
            "charts": [],
            "final_answer": "",
            "error_analysis": "",
            "run_id": "",
            "trace_path": main_module.LAST_TRACE_PATH or "",
            "failures": failures,
        }
        return result

    duration_ms = int((time.perf_counter() - start) * 1000)
    failures = check_case(case, state, duration_ms)

    result = {
        "id": case_id,
        "question": question,
        "result": "PASS" if not failures else "FAIL",
        "duration_ms": duration_ms,
        "iteration": state.get("iteration"),
        "charts": state.get("exec_result", {}).get("charts", []) or [],
        "final_answer": state.get("final_answer", "") or "",
        "error_analysis": state.get("error_analysis", "") or "",
        "run_id": state.get("run_id", "") or "",
        # 本次 run_agent 生成的 trace 文件路径,便于从报告直接跳转定位
        "trace_path": main_module.LAST_TRACE_PATH or "",
        "failures": failures,
    }

    if verbose and failures:
        print(f"\n--- [{case_id}] final_answer ---")
        print(result["final_answer"] or "(空)")
        print(f"--- [{case_id}] error_analysis ---")
        print(result["error_analysis"] or "(空)")

    return result


def print_table(results: list[dict]) -> None:
    """打印结果表格：id / 结果 / 耗时 / 失败原因。

    参数：
        results: run_one 返回的结果字典列表。
    """
    # 列宽：id 取实际最长,结果列固定 6,耗时列 10
    id_width = max((len(r["id"]) for r in results), default=10)
    header = f"{'用例 id':<{id_width}}  {'结果':<6}  {'耗时':>8}  失败原因"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        reason = "; ".join(r["failures"]) if r["failures"] else "-"
        print(f"{r['id']:<{id_width}}  {r['result']:<6}  {r['duration_ms']:>6}ms  {reason}")


def apply_judge(results: list[dict]) -> float | None:
    """对每条结果的 report.md 汇总正文打分,把得分挂到结果 dict 上。

    参数:
        results: run_one 返回的结果列表(需含 run_id / question)。

    返回:
        所有成功判分用例的均分;全部失败(或报告缺失)时返回 None。

    为什么单独放在这里:
        判分是「展示性」能力,失败绝不能影响 PASS/FAIL;所有异常都被吞掉。
    """
    try:
        # 延迟导入:未启用 --judge 时不加载 judge / LLM 依赖
        from judge import judge_report
    except Exception as exc:
        print(f"[eval] judge 模块加载失败,跳过判分: {exc}", file=sys.stderr)
        return None

    averages: list[float] = []
    for result in results:
        run_id = result.get("run_id") or ""
        report_path = OUTPUT_DIR / "runs" / run_id / "report.md" if run_id else None
        if not report_path or not report_path.is_file():
            result["judge"] = None
            continue
        try:
            score = judge_report(
                report_path.read_text(encoding="utf-8"), result.get("question", "")
            )
        except Exception as exc:
            print(f"[eval] 判分异常(已忽略): {type(exc).__name__}: {exc}", file=sys.stderr)
            score = None
        result["judge"] = score
        if score:
            averages.append(float(score["average"]))

    return round(sum(averages) / len(averages), 2) if averages else None


def write_report(results: list[dict], judge_average: float | None = None) -> Path:
    """把完整评估报告写到 outputs/eval/eval_report_<时间戳>.json。

    参数：
        results: 全部用例的结果字典列表。

    返回：
        报告文件的 Path。

    为什么单独用 eval/ 子目录:eval_report_*.json 历史上和 runs/、traces/、
    状态文件混在 outputs 根目录,40+ 份报告很难定位;独立子目录后根目录只剩
    runs/ / traces/ / eval/ / reports/ / legacy/ 与 5 个状态文件,一目了然。
    """
    eval_dir = OUTPUT_DIR / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "passed": sum(1 for r in results if r["result"] == "PASS"),
        "failed": sum(1 for r in results if r["result"] == "FAIL"),
        # --judge 启用时才有值;判分失败不影响 passed / failed
        "judge_average": judge_average,
        "cases": results,
    }
    report_path = eval_dir / f"eval_report_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report_path


def main() -> int:
    """评估主入口：解析参数、加载用例、逐条执行、输出表格与报告、返回 exit code。

    返回：
        0 = 全部通过;1 = 存在失败用例(供 CI 判断)。
    """
    parser = argparse.ArgumentParser(description="数据分析 Agent 回归评估")
    parser.add_argument("--only", default=None, help="只运行指定 id 的单条用例")
    parser.add_argument(
        "--cases",
        default=None,
        help="用例文件路径,默认 eval/cases.json;可传 domains/ecommerce/eval/cases.json 等领域包用例",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="失败时打印 final_answer / error_analysis"
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="启用 LLM-as-judge 对 report.md 汇总正文打分(默认关闭,失败不影响 PASS/FAIL)",
    )
    args = parser.parse_args()

    # --cases 允许相对路径:先 resolve 成绝对路径,显示时再尽量相对项目根,
    # 否则外部路径会在 relative_to 处抛 ValueError
    cases_path = Path(args.cases).resolve() if args.cases else None
    cases = load_cases(cases_path, args.only)
    if cases_path is None:
        path_display = str(CASES_PATH.relative_to(PROJECT_ROOT))
    else:
        try:
            path_display = str(cases_path.relative_to(PROJECT_ROOT))
        except ValueError:
            path_display = str(cases_path)
    print(f"[eval] 用例文件: {path_display};共加载 {len(cases)} 条")

    # 领域包用例:cases 文件位于 domains/<name>/eval/,数据在 domains/<name>/examples/
    pack_dir = cases_path.parent.parent if cases_path else None

    results: list[dict] = []
    for index, case in enumerate(cases, 1):
        case_id = case["id"]
        # 每条用例的数据文件即用即清:用例之间互不残留,缺表负例的前提才成立;
        # 只删本次复制的,用户原有文件绝不动
        staged_files = _stage_case_datasets(case, pack_dir)
        try:
            print(f"[{index}/{len(cases)}] 运行 {case_id}: {case['question']!r}")
            result = run_one(case, args.verbose)
        finally:
            _cleanup_staged_datasets(staged_files)
        results.append(result)
        # 每条结果即时输出一行,长耗时用例也能看到进度
        print(f"    -> {result['result']}  {result['duration_ms']}ms")

    # 汇总表格
    print_table(results)

    # LLM-as-judge(默认关闭):只写均分,不参与 PASS/FAIL 判定
    judge_average: float | None = None
    if args.judge:
        judge_average = apply_judge(results)
        print(f"[eval] judge 均分: {judge_average if judge_average is not None else '不可用'}")

    # 落盘报告
    report_path = write_report(results, judge_average)
    passed = sum(1 for r in results if r["result"] == "PASS")
    failed = len(results) - passed
    print(f"\n[eval] 通过 {passed}/{len(results)},失败 {failed}")
    print(f"[eval] 完整报告: {report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
