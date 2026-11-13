"""轻量可观测性：每次图运行落一份 trace,记录节点 / LLM / 工具耗时。

设计要点：
- 基于 langchain_core.callbacks.BaseCallbackHandler,挂到 graph.invoke 的 config.callbacks 上;
- 用 threading.Lock 保护共享步骤列表,保证节点内并发子调用(如并行工具)线程安全;
- 只记录元数据(名称、耗时、状态、错误、token 用量),**绝不**把 DataFrame / 全量数据
  或完整 prompt/response 写入 trace(红线);
- trace 写失败只打印警告,不阻断主流程;
- 开关：环境变量 TRACE_ENABLED(默认 true),false 时不生成 trace。

使用：
    handler = TraceCallbackHandler()
    config = {"configurable": {...}, "callbacks": [handler]}
    graph.invoke(state, config=config)
    path = write_trace(handler, thread_id, question, started_at)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# 落盘前统一脱敏:异常文本里可能带 BYOK 的 sk- 密钥,绝不能写进 trace
from llm import mask_secrets

# trace 输出目录：与 result_*.json 同属 outputs/,独立子目录便于批量查看
TRACE_DIR = Path("outputs/traces")


def trace_enabled() -> bool:
    """读取 TRACE_ENABLED 环境变量,默认开启。

    取值为 0 / false / no / off(大小写不敏感)时关闭,其余视为开启。
    关闭后 run_agent / resume_agent 不创建 TraceCallbackHandler,也不写 trace 文件。
    """
    value = os.getenv("TRACE_ENABLED", "true").strip().lower()
    return value not in ("0", "false", "no", "off")


class TraceCallbackHandler(BaseCallbackHandler):
    """记录节点 / LLM / 工具调用的耗时与状态,供落盘 trace 使用。

    线程安全：所有对 _steps / _starts 等共享结构的读写都持 _lock,
    避免 LangGraph 节点内并发子调用时出现步骤丢失或耗时错乱。

    红线：任何回调方法都不记录 inputs / outputs 的具体内容,只取名称与元数据,
    防止 DataFrame、全量数据、完整 prompt 被写入 trace。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 已完成的步骤(按回调结束顺序追加)
        self._steps: list[dict] = []
        # 进行中的调用：run_id(str) -> {name, start, parent_id}
        self._chain_starts: dict[str, dict] = {}
        self._llm_starts: dict[str, dict] = {}
        self._tool_starts: dict[str, dict] = {}
        # 正在执行的 chain 栈:用于给子调用标 parent_id(只记录父子关系,steps 保持平铺)
        self._chain_stack: list[str] = []

    # ---------------- 内部辅助 ----------------
    @staticmethod
    def _now() -> float:
        """统一用 perf_counter 计量耗时(单调时钟,不受系统时间回拨影响)。"""
        return time.perf_counter()

    @staticmethod
    def _extract_name(serialized: Any, kwargs: dict) -> str:
        """从回调的 serialized / kwargs 中尽量提取可读名称。

        优先级：kwargs["name"] > serialized["name"] > serialized["id"][-1] > "unknown"。
        LangChain 不同版本把名称放在不同位置,这里做兼容兜底。
        """
        name = kwargs.get("name")
        if name:
            return str(name)
        if isinstance(serialized, dict):
            if serialized.get("name"):
                return str(serialized["name"])
            sid = serialized.get("id")
            if isinstance(sid, (list, tuple)) and sid:
                return str(sid[-1])
        attr_name = getattr(serialized, "name", None)
        if attr_name:
            return str(attr_name)
        return "unknown"

    @staticmethod
    def _extract_llm_name(serialized: Any, kwargs: dict) -> str:
        """提取 LLM 模型名：优先用 LLM 实例的 model 字段,其次退回通用名称提取。"""
        if isinstance(serialized, dict):
            kwargs_model = serialized.get("kwargs", {})
            if isinstance(kwargs_model, dict) and kwargs_model.get("model"):
                return str(kwargs_model["model"])
        return TraceCallbackHandler._extract_name(serialized, kwargs)

    @staticmethod
    def _extract_tokens(response: Any) -> dict | None:
        """从 LLM 响应中安全提取 token 用量;任何字段缺失都返回 None,绝不抛错。

        两条路径兼容不同 LangChain 版本：
        1. response.llm_output["token_usage"](传统 OpenAI 风格);
        2. response.generations[0][0].message.usage_metadata(新版消息级用量)。
        """
        try:
            llm_output = getattr(response, "llm_output", None)
            if isinstance(llm_output, dict):
                usage = llm_output.get("token_usage")
                if isinstance(usage, dict):
                    return {
                        "prompt": usage.get("prompt_tokens"),
                        "completion": usage.get("completion_tokens"),
                        "total": usage.get("total_tokens"),
                    }
            generations = getattr(response, "generations", None) or []
            for gen_list in generations:
                for gen in gen_list or []:
                    message = getattr(gen, "message", None)
                    usage = getattr(message, "usage_metadata", None)
                    if isinstance(usage, dict):
                        return {
                            "prompt": usage.get("input_tokens"),
                            "completion": usage.get("output_tokens"),
                            "total": usage.get("total_tokens"),
                        }
        except Exception:
            # 任何提取异常都吞掉,token 用量是「锦上添花」,不能影响主流程
            return None
        return None

    def _append_step(self, step: dict) -> None:
        """加锁追加一条已完成步骤。"""
        with self._lock:
            self._steps.append(step)

    # ---------------- Chain(节点)回调 ----------------
    def on_chain_start(
        self, serialized: Any, inputs: Any, *, run_id: Any, **kwargs: Any
    ) -> None:
        name = self._extract_name(serialized, kwargs)
        rid = str(run_id)
        with self._lock:
            # 父 chain 是当前栈顶;节点内的 LLM / 工具调用都挂在所执行的节点下
            parent_id = self._chain_stack[-1] if self._chain_stack else None
            self._chain_starts[rid] = {"name": name, "start": self._now(), "parent_id": parent_id}
            self._chain_stack.append(rid)

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._finish_chain(run_id, "ok", None)

    def on_chain_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._finish_chain(run_id, "error", str(error))

    def _finish_chain(self, run_id: Any, status: str, error: str | None) -> None:
        rid = str(run_id)
        with self._lock:
            info = self._chain_starts.pop(rid, None)
            if rid in self._chain_stack:
                self._chain_stack.remove(rid)
        if info is None:
            return
        duration_ms = int((self._now() - info["start"]) * 1000)
        self._append_step(
            {
                "type": "node",
                "name": info["name"],
                # run_id 用于和 parent_id 对上号(平铺步骤间的父子关系仍可解析)
                "run_id": rid,
                "duration_ms": duration_ms,
                "status": status,
                "error": error,
                "parent_id": info.get("parent_id"),
            }
        )

    # ---------------- LLM 回调 ----------------
    def on_llm_start(
        self, serialized: Any, prompts: list[str], *, run_id: Any, **kwargs: Any
    ) -> None:
        name = self._extract_llm_name(serialized, kwargs)
        with self._lock:
            parent_id = self._chain_stack[-1] if self._chain_stack else None
            self._llm_starts[str(run_id)] = {
                "name": name,
                "start": self._now(),
                "parent_id": parent_id,
            }

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._finish_llm(run_id, "ok", None, self._extract_tokens(response))

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._finish_llm(run_id, "error", str(error), None)

    def _finish_llm(
        self, run_id: Any, status: str, error: str | None, tokens: dict | None
    ) -> None:
        rid = str(run_id)
        with self._lock:
            info = self._llm_starts.pop(rid, None)
        if info is None:
            return
        duration_ms = int((self._now() - info["start"]) * 1000)
        self._append_step(
            {
                "type": "llm",
                "name": info["name"],
                # model 单独字段:只记模型标识,绝不写 prompt / response 全文
                "model": info["name"],
                "run_id": rid,
                "duration_ms": duration_ms,
                "status": status,
                "error": error,
                "tokens": tokens,
                "parent_id": info.get("parent_id"),
            }
        )

    # ---------------- Tool 回调 ----------------
    def on_tool_start(
        self, serialized: Any, input_str: str, *, run_id: Any, **kwargs: Any
    ) -> None:
        name = self._extract_name(serialized, kwargs)
        with self._lock:
            parent_id = self._chain_stack[-1] if self._chain_stack else None
            self._tool_starts[str(run_id)] = {
                "name": name,
                "start": self._now(),
                "parent_id": parent_id,
            }

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._finish_tool(run_id, "ok", None)

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._finish_tool(run_id, "error", str(error))

    def _finish_tool(self, run_id: Any, status: str, error: str | None) -> None:
        rid = str(run_id)
        with self._lock:
            info = self._tool_starts.pop(rid, None)
        if info is None:
            return
        duration_ms = int((self._now() - info["start"]) * 1000)
        self._append_step(
            {
                "type": "tool",
                "name": info["name"],
                "run_id": rid,
                "duration_ms": duration_ms,
                "status": status,
                "error": error,
                "parent_id": info.get("parent_id"),
            }
        )

    # ---------------- 对外读取 ----------------
    def get_steps(self) -> list[dict]:
        """返回已完成步骤的快照(拷贝),调用方可以自由序列化。"""
        with self._lock:
            return list(self._steps)

    def total_tokens(self) -> int:
        """汇总已记录的所有 LLM 步骤的 token 用量(无法获取时按 0 计)。

        为什么只算 llm 步骤:节点 / 工具步骤的耗时虽含内部 LLM,
        但重复计入会让预算失真;预算只应看真实的调用成本。
        """
        total = 0
        with self._lock:
            for step in self._steps:
                if step.get("type") != "llm":
                    continue
                tokens = step.get("tokens") or {}
                value = tokens.get("total")
                if isinstance(value, (int, float)):
                    total += int(value)
        return total


