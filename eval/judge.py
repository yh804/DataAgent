"""LLM-as-judge:对 report.md 的汇总正文打分(默认关闭,由 run_eval --judge 触发)。

设计原则:
- 只读报告正文,不接触数据 / 代码,避免「裁判」被实现细节带偏;
- 三个维度各打 1–5 分并给理由(正确性 / 完整性 / 可读性);
- 任何失败都返回 None,绝不改变 eval 的 PASS/FAIL(判分失败只影响均分展示)。

用法:
    python eval/judge.py outputs/runs/<run_id>/report.md
    python eval/run_eval.py --judge
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from llm import get_llm  # noqa: E402  (sys.path 调整后再导入)

JUDGE_SYSTEM_PROMPT = """你是数据分析报告的评审员。请只根据用户给出的报告摘录打分,不要臆测未出现的数据。
评分维度(每项 1–5 分,5 分最好):
- 正确性(correctness)：结论与报告中给出的数字是否自洽,有无明显自相矛盾或超出证据的判断;
- 完整性(completeness)：是否给出结论 + 关键发现 + 建议 + 局限,关键数字是否有口径说明;
- 可读性(readability)：结构清晰、措辞准确、无空话套话。
若报告摘录为空或几乎没有结论,三项都给 1 分并说明原因。"""

JUDGE_USER_TEMPLATE = """【用户问题】
{question}

【报告摘录】
{body}

请按维度打分并给出简短理由。"""


class JudgeScore(BaseModel):
    """单份报告的评审得分(1–5 分)。"""

    correctness: int = Field(ge=1, le=5, description="正确性 1–5 分")
    completeness: int = Field(ge=1, le=5, description="完整性 1–5 分")
    readability: int = Field(ge=1, le=5, description="可读性 1–5 分")
    reason: str = Field(default="", description="一句话说明扣分 / 加分的主要依据")


def summary_body(report_text: str) -> str:
    """取报告「## 一句话结论」到「## 图表」之间的汇总正文。

    与 run_eval 的口径保持一致:只评汇总质量,不把附录 / 生成代码 / 数据版本算进去。
    """
    start = report_text.find("## 一句话结论")
    if start == -1:
        return ""
    end = report_text.find("## 图表", start)
    return report_text[start:end if end != -1 else len(report_text)]


def judge_report(report_text: str, question: str = "") -> dict | None:
    """对报告正文打分,返回 {correctness, completeness, readability, average, reason}。

    参数:
        report_text: report.md 全文。
        question: 原始问题(帮助裁判判断结论是否切题)。

    返回:
        得分 dict;正文为空 / LLM 调用失败 / 解析失败时返回 None(不影响 PASS/FAIL)。
    """
    body = summary_body(report_text).strip()
    if not body:
        return None
    try:
        score: JudgeScore = (
            get_llm()
            .with_structured_output(JudgeScore, method="function_calling")
            .invoke(
                [
                    ("system", JUDGE_SYSTEM_PROMPT),
                    ("human", JUDGE_USER_TEMPLATE.format(question=question or "(未提供)", body=body)),
                ]
            )
        )
    except Exception as exc:
        print(f"[judge] 判分失败(不影响 PASS/FAIL): {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if score is None:
        return None

    result = score.model_dump()
    result["average"] = round(
        (result["correctness"] + result["completeness"] + result["readability"]) / 3, 2
    )
    return result


def main() -> int:
    """命令行入口：对指定 report.md 打分并输出 JSON。

    返回：
        0 = 打分成功;1 = 报告不存在;2 = 判分失败(不影响报告本身)。
    """
    parser = argparse.ArgumentParser(description="LLM-as-judge 报告评分")
    parser.add_argument("report", help="report.md 路径,或 outputs/runs/<run_id> 目录")
    parser.add_argument("--question", default="", help="原始用户问题(可选)")
    args = parser.parse_args()

    target = Path(args.report)
    if target.is_dir():
        target = target / "report.md"
    if not target.is_file():
        print(f"[judge] 报告不存在: {target}", file=sys.stderr)
        return 1

    score = judge_report(target.read_text(encoding="utf-8"), args.question)
    if score is None:
        print("[judge] 判分失败(报告为空或 LLM 不可用)", file=sys.stderr)
        return 2

    print(json.dumps(score, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
