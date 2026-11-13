"""检查点后端装配：LangGraph 状态持久化的唯一入口。

为什么单独成模块：
    构建规则 2.3 要求 checkpointer 可持久化、可恢复;把后端选择与降级策略
    收敛到一个函数,main.py 只负责「拿来用」,不关心是 SQLite 还是内存。

配置(环境变量)：
    CHECKPOINT_BACKEND: sqlite(默认) / memory。
    CHECKPOINT_PATH: SQLite 文件路径,默认 outputs/checkpoints.sqlite。

降级策略(只打印一次警告,绝不崩溃)：
    - 未安装 langgraph-checkpoint-sqlite → MemorySaver;
    - SQLite 文件目录无写权限 / 打开失败 → MemorySaver;
    降级后进程内功能不变,但进程退出后检查点丢失(与旧行为一致)。
"""

from __future__ import annotations

import atexit
import os
import sqlite3
from pathlib import Path

# 降级警告只打印一次:build_graph 被缓存为单例,正常每进程只调一次,
# 但若未来有人绕开缓存重复调用,也不应刷屏
_warned = False

DEFAULT_CHECKPOINT_PATH = "outputs/checkpoints.sqlite"


def _warn(message: str) -> None:
    """打印一次降级警告(重复调用静默)。"""
    global _warned
    if not _warned:
        _warned = True
        print(f"[checkpoint] 警告: {message};已降级为内存检查点,进程退出后状态丢失")


def build_checkpointer():
    """构造图使用的 checkpointer。

    返回：
        BaseCheckpointSaver 实例(SqliteSaver 或 MemorySaver)。
        任何环境下都保证返回可用对象,调用方无需 try/except。
    """
    backend = os.getenv("CHECKPOINT_BACKEND", "sqlite").strip().lower()

    if backend == "memory":
        # 显式选择内存后端:测试 / 一次性脚本场景,不产生磁盘文件
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    if backend != "sqlite":
        _warn(f"未知 CHECKPOINT_BACKEND={backend!r}(可选 sqlite / memory)")

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        _warn("未安装 langgraph-checkpoint-sqlite(pip install -r requirements.txt)")
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    path = Path(os.getenv("CHECKPOINT_PATH") or DEFAULT_CHECKPOINT_PATH).resolve()
    try:
        # 目录不存在时先创建,否则 sqlite3 无法建库文件
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False:SqliteSaver 内部自带锁串行化访问,
        # 允许同一连接被不同线程(trace 回调等)复用
        conn = sqlite3.connect(str(path), check_same_thread=False)
        # WAL 模式:让「一个进程写、另一个进程读」并发,跨进程读取不被写锁阻塞
        conn.execute("PRAGMA journal_mode=WAL")
        saver = SqliteSaver(conn)
        # 显式建表:把初始化失败提前到建图阶段暴露,而不是等第一次 invoke
        saver.setup()
    except (OSError, sqlite3.Error) as exc:
        # 无写权限 / 磁盘满 / 路径非法:降级而不是崩溃,保证图至少能跑
        _warn(f"无法打开 SQLite 检查点 {path}: {type(exc).__name__}: {exc}")
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    # 注册退出时关闭连接:让 WAL 文件正常合并,避免遗留 -wal / -shm 垃圾文件
    atexit.register(_close_quietly, conn)
    return saver


def _close_quietly(conn: sqlite3.Connection) -> None:
    """进程退出时关闭连接;关闭失败不影响退出(解释器 teardown 阶段容不得异常)。"""
    try:
        conn.close()
    except Exception:  # noqa: BLE001, S110 - 退出阶段的兜底,静默是刻意的
        pass
