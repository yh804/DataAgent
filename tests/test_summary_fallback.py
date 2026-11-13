"""summarize 节点的降级测试(批次 B)。

覆盖两件事:
1. LLM 汇总抛异常时,summarize 必须回退到模板,且 final_answer 非空、含「原始输出」、
   report.md 仍然生成;
2. SUMMARY_ENABLED=false 时,不调 LLM,final_answer 保持旧格式(不含「关键发现」),
   report.md 仍然生成。

为什么直接调用 summarize_node 而不是跑整图:
    整图需要 Docker 沙箱 + 真实 LLM 才能走到成功分支,单测会变得又慢又不可控;
    summarize 的输入就是 analyze 成功后的 state,构造等价 state 即可精确测降级路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402  (sys.path 调整后再导入)
import nodes.summarize as summarize_mod  # noqa: E402  (patch 点: 该模块自己的 get_llm)


def _make_state(run_id: str) -> dict:
    """构造 analyze 成功后的最小 state(结构与真实运行保持一致)。"""
    schema = {
        "name": "测试数据.csv",
        "columns": [
            {"name": "分组", "dtype": "object", "null_ratio": 0.0},
            {"name": "收入", "dtype": "float64", "null_ratio": 0.0},
        ],
        "row_count": 800,
        "encoding": "utf-8",
        "size_bytes": 12345,
        "mtime_iso": "2026-01-01T00:00:00",
        "sha256": "a" * 64,
    }
    return {
        "question": "统计一下 测试数据.csv 中测试组和对照组的平均收入",
        "dataset_name": "测试数据.csv",
        "plan": ["读取数据", "按分组计算平均收入"],
        "dataset_schema": schema,
        "dataset_fingerprint": "测试数据.csv@aaaaaaaaaaaa",
        "run_id": run_id,
        "code": "import pandas as pd\nprint('ok')",
        "exec_result": {
            "stdout": "行数: 800\n收入均值: 68.3",
            "stderr": "",
            "charts": [],
            "files": [],
            "duration_ms": 120,
            "exit_status": "success",
        },
        "iteration": 1,
        # _extract_explanation 从 [code_analyst] 消息里解析「分析思路」
        "messages": [
            {
                "content": (
                    "[code_analyst] 执行成功(120ms,第 1 次尝试)。\n"
                    "分析思路：按分组计算平均收入\n结果：\n行数: 800"
                )
            }
        ],
    }


def _raise_llm(*_args, **_kwargs):
    """模拟 LLM 不可用:任何调用都抛异常。"""
    raise RuntimeError("llm down")


def test_summary_llm_failure_falls_back(monkeypatch, tmp_path):
    """LLM 汇总失败时:模板降级 + final_answer 非空 + report.md 生成。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUMMARY_ENABLED", raising=False)
    monkeypatch.setattr(summarize_mod, "get_llm", _raise_llm)

    run_id = "20260101_000000_fb0001"
    result = main.summarize_node(
        _make_state(run_id), {"configurable": {"thread_id": "t-fallback"}}
    )

    assert result["final_answer"], "final_answer 不允许为空"
    assert "原始输出" in result["final_answer"]
    assert any("LLM 汇总不可用" in item for item in result["warnings"])

    report_path = tmp_path / "outputs" / "runs" / run_id / "report.md"
    assert report_path.is_file(), "降级时 report.md 仍必须生成"
    assert (report_path.parent / "result.json").is_file()


def test_previous_turn_context_includes_summarize():
    """多轮上下文:下一轮必须能看到 [summarize] 的结论(而不是只认 [code_analyst])。"""
    from langchain_core.messages import AIMessage, HumanMessage

    state = {
        "messages": [
            HumanMessage("统计 测试数据.csv 的平均收入"),
            AIMessage("[code_analyst] 执行成功(120ms,第 1 次尝试)。\n分析思路：计算均值"),
            AIMessage("[summarize] 结论：收入均值 68.3"),
            HumanMessage("那按分组再看一次"),
        ]
    }
    previous_question, previous_answer = main._extract_previous_turn_context(state)
    assert previous_question == "统计 测试数据.csv 的平均收入"
    assert previous_answer.startswith("[summarize]")


def test_summary_disabled_keeps_legacy_answer(monkeypatch, tmp_path):
    """SUMMARY_ENABLED=false:不调 LLM,final_answer 保持旧格式且不含「关键发现」。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUMMARY_ENABLED", "false")
    # 若关闭开关后仍调 LLM,这里会直接抛出异常导致用例失败
    monkeypatch.setattr(summarize_mod, "get_llm", _raise_llm)

    run_id = "20260101_000000_fb0002"
    result = main.summarize_node(
        _make_state(run_id), {"configurable": {"thread_id": "t-disabled"}}
    )

    assert result["final_answer"], "关闭汇总时 final_answer 也不允许为空"
    assert "关键发现" not in result["final_answer"]
    assert result["summary"] == {}
    assert (tmp_path / "outputs" / "runs" / run_id / "report.md").is_file()
