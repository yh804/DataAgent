"""进度回调 → asyncio 队列的桥接(批次 D)。

为什么需要这个模块：
    LangGraph 图在线程池里执行,节点进度回调运行在 worker 线程;
    SSE 发送方运行在事件循环线程。两边之间必须有一个线程安全的桥,
    否则 worker 直接操作 asyncio.Queue 会触发 "bound to a different event loop"。

用法：
    bridge = EventBridge(asyncio.get_running_loop())
    run_agent(..., progress_callback=bridge.push_threadsafe)   # worker 线程里调
    async for chunk in sse_stream(bridge): ...                  # 事件循环里消费
"""

from __future__ import annotations

import asyncio
import json

# 心跳间隔:代理 / 浏览器空闲超时普遍在 30-60s,15s 足够兜底
KEEPALIVE_SECONDS = 15


def format_sse(event: str, data: dict) -> str:
    """格式化成一条 SSE 帧(event 名 + JSON data,ensure_ascii=False 保留中文)。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class EventBridge:
    """worker 线程与事件循环之间的事件队列。

    属性：
        queue: asyncio.Queue,元素为 {"event": <事件名>, "data": <dict>}。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self._cancelled = False

    def push_threadsafe(self, event: dict) -> None:
        """worker 线程调用:把事件安全地排入 asyncio 队列。

        客户端已断开(_cancelled)或事件循环已关闭时直接丢弃,
        绝不抛异常打断图执行。
        """
        if self._cancelled:
            return
        try:
            self._loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except RuntimeError:
            # 事件循环已关闭(服务退出中):丢弃即可
            pass

    def push_local(self, event: dict) -> None:
        """事件循环线程内直接入队(如 queued 事件),不走跨线程通道。"""
        if not self._cancelled:
            self.queue.put_nowait(event)

    def cancel(self) -> None:
        """标记客户端已断开:后续推送直接丢弃(worker 仍跑完,状态照常落库)。"""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


async def sse_stream(bridge: EventBridge):
    """把 bridge 里的事件转成 SSE 帧流;收到 done 事件后结束。

    每 KEEPALIVE_SECONDS 没有事件就发一行注释心跳,防止连接被中间层掐掉。
    """
    while True:
        try:
            event = await asyncio.wait_for(bridge.queue.get(), timeout=KEEPALIVE_SECONDS)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
            continue
        yield format_sse(event["event"], event["data"])
        if event["event"] == "done":
            return
