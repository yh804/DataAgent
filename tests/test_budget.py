"""token 预算守卫的单元测试(第 2 次 LLM 调用前必须被拦截)。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from trace import TraceCallbackHandler  # noqa: E402


class _FakeResponse:
    """模拟 LangChain 的 LLMResult(只带 token_usage 字段)。"""

    def __init__(self, total_tokens: int) -> None:
        self.llm_output = {
            "token_usage": {
                "prompt_tokens": total_tokens // 2,
                "completion_tokens": total_tokens - total_tokens // 2,
                "total_tokens": total_tokens,
            }
        }
        self.generations = []


def test_budget_blocks_second_llm_call(monkeypatch):
    """预算=1:第 1 次调用前允许,记录用量后第 2 次调用前被拦截。"""
    monkeypatch.setenv("SESSION_TOKEN_BUDGET", "1")
    handler = TraceCallbackHandler()
    config = {"callbacks": [handler]}

    # 第 1 次调用前:尚未产生用量,允许发起
    assert main._budget_blocked(config) is False

    # 模拟第 1 次 LLM 调用完成(记录 10 tokens)
    handler.on_llm_start({}, ["prompt"], run_id="llm-1")
    handler.on_llm_end(_FakeResponse(10), run_id="llm-1")

    assert main._used_tokens(config) == 10
    # 第 2 次调用前:已达上限,必须拦截
    assert main._budget_blocked(config) is True

    error = main._budget_error(config)
    assert "预算超限(不可重试)" in error


def test_budget_zero_means_unlimited(monkeypatch):
    """SESSION_TOKEN_BUDGET=0(默认)表示不限,token 再高也不拦截。"""
    monkeypatch.delenv("SESSION_TOKEN_BUDGET", raising=False)
    handler = TraceCallbackHandler()
    handler.on_llm_start({}, ["prompt"], run_id="llm-1")
    handler.on_llm_end(_FakeResponse(1_000_000), run_id="llm-1")

    assert main._token_budget() == 0
    assert main._budget_blocked({"callbacks": [handler]}) is False


def test_total_tokens_ignores_non_llm_steps():
    """只有 llm 步骤计入 token 用量,节点 / 工具步骤不重复统计。"""
    handler = TraceCallbackHandler()
    handler.on_chain_start({"name": "node"}, {}, run_id="chain-1")
    handler.on_llm_start({}, ["p"], run_id="llm-1")
    handler.on_llm_end(_FakeResponse(20), run_id="llm-1")
    handler.on_chain_end({}, run_id="chain-1")

    assert handler.total_tokens() == 20


class _FakeCallbackManager:
    """模拟 LangChain 在节点内传下来的 CallbackManager(.handlers 是列表)。"""

    def __init__(self, handlers: list) -> None:
        self.handlers = handlers


def test_used_tokens_supports_callback_manager(monkeypatch):
    """节点内 config["callbacks"] 是 CallbackManager 而非 list,也必须能统计。"""
    monkeypatch.setenv("SESSION_TOKEN_BUDGET", "1")
    handler = TraceCallbackHandler()
    handler.on_llm_start({}, ["p"], run_id="llm-1")
    handler.on_llm_end(_FakeResponse(10), run_id="llm-1")

    config = {"callbacks": _FakeCallbackManager([handler])}
    assert main._used_tokens(config) == 10
    assert main._budget_blocked(config) is True
