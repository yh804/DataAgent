"""图路由函数的真值表测试(纯字典 state,不运行图、不联网)。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import route_after_code_analyst, route_after_review  # noqa: E402


def test_route_after_review_approve():
    assert route_after_review({"plan_approved": True}) == "analyze"


def test_route_after_review_reject_or_invalid():
    assert route_after_review({"plan_approved": False}) == "end"
    assert route_after_review({}) == "end"


def test_route_after_code_analyst_non_analyze_ends():
    # discover / clarify 不需要汇总,直接结束
    assert route_after_code_analyst({"next_action": "discover"}) == "end"
    assert route_after_code_analyst({"next_action": "clarify"}) == "end"


def test_route_after_code_analyst_success_summarizes():
    state = {
        "next_action": "analyze",
        "exec_result": {"exit_status": "success"},
        "iteration": 1,
    }
    assert route_after_code_analyst(state) == "summarize"


def test_route_after_code_analyst_retryable_failure_retries():
    state = {
        "next_action": "analyze",
        "exec_result": {"exit_status": "error"},
        "error_analysis": "错误类型：代码执行失败(可重试)",
        "iteration": 1,
    }
    assert route_after_code_analyst(state) == "retry"


def test_route_after_code_analyst_non_retryable_ends():
    state = {
        "next_action": "analyze",
        "exec_result": {"exit_status": "error"},
        "error_analysis": "错误类型：数据集不可用(不可重试)",
        "iteration": 1,
    }
    assert route_after_code_analyst(state) == "end"


def test_route_after_code_analyst_retry_limit_ends():
    state = {
        "next_action": "analyze",
        "exec_result": {"exit_status": "error"},
        "error_analysis": "错误类型：代码执行失败(可重试)",
        "iteration": 3,
    }
    assert route_after_code_analyst(state) == "end"
