"""POST /api/llm/test 的离线测试:成功 / 超时 / 未配置,不联网。"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.testclient import TestClient

import web.app as web_app
import web.routers.datasets as datasets_router

KEY = "sk-test1234567890abcdef"


def test_llm_test_success(monkeypatch):
    """mock ChatOpenAI 成功:返回 ok/model/latency_ms/error 结构。"""
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def invoke(self, _prompt):
            return SimpleNamespace(content="pong")

    monkeypatch.setattr(datasets_router, "ChatOpenAI", FakeClient)
    client = TestClient(web_app.app)
    resp = client.post(
        "/api/llm/test",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "deepseek-chat",
            "api_key": KEY,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model"] == "deepseek-chat"
    assert body["latency_ms"] >= 0
    assert body["error"] == ""
    # 测试请求必须极短:max_tokens=1、10 秒超时、不重试
    assert captured["max_tokens"] == 1
    assert captured["timeout"] == 10.0
    assert captured["max_retries"] == 0


def test_llm_test_timeout_returns_readable_error(monkeypatch):
    """mock 超时异常:ok=false,error 可读且不回显 key。"""

    class TimeoutClient:
        def __init__(self, **_kwargs):
            pass

        def invoke(self, _prompt):
            raise TimeoutError(f"request timed out with key {KEY}")

    monkeypatch.setattr(datasets_router, "ChatOpenAI", TimeoutClient)
    client = TestClient(web_app.app)
    resp = client.post(
        "/api/llm/test",
        json={
            "base_url": "https://api.example.com/v1",
            "model": "deepseek-chat",
            "api_key": KEY,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "timed out" in body["error"]
    assert KEY not in body["error"]
    assert "sk-***" in body["error"]


def test_llm_test_without_any_key(monkeypatch):
    """请求体与 .env 都没有 key:直接返回未配置,不构造客户端。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = TestClient(web_app.app)
    resp = client.post(
        "/api/llm/test",
        json={"base_url": "https://api.example.com/v1", "model": "m", "api_key": ""},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "未配置 API Key"
