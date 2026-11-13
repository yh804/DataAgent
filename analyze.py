"""命令行工具：分析 CSV / Excel 并导出 Markdown 报告。

用法：
    python analyze.py "C:\\data\\sales.csv" "按月统计销售额趋势"
    python analyze.py "C:\\data\\sales.xlsx" "按月统计销售额趋势"
    python analyze.py "C:\\data\\sales.xlsx" "统计备注" --sheet 备注

流程：
    1. 校验文件存在且后缀受支持(.csv / .xlsx / .xls);
    2. 创建项目内临时数据目录;
    3. Excel 由主进程读成 DataFrame 再写成 UTF-8 CSV(沙箱只认 CSV,无需重建镜像);
    4. 设置 SANDBOX_DATA_DIR 指向临时目录;
    5. 运行 Agent(auto_approve=True 跳过人工审批);
    6. 导出 outputs/reports/report_<时间戳>.md;
    7. 无论成功失败都清理临时目录。

为什么复制 / 转换到临时目录：
    Agent 只认识「数据目录 + 文件名」,复制后原始数据路径不被改动,
    沙箱也只读挂载这一个临时目录,最小化数据暴露面。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

# 统一报告渲染:analyze / ask_db / summarize 共用同一模板,避免章节口径分裂
from reporting import render_report

# 项目根目录：报告、临时目录、沙箱产物都以它为基准,避免受调用时 cwd 影响
PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs"
# 临时数据目录的父目录：每次运行在其下创建唯一子目录,失败时也便于定位残留
TMP_ROOT = PROJECT_ROOT / ".tmp_analyze"

# 沙箱内只处理 CSV;Excel 在主进程转成 CSV 后再交给 Agent
CSV_SUFFIXES = {".csv"}
EXCEL_SUFFIXES = {".xlsx", ".xls"}
SUPPORTED_SUFFIXES = CSV_SUFFIXES | EXCEL_SUFFIXES


class InputError(Exception):
    """输入文件不合法(后缀 / 格式 / sheet),对应退出码 2。"""


class ExcelReadError(InputError):
    """Excel 读取失败(损坏、加密、缺少引擎),对应退出码 2。"""


class SheetNotFoundError(InputError):
    """指定的 sheet 不存在,对应退出码 2。"""


def validate_input(path: Path) -> None:
    """校验目标文件存在且后缀受支持。

    异常：
        FileNotFoundError: 文件不存在。
        ValueError: 后缀不在支持列表内。
    """
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"不支持的文件类型: {path.suffix or '(无后缀)'},"
            f"仅支持 {' / '.join(sorted(SUPPORTED_SUFFIXES))}"
        )


def create_work_dir() -> Path:
    """创建本次运行的唯一临时数据目录。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run_", dir=TMP_ROOT))


def _resolve_sheet(sheet: str | None, sheet_names: list[str]) -> str | int:
    """把 --sheet 参数解析为 pandas 可用的 sheet 名或索引。

    参数：
        sheet: 用户传入的 sheet 名或索引字符串;None 表示用第一个 sheet。
        sheet_names: 工作簿内的 sheet 名列表。

    返回：
        sheet 名(命中名称时)或 0 基索引(命中数字时)。

    异常：
        SheetNotFoundError: 名称和索引都无法匹配。
    """
    if sheet is None:
        return sheet_names[0]
    # 名称优先：避免用户想用名为 "1" 的 sheet 时被当成索引
    if sheet in sheet_names:
        return sheet
    if sheet.isdigit():
        index = int(sheet)
        if 0 <= index < len(sheet_names):
            return index
    raise SheetNotFoundError(
        f"sheet 不存在: {sheet};可用 sheet: {sheet_names}(索引从 0 开始)"
    )