def write_trace(
    handler: TraceCallbackHandler,
    thread_id: str,
    question: str,
    started_at: datetime,
) -> str | None:
    """把 handler 收集到的步骤汇总成 trace JSON 落到 outputs/traces/。

    参数：
        handler: 本次图运行挂载的 TraceCallbackHandler 实例。
        thread_id: 会话标识,用于文件名与 trace 内容关联。
        question: 本轮用户问题(resume_agent 时可能为空串)。
        started_at: 图运行开始时间(datetime),用于计算总耗时与 started_at 字段。

    返回：
        写入成功返回 trace 文件绝对路径字符串;TRACE_ENABLED=false 或写失败返回 None。

    红线：trace 内容只含元数据(名称 / 耗时 / 状态 / token),不含 DataFrame 或全量数据。
    写失败只打印警告,不抛异常、不阻断主流程。
    """
    if not trace_enabled():
        return None

    steps = handler.get_steps()
    summary = {
        "node_count": sum(1 for s in steps if s["type"] == "node"),
        "llm_calls": sum(1 for s in steps if s["type"] == "llm"),
        "tool_calls": sum(1 for s in steps if s["type"] == "tool"),
    }
    finished_at = datetime.now()
    # total_ms 用墙钟差值:节点耗时已含其内部 LLM / 工具耗时,求和会重复计时
    total_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)
    # sum_duration_ms 仅作参考:各步骤耗时之和(会因父子嵌套重复,不能当墙钟用)
    sum_duration_ms = sum(int(s.get("duration_ms") or 0) for s in steps)

    trace = {
        "thread_id": thread_id,
        "question": question,
        # 时间戳保留微秒:保证外部用 finished_at − started_at 复算 total_ms 完全一致
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        # wall time:finished_at − started_at
        "total_ms": total_ms,
        # 步骤耗时求和:节点耗时已含其内部 LLM / 工具耗时,求和会重复
        "sum_duration_ms": sum_duration_ms,
        "total_tokens": handler.total_tokens(),
        "steps": steps,
        "summary": summary,
    }

    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = finished_at.strftime("%Y%m%d_%H%M%S")
        # 文件名含 thread_id 前 8 位,便于在多会话并发时区分
        short_tid = (thread_id or "unknown")[:8]
        path = TRACE_DIR / f"trace_{timestamp}_{short_tid}.json"
        # 序列化后整体脱敏:步骤里的 error 可能原样携带 sk- 密钥
        path.write_text(
            mask_secrets(json.dumps(trace, ensure_ascii=False, indent=2)),
            encoding="utf-8",
        )
        return str(path.resolve())
    except Exception as exc:
        # 写失败只警告,不阻断主流程(trace 是观测能力,不是业务能力)
        print(f"[trace] 写入失败: {mask_secrets(f'{type(exc).__name__}: {exc}')}")
        return None
