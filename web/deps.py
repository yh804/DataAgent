"""请求级模型配置依赖(BYOK)。

优先级:请求头 > .env。
- X-LLM-Key / X-LLM-Base / X-LLM-Model 都可选,缺省字段回落到 .env;
- 三个都为空时返回 None,完全走自托管模式;
- 配置只写入 ContextVar,请求结束立即重置,服务端不落盘、不记日志。

密钥安全:
- 校验错误信息绝不回显 key;
- 万一异常文本里带了 key,统一用 mask_secrets 脱敏。
"""

from __future__ import annotations

import os
from typing import AsyncIterator

from fastapi import HTTPException, Request

from llm import LLMSettings, current_llm, mask_secrets

HEADER_KEY = "X-LLM-Key"
HEADER_BASE = "X-LLM-Base"
HEADER_MODEL = "X-LLM-Model"

MAX_BASE_URL_LEN = 200
MAX_MODEL_LEN = 64
MIN_KEY_LEN = 8
MAX_KEY_LEN = 256


def _invalid(reason: str) -> HTTPException:
    """构造 400 错误;reason 里只放规则说明,绝不含密钥本身。"""
    return HTTPException(status_code=400, detail=f"模型配置不合法: {reason}")


def parse_llm_settings(request: Request) -> LLMSettings | None:
    """解析并校验请求头里的模型配置。

    Args:
        request: FastAPI 请求对象。

    Returns:
        LLMSettings(缺省字段用 .env 补齐);三个头都为空时返回 None。

    Raises:
        HTTPException: 400,配置不合法;错误信息不回显 key。
    """
    api_key = (request.headers.get(HEADER_KEY) or "").strip()
    base_url = (request.headers.get(HEADER_BASE) or "").strip()
    model = (request.headers.get(HEADER_MODEL) or "").strip()

    if not (api_key or base_url or model):
        return None

    if base_url and not (
        base_url.startswith(("http://", "https://"))
        and len(base_url) <= MAX_BASE_URL_LEN
    ):
        raise _invalid(
            f"Base URL 必须以 http:// 或 https:// 开头且不超过 {MAX_BASE_URL_LEN} 字符"
        )
    if len(model) > MAX_MODEL_LEN:
        raise _invalid(f"Model 长度不能超过 {MAX_MODEL_LEN} 字符")
    if api_key and not (MIN_KEY_LEN <= len(api_key) <= MAX_KEY_LEN):
        raise _invalid(f"API Key 长度需在 {MIN_KEY_LEN}~{MAX_KEY_LEN} 字符之间")

    return LLMSettings(
        api_key=api_key or (os.getenv("LLM_API_KEY") or "").strip(),
        base_url=base_url or (os.getenv("LLM_BASE_URL") or "").strip(),
        model=model or (os.getenv("LLM_MODEL") or "").strip(),
    )


async def llm_override(request: Request) -> AsyncIterator[LLMSettings | None]:
    """FastAPI 依赖:把请求级配置写入 ContextVar,请求结束自动重置。

    Args:
        request: FastAPI 请求对象。

    Yields:
        LLMSettings 或 None;图执行期间 get_llm() 会优先读取它。
    """
    try:
        settings = parse_llm_settings(request)
    except HTTPException as exc:
        # 双保险:规则说明本就不含 key,这里再脱敏一次,防止未来改动引入泄露
        exc.detail = mask_secrets(exc.detail)
        raise

    if settings is None:
        yield None
        return

    token = current_llm.set(settings)
    try:
        yield settings
    finally:
        current_llm.reset(token)