def convert_excel_to_csv(
    source: Path,
    work_dir: Path,
    sheet: str | None,
) -> tuple[Path, list[str], str]:
    """把 Excel 指定 sheet 读成 DataFrame 并写出 UTF-8 CSV。

    参数：
        source: 源 Excel 绝对路径(.xlsx / .xls)。
        work_dir: 本次运行的临时数据目录,CSV 写在这里。
        sheet: sheet 名或索引;None 表示第一个 sheet。

    返回：
        (转换后的 CSV 路径, 全部 sheet 名列表, 实际使用的 sheet 名)。

    异常：
        ExcelReadError: 文件损坏、被加密或缺少读取引擎。
        SheetNotFoundError: sheet 不存在。

    为什么在主进程转换：
        沙箱镜像只装了 pandas、没有 openpyxl;在主进程用 DataFrame 转 CSV,
        沙箱挂载到的仍是普通 CSV,无需重建镜像。
    """
    try:
        workbook = pd.ExcelFile(source) # 打开 Excel 文件句柄的类
    except Exception as exc:
        # 常见原因：文件损坏 / 被密码加密 / .xls 缺少 xlrd 引擎
        raise ExcelReadError(
            f"Excel 读取失败(文件可能损坏、被加密或缺少读取引擎): {exc}"
        ) from exc

    try:
        sheet_names = list(workbook.sheet_names)  # 获取全部sheet名称列表
        target = _resolve_sheet(sheet, sheet_names)  # 解析输入sheet,返回索引或sheet名
        used_name = target if isinstance(target, str) else sheet_names[target]  # 统一拿到sheet真实名称(用于日志/报错)
        frame = workbook.parse(target)  # 读取指定sheet,得到DataFrame
    except SheetNotFoundError:
        raise
    except Exception as exc:
        raise ExcelReadError(f"Excel sheet 解析失败: {exc}") from exc
    finally:
        workbook.close()

    # 用 <原名>.csv：去掉 Excel 后缀、保留主干,避免中文 / 空格影响沙箱路径
    csv_path = work_dir / f"{source.stem}.csv"
    frame.to_csv(csv_path, index=False, encoding="utf-8")
    return csv_path, sheet_names, used_name


def cleanup_data_dir(work_dir: Path) -> None:
    """删除本次临时目录;父目录为空时一并清理,不留残渣。"""
    shutil.rmtree(work_dir, ignore_errors=True)
    try:
        TMP_ROOT.rmdir()
    except OSError:
        # 父目录非空(并发运行留下其他子目录)时保留,不影响本次结果
        pass


def _conclusion_text(state: dict) -> str:
    """按优先级取结论：成功结论 > 结构化错误 > 最后一条消息。

    参数：
        state: run_agent 返回的最终状态字典。

    返回：
        用于报告「结论」章节的文本,保证不会为空。
    """
    final_answer = state.get("final_answer") or ""
    if final_answer:
        return final_answer.strip()
    error_analysis = state.get("error_analysis") or ""
    if error_analysis:
        return error_analysis.strip()
    messages = state.get("messages") or []
    if messages:
        return str(getattr(messages[-1], "content", "")).strip() or "(无结果)"
    return "(无结果)"


def build_report(
    source_path: Path,
    user_question: str,
    state: dict,
    now: datetime,
    analysis_name: str | None = None,
    sql: str = "",
) -> str:
    """组装 Markdown 报告文本(统一走 reporting.render_report)。

    参数：
        source_path: 用户传入的原始文件绝对路径(写入报告元信息)。
        user_question: 用户原始问题(不含 analyze 拼接的「分析 <文件名>：」前缀)。
        state: run_agent 返回的最终状态字典。
        now: 报告生成时间,同时用于文件命名与正文展示。
        analysis_name: 实际交给 Agent 的文件名;仅 Excel / 数据库转换时传入,
            为 None 表示 CSV 原样分析。
        sql: 数据库问答入口执行的 SQL;非空时在「口径与数据版本」后插入「执行的 SQL」。

    返回：
        完整 Markdown 文本。

    为什么改成组装 run dict 再渲染：
        章节顺序 / 图表路径 / 数据版本口径统一收口到 reporting.render_report,
        三个入口(analyze / ask_db / summarize)输出结构一致,便于对比与回归。
    """
    exec_result = state.get("exec_result") or {}
    # 优先使用 summarize 归档后的图表路径;未经过 summarize(如失败)时回退沙箱清单
    charts = state.get("chart_files") or exec_result.get("charts") or []
    summary = state.get("summary") or None

    # 元信息不重复:CSV 场景只保留「文件」;Excel / 数据库场景保留
    # 「原始文件」+「分析用文件」,不再重复打印同一行
    if analysis_name:
        meta_lines = [
            f"- 原始文件：{source_path}",
            f"- 分析用文件：{analysis_name}",
        ]
    else:
        meta_lines = [f"- 文件：{source_path}"]

    # 没有 summary 时(失败 / 未启用汇总),用结论文本的首行做一句话结论,
    # 保证报告结论章节不留空
    conclusion = _conclusion_text(state)
    headline_guess = conclusion.splitlines()[0].strip() if conclusion.strip() else ""

    run = {
        "run_id": state.get("run_id") or "",
        "question": user_question,
        "dataset": state.get("dataset_name") or analysis_name or source_path.name,
        "meta": meta_lines,
        "now": f"{now:%Y-%m-%d %H:%M:%S}",
        "summary": summary,
        "fallback_headline": headline_guess,
        "warnings": state.get("warnings") or [],
        "charts": charts,
        "schema": state.get("dataset_schema") or {},
        "fingerprint": state.get("dataset_fingerprint") or "",
        "sql": sql,
        "code": state.get("code") or "",
        "duration_ms": exec_result.get("duration_ms"),
        "iteration": state.get("iteration"),
    }
    return render_report(run)


