"""BYOK 请求头依赖的离线测试:解析、校验、缓存隔离、ContextVar 重置。"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

import llm
from llm import LLMSettings, current_llm, get_llm
from web.deps import (
    HEADER_BASE,
    HEADER_KEY,
    HEADER_MODEL,
    llm_override,
    parse_llm_settings,
)

KEY_A = "sk-aaaaaaaaaaaaaaaa1111"
KEY_B = "sk-bbbbbbbbbbbbbbbb2222"


def _app() -> FastAPI:
    """最小 FastAPI 应用:只暴露一个依赖 llm_override 的探针端点。"""
    app = FastAPI()

    @app.get("/probe")
    async def probe(settings: LLMSettings | None = Depends(llm_override)):
        return {
            "settings": None
            if settings is None
            else {
                "api_key": settings.api_key,
                "base_url": settings.base_url,
                "model": settings.model,
            },
            "context_set": current_llm.get() is not None,
        }

    return app


def test_three_headers_empty_returns_none():
    """三个头都为空 -> None(完全走 .env 自托管模式)。"""
    client = TestClient(_app())
    body = client.get("/probe").json()
    assert body["settings"] is None
    assert body["context_set"] is False


def test_invalid_base_url_returns_400_without_key():
    """非法 base_url -> 400,错误信息里不能出现 key 原文。"""
    client = TestClient(_app())
    resp = client.get(
        "/probe",
        headers={HEADER_KEY: KEY_A, HEADER_BASE: "ftp://evil.example"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "模型配置不合法" in detail
    assert KEY_A not in detail


def test_short_key_returns_400_without_key():
    """过短 key -> 400,且不回显 key。"""
    client = TestClient(_app())
    resp = client.get(
        "/probe",
        headers={HEADER_KEY: "short", HEADER_BASE: "https://api.example.com/v1"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "模型配置不合法" in detail
    assert "short" not in detail


def test_partial_headers_fall_back_to_env(monkeypatch):
    """只带 Model 时其余字段回落到 .env。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-envkey12345678")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")

    settings = parse_llm_settings(
        type(
            "Req",
            (),
            {"headers": {HEADER_MODEL: "request-model"}},
        )()
    )
    assert settings is not None
    assert settings.api_key == "sk-envkey12345678"
    assert settings.base_url == "https://env.example.com/v1"
    assert settings.model == "request-model"


def test_two_keys_get_different_clients():
    """缓存 key 含密钥指纹:不同 key 不会拿到同一个 client。"""
    llm.reset_llm()
    client_a = get_llm(
        LLMSettings(api_key=KEY_A, base_url="https://api.example.com/v1", model="m")
    )
    client_b = get_llm(
        LLMSettings(api_key=KEY_B, base_url="https://api.example.com/v1", model="m")
    )
    assert client_a is not client_b
    assert len(llm._LLM_CACHE) >= 2
    # 缓存 key 只存 sha256 前 8 位,不存明文 key
    for cache_key in llm._LLM_CACHE:
        assert KEY_A not in cache_key
        assert KEY_B not in cache_key
    llm.reset_llm()


def test_request_settings_override_env_client(monkeypatch):
    """请求头配置优先于 .env:ContextVar 存在时返回请求级 client,而不是 env 单例。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-envkey1234567890")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    llm.reset_llm()

    env_client = get_llm()
    request_settings = LLMSettings(
        api_key=KEY_A,
        base_url="https://request.example.com/v1",
        model="request-model",
    )
    token = current_llm.set(request_settings)
    try:
        request_client = get_llm()
    finally:
        current_llm.reset(token)

    assert request_client is not env_client
    assert str(request_client.openai_api_base).startswith("https://request.example.com")
    assert request_client.model_name == "request-model"
    # 重置后回到 env 单例,证明请求级配置没有污染全局
    assert get_llm() is env_client
    llm.reset_llm()


def test_contextvar_reset_after_request():
    """请求结束后 ContextVar 被重置:下个不带头的请求读不到上一个 Key。"""
    client = TestClient(_app())
    first = client.get(
        "/probe",
        headers={
            HEADER_KEY: KEY_A,
            HEADER_BASE: "https://api.example.com/v1",
            HEADER_MODEL: "m",
        },
    ).json()
    assert first["context_set"] is True
    assert first["settings"]["api_key"] == KEY_A

    second = client.get("/probe").json()
    assert second["context_set"] is False
    assert second["settings"] is None
    assert current_llm.get() is None
