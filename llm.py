"""LLM 接入层：OpenAI 兼容客户端 + 结构化输出模型。

配置全部来自环境变量(可用 .env 文件注入)：
- LLM_BASE_URL：OpenAI 兼容接口地址(DeepSeek / Moonshot / 本地 vLLM 等均可)
- LLM_API_KEY：接口密钥
- LLM_MODEL：模型名称
- LLM_TIMEOUT：单次请求超时秒数(默认 60)
- LLM_MAX_RETRIES：失败重试次数(默认 1)

设计要点：
- temperature=0：代码生成与意图识别要求确定性,避免同一问题产出漂移;
- 结构化输出用 pydantic 模型约束,LLM 返回非法 JSON 时由 langchain 抛错,
  调用方捕获后走降级逻辑,绝不让脏数据进入图状态。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

# 模块导入即加载 .env：保证无论谁第一个导入本模块,环境变量都已就位
load_dotenv()

# LLM 客户端单例：复用 httpx2/httpx 连接池,避免每次 get_llm() 新建 client
# 造成重复 TLS 握手。线程锁保护首次初始化,reset_llm() 供测试隔离使用。
_LLM: ChatOpenAI | None = None
_LLM_LOCK = threading.Lock()

# BYOK 客户端缓存:按 base_url / model / key 指纹 等维度隔离,
# 避免不同用户共用同一个 client 而串用密钥。上限 8 个,超出丢最旧。
_LLM_CACHE: "OrderedDict[tuple, ChatOpenAI]" = OrderedDict()
_LLM_CACHE_MAX = 8

# 密钥脱敏:anywhere 出现形如 sk-xxxxxxxx 的串都替换掉,防止异常文本落盘泄露
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{8,}")


def mask_secrets(text: Any) -> str:
    """把文本里的 API Key 形态串替换成 sk-***。

    Args:
        text: 任意文本(异常信息 / 日志 / 落盘 JSON)。

    Returns:
        脱敏后的字符串。
    """
    return _KEY_PATTERN.sub("sk-***", str(text or ""))


@dataclass(frozen=True)
class LLMSettings:
    """请求级模型配置(BYOK);为空字符串表示该字段回落到 .env。"""

    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0
    max_retries: int = 1


# 请求级配置:由 web/deps.py 在请求开始时 set,请求结束时 reset
current_llm: ContextVar[LLMSettings | None] = ContextVar(
    "current_llm", default=None
)


class PlannerDecision(BaseModel):
    """planner 节点的结构化决策结果。

    intent 取值即图的路由动作;dataset_name 允许为空字符串表示「未锁定」;
    plan 是给用户/后续节点看的自然语言步骤;reason 记录决策依据,便于审计回放。
    """

    intent: Literal["discover", "analyze", "clarify"] = Field(
        description="用户意图：discover=查看有哪些数据;analyze=对某个数据集做分析;clarify=意图不明需要反问"
    )
    dataset_name: str = Field(
        default="", description="要分析的数据集文件名(如 orders.csv),未提及则为空字符串"
    )
    plan: list[str] = Field(
        default_factory=list, description="完成该问题的分析步骤清单(自然语言,按执行顺序)"
    )
    reason: str = Field(default="", description="做出该意图判断的依据,一句话说明")


class GeneratedCode(BaseModel):
    """code_analyst 节点的结构化代码生成结果。"""

    code: str = Field(description="可直接在沙箱中运行的完整 Python 脚本")
    explanation: str = Field(description="这段代码的分析思路,一两句话,面向用户")


class GeneratedSQL(BaseModel):
    """数据库问答入口的结构化 SQL 生成结果。"""

    sql: str = Field(description="只读的单条 SQLite SELECT / WITH 查询语句,不要带分号")
    explanation: str = Field(description="这条 SQL 的查询思路,一两句话,面向用户")


class UnknownTerm(BaseModel):
    """无法从表结构 / 术语表确定含义的业务词。"""

    term: str = Field(description="用户问题里无法确定口径的业务词")
    reason: str = Field(default="", description="为什么无法从现有表结构与术语表确定,一句话")
    candidates: list[str] = Field(
        default_factory=list,
        description="最多 3 个最可能的字段或口径,写成 表名.列名;不确定就留空",
    )


class GlossaryCheck(BaseModel):
    """SQL 生成前的术语检查结果;没有未知词时 unknown_terms 为空。"""

    unknown_terms: list[UnknownTerm] = Field(
        default_factory=list, description="最多 3 个无法确定的业务词;没有就返回空列表"
    )


class StatsPlan(BaseModel):
    """统计分析入口的方法选择结果(LLM 只选方法 + 填参数,不写计算代码)。

    method 限定为 stats.py 提供的确定性方法;无法判断时返回 clarify 让用户补充信息。
    params 的键名必须与对应 stats 函数的参数一致(如 group_col / value_col / xs)。
    """

    method: Literal[
        "describe_stats",
        "ttest",
        "chi_square",
        "anova",
        "correlation",
        "linear_regression",
        "ab_sample_size",
        "clarify",
    ] = Field(description="选定的统计方法;信息不足时选 clarify")
    params: dict[str, Any] = Field(
        default_factory=dict, description="调用该方法的参数,键名与函数签名一致"
    )
    reason: str = Field(default="", description="选择该方法的依据,一句话说明")


class StatsInterpretation(BaseModel):
    """统计结果的文字解读(LLM 只解释,不参与计算)。"""

    conclusion: str = Field(description="基于计算结果的核心结论,必须区分「显著」与「重要」")
    assumptions: str = Field(description="假设是否满足：正态性 / 方差齐性 / 期望频数等")
    caveats: str = Field(description="结论的局限、不能外推的范围、p 值的正确理解")
    next_steps: list[str] = Field(
        default_factory=list, description="建议的后续验证或补充分析(2~4 条)"
    )


class ReportSummary(BaseModel):
    """分析结果的汇总报告(LLM 只做归纳,不得产生新数字)。

    硬约束(写进每个字段的 description,让模型在生成时持续看到):
    只能引用给定执行结果中的数字;结果里没有的数字必须留空,禁止推算 / 估算 / 四舍五入改写。
    """

    headline: str = Field(
        description="一句话结论,必须包含一个来自执行结果的关键数字;"
        "只能引用给定结果中的数字,禁止推算,没有可用数字就留空"
    )
    findings: list[str] = Field(
        default_factory=list,
        description="2~4 条关键发现,每条都必须含一个来自执行结果的数字;"
        "只能引用给定结果中的数字,禁止推算,没有就留空",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="2~3 条可执行的后续建议(基于上述发现,不含新数字);"
        "只能引用给定结果中的数字,禁止推算",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="至少 2 条局限 / 口径说明(如缺失值处理、样本代表性、p 值误读);"
        "只能引用给定结果中的数字,禁止推算",
    )
    method: str = Field(
        default="",
        description="口径与方法:分组怎么分、缺失怎么处理、统计量如何计算;"
        "只能引用给定结果中的数字,禁止推算",
    )
    followups: list[str] = Field(
        default_factory=list,
        description="基于用户问题和本次结论,用户最可能继续追问的 3 个问题;"
        "每条不超过 25 个汉字;必须具体(带列名/维度/指标);不要重复原问题",
    )


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量,非法值退回默认(配置错误不应让进程崩溃)。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[llm] 警告: {name}={raw!r} 不是数字,使用默认值 {default}")
        return default


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量,非法值退回默认。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[llm] 警告: {name}={raw!r} 不是整数,使用默认值 {default}")
        return default


def _build_http_client():
    """构建共享的 http client(仅用于绕开本机 brotli 兼容问题)。

    Returns:
        httpx2.Client(锁 gzip 编码)或 None(未安装 httpx2 时用 SDK 默认)。
    """
    try:
        import httpx2

        return httpx2.Client(headers={"Accept-Encoding": "gzip, identity"})
    except ImportError:
        return None


def _build_client(
    base_url: str | None,
    api_key: str | None,
    model: str,
    timeout: float,
    max_retries: int,
) -> ChatOpenAI:
    """按给定参数构造 ChatOpenAI(temperature=0)。"""
    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=0,
        timeout=timeout,
        max_retries=max_retries,
        http_client=_build_http_client(),
    )


def _cached_client(settings: LLMSettings) -> ChatOpenAI:
    """按 BYOK 配置取缓存客户端;缓存 key 含密钥指纹,不同用户互不串用。"""
    key = (
        settings.base_url,
        settings.model,
        hashlib.sha256(settings.api_key.encode("utf-8")).hexdigest()[:8],
        settings.timeout,
        settings.max_retries,
    )
    with _LLM_LOCK:
        cached = _LLM_CACHE.get(key)
        if cached is not None:
            # 命中后移到队尾,保证淘汰顺序是最久未使用
            _LLM_CACHE.move_to_end(key)
            return cached
        client = _build_client(
            settings.base_url or None,
            settings.api_key or None,
            settings.model or "",
            settings.timeout,
            settings.max_retries,
        )
        _LLM_CACHE[key] = client
        while len(_LLM_CACHE) > _LLM_CACHE_MAX:
            _LLM_CACHE.popitem(last=False)
        return client


def get_llm(settings: LLMSettings | None = None) -> ChatOpenAI:
    """返回 ChatOpenAI(temperature=0);支持环境变量与请求级 BYOK 两种模式。

    Args:
        settings: 请求级模型配置。None 时优先读 ContextVar current_llm,
            仍为空才回落到环境变量(自托管模式,行为与改造前完全一致)。

    环境变量(仅在 settings 与 ContextVar 都为空时生效):
        LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_TIMEOUT / LLM_MAX_RETRIES。

    Returns:
        temperature=0 的 ChatOpenAI。env 模式复用进程级单例;
        BYOK 模式按 (base_url, model, key 指纹, timeout, retries) 缓存,
        最多 8 个,超出丢最旧,避免不同用户串用 client。
    """
    if settings is None:
        settings = current_llm.get()

    if settings is None:
        global _LLM
        if _LLM is not None:
            return _LLM
        with _LLM_LOCK:
            # 锁内再次检查:防止多个线程同时通过锁外检查后重复初始化
            if _LLM is None:
                _LLM = _build_client(
                    os.getenv("LLM_BASE_URL"),
                    os.getenv("LLM_API_KEY"),
                    os.getenv("LLM_MODEL", ""),
                    _env_float("LLM_TIMEOUT", 60.0),
                    _env_int("LLM_MAX_RETRIES", 1),
                )
        return _LLM

    return _cached_client(settings)


def reset_llm() -> None:
    """清空 LLM 客户端缓存(测试用)。

    同时清空 env 单例与 BYOK 缓存,避免测试间配置互相污染。
    生产代码不应调用本函数。
    """
    global _LLM
    with _LLM_LOCK:
        _LLM = None
        _LLM_CACHE.clear()