def write_report(text: str, now: datetime) -> Path:
    """把报告写入 outputs/reports/report_<时间戳>.md 并返回路径。

    为什么单独用 reports/ 子目录:analyze.py 的历史 9 份 Markdown 报告和
    eval_report_*.json / result_*.json / server*.log 混在 outputs 根目录,
    与 runs/ 归档报告、traces/ 追踪文件难以区分;独立子目录后一目了然。
    """
    reports_dir = OUTPUT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"report_{now:%Y%m%d_%H%M%S}.md"
    report_path.write_text(text, encoding="utf-8")
    return report_path


def main() -> int:
    """命令行入口。

    返回：
        0 = 分析成功;1 = 分析执行失败(报告仍会生成,结论为错误分析);
        2 = 输入错误(文件不存在 / 后缀不支持 / Excel 读取失败 / sheet 不存在),
            未执行任何分析。
    """
    parser = argparse.ArgumentParser(description="分析 CSV / Excel 文件并导出 Markdown 报告")
    parser.add_argument("input", help="待分析的文件路径(.csv / .xlsx / .xls)")
    parser.add_argument("question", help="自然语言分析问题")
    parser.add_argument(
        "--sheet",
        default=None,
        help="Excel 的 sheet 名或索引(从 0 开始),默认使用第一个 sheet",
    )
    args = parser.parse_args()

    # 先解析为绝对路径,避免后续 chdir 改变相对路径的含义
    source_path = Path(args.input).expanduser()
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    source_path = source_path.resolve()

    try:
        validate_input(source_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[analyze] {exc}", file=sys.stderr)
        return 2

    # 统一以项目根目录为工作目录：报告与沙箱产物稳定落到项目 outputs/
    os.chdir(PROJECT_ROOT)

    work_dir: Path | None = None
    try:
        work_dir = create_work_dir()
        is_excel = source_path.suffix.lower() in EXCEL_SUFFIXES

        if is_excel:
            try:
                analysis_path, sheet_names, used_sheet = convert_excel_to_csv(
                    source_path, work_dir, args.sheet
                )
            except InputError as exc:
                # Excel 读取失败 / sheet 不存在：明确报错,退出码 2,不进入分析
                print(f"[analyze] {exc}", file=sys.stderr)
                return 2

            if len(sheet_names) > 1 and args.sheet is None:
                print(
                    f"[analyze] 检测到 {len(sheet_names)} 个 sheet,"
                    f"默认使用第一个：{used_sheet},可用 --sheet 指定"
                )
            print(f"[analyze] Excel 已转为 CSV: {analysis_path.name}(sheet: {used_sheet})")
        else:
            # CSV 原样复制：不转码、不改名,行为与旧版完全一致
            analysis_path = work_dir / source_path.name
            shutil.copy2(source_path, analysis_path)

        # 必须在调用 run_agent 前设置：tools / sandbox 每次调用都会重新读取该变量
        os.environ["SANDBOX_DATA_DIR"] = str(work_dir)

        # 延迟导入：确保环境变量与工作目录已就绪,且不影响参数校验阶段的快速失败
        from main import run_agent

        # 问题里带上「实际分析用的文件名」,保证 planner 能据此锁定数据集
        agent_question = f"分析 {analysis_path.name}：{args.question}"
        print(f"[analyze] 文件: {source_path}")
        print(f"[analyze] 问题: {agent_question}")

        state = run_agent(agent_question, auto_approve=True)

        now = datetime.now()
        report_path = write_report(
            build_report(
                source_path,
                args.question,
                state,
                now,
                analysis_name=analysis_path.name if is_excel else None,
            ),
            now,
        )

        final_answer = state.get("final_answer") or ""
        if final_answer:
            print(f"\n[analyze] 结论：\n{final_answer}\n")
        elif state.get("error_analysis"):
            print(f"\n[analyze] 分析失败：\n{state['error_analysis']}\n", file=sys.stderr)

        print(f"[analyze] 报告: {report_path}")

        # 成功判定与沙箱执行结果对齐：失败时仍生成报告,但返回非零退出码
        success = (
            (state.get("exec_result") or {}).get("exit_status") == "success"
            and bool(final_answer)
        )
        return 0 if success else 1
    finally:
        if work_dir is not None:
            cleanup_data_dir(work_dir)


if __name__ == "__main__":
    sys.exit(main())
