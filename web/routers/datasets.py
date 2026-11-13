"""数据源相关端点: 首页、健康检查、数据集列表与上传、模型连通性测试。"""

from __future__ import annotations

import time

import web.app as A
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from langchain_openai import ChatOpenAI
from llm import mask_secrets
from pydantic import BaseModel

router = APIRouter()

# 连通性测试固定 10 秒超时、只生成 1 个 token,避免测试本身消耗额度或长时间挂起
LLM_TEST_TIMEOUT = 10.0
LLM_TEST_MAX_TOKENS = 1
MAX_BASE_URL_LEN = 200


class LlmTestIn(BaseModel):
    """模型连通性测试请求体;api_key 为空时回落到 .env。"""

    base_url: str = ""
    model: str = ""
    api_key: str = ""


@router.get("/")
async def index():
    # 首页禁止缓存:HTML 结构变了而浏览器仍用旧页面时,
    # 新版 app.js 会去操作已不存在的节点,直接报 null
    return A.FileResponse(
        A.STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )

@router.get("/api/health")
async def health(request: Request):
    # docker 探测有子进程调用,放线程池避免阻塞事件循环
    docker = await A.run_in_threadpool(A._docker_ok)
    # BYOK:请求头里的 key 优先于 .env;两者都没有才算未配置
    request_key = (request.headers.get("X-LLM-Key") or "").strip()
    env_key = (A.os.getenv("LLM_API_KEY") or "").strip()
    if request_key:
        llm_source = "request"
    elif env_key:
        llm_source = "env"
    else:
        llm_source = "none"
    return {
        "llm_configured": llm_source != "none",
        "llm_source": llm_source,
        "docker": docker,
        "checkpointer": A._checkpointer_kind(),
        "checkpoint_path": A.os.getenv("CHECKPOINT_PATH") or A.checkpoint_mod.DEFAULT_CHECKPOINT_PATH,
        "summary_enabled": A.agent_main._summary_enabled(),
    }


@router.post("/api/llm/test")
async def llm_test(payload: LlmTestIn):
    """用一次极小的补全请求测试模型连通性(不落盘、不回显密钥)。"""
    base_url = (payload.base_url or A.os.getenv("LLM_BASE_URL") or "").strip()
    model = (payload.model or A.os.getenv("LLM_MODEL") or "").strip()
    api_key = (payload.api_key or A.os.getenv("LLM_API_KEY") or "").strip()

    if not api_key:
        return {"ok": False, "model": model, "latency_ms": 0, "error": "未配置 API Key"}
    if base_url and not (
        base_url.startswith(("http://", "https://"))
        and len(base_url) <= MAX_BASE_URL_LEN
    ):
        return {
            "ok": False,
            "model": model,
            "latency_ms": 0,
            "error": f"模型配置不合法: Base URL 必须以 http:// 或 https:// 开头且不超过 {MAX_BASE_URL_LEN} 字符",
        }

    started = time.perf_counter()
    try:
        client = ChatOpenAI(
            base_url=base_url or None,
            api_key=api_key,
            model=model,
            temperature=0,
            timeout=LLM_TEST_TIMEOUT,
            max_retries=0,
            max_tokens=LLM_TEST_MAX_TOKENS,
        )
        await A.run_in_threadpool(client.invoke, "ping")
    except Exception as exc:  # noqa: BLE001 - 测试端点只返回可读原因
        latency_ms = int((time.perf_counter() - started) * 1000)
        reason = mask_secrets(str(exc)).strip() or exc.__class__.__name__
        return {"ok": False, "model": model, "latency_ms": latency_ms, "error": reason}
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {"ok": True, "model": model, "latency_ms": latency_ms, "error": ""}

@router.get("/api/datasets")
async def list_datasets_api():
    data_dir = A._data_dir()
    items: list[dict] = []
    if data_dir.is_dir():
        for path in data_dir.glob("*.csv"):
            if not path.is_file():
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "mtime": A.datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                }
            )
    # 按修改时间倒序:最近更新的数据集排最前
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items

@router.post("/api/datasets")
async def upload_dataset(file: A.UploadFile = A.File(...)):
    raw_name = file.filename or ""
    stem, suffix = A._sanitize_stem(raw_name)
    if suffix not in A.UPLOAD_SUFFIXES:
        raise A.HTTPException(
            status_code=400,
            detail=f"只支持 {' / '.join(sorted(A.UPLOAD_SUFFIXES))} 文件",
        )

    # 先读入内存再判断大小:上限 20MB,单用户本地工具无需流式落盘
    content = await file.read(A.MAX_UPLOAD_BYTES + 1)
    if len(content) > A.MAX_UPLOAD_BYTES:
        raise A.HTTPException(status_code=400, detail="文件超过 20MB 上限")
    if not content:
        raise A.HTTPException(status_code=400, detail="文件为空")

    data_dir = A._data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        names = A._write_upload_as_csv(content, stem, suffix, data_dir)
    except Exception as exc:
        # 解析失败(损坏文件/编码异常)必须回 400,不能把半成品写进数据目录
        raise A.HTTPException(status_code=400, detail=f"文件解析失败:{exc}") from exc
    if not names:
        raise A.HTTPException(status_code=400, detail="文件里没有可用的数据表")
    return {"name": names[0], "names": names}
