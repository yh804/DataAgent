"""数据库查询进度、取消与超时的测试。"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import db
import web.app as app
from db_adapter import QueryCancelled


class FakeBridge:
    """只记录事件的最小事件桥,cancel_on_start 用于模拟客户端断开。"""

    def __init__(self, cancel_on_start: bool = False) -> None:
        self.events: list[dict] = []
        self._cancelled = False
        self._cancel_on_start = cancel_on_start

    def push_threadsafe(self, event: dict) -> None:
        self.events.append(event)
        if self._cancel_on_start and event["event"] == "db_start":
            self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def events_of(self, name: str) -> list[dict]:
        return [event["data"] for event in self.events if event["event"] == name]


class SlowAdapter:
    """模拟耗时查询,并按 cancel_event 主动退出。"""

    def __init__(self, seconds: float = 1.2) -> None:
        self.seconds = seconds

    def run_select(self, _runtime, _sql, max_rows, timeout_seconds=None, cancel_event=None):
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise QueryCancelled("查询已取消")
            time.sleep(0.02)
        return {"dataframe": None, "row_count": max_rows, "cached": False}


def test_progress_events_are_pushed_every_second():
    bridge = FakeBridge()
    adapter = SlowAdapter(seconds=1.2)

    result = app._run_db_query_with_progress(
        "thread-progress", "dbconn01", adapter, {}, "SELECT 1",
        timeout_seconds=5, bridge=bridge,
    )

    assert result["row_count"] == app.DEFAULT_MAX_ROWS
    assert bridge.events[0]["event"] == "db_start"
    assert bridge.events_of("db_start")[0]["sql"] == "SELECT 1"
    progress = bridge.events_of("db_progress")
    assert progress and progress[0]["elapsed"] >= 1


def test_cancel_sets_event_and_raises_query_cancelled():
    bridge = FakeBridge(cancel_on_start=True)

    with pytest.raises(QueryCancelled):
        app._run_db_query_with_progress(
            "thread-cancel", "dbconn01", SlowAdapter(seconds=5), {}, "SELECT 1",
            timeout_seconds=30, bridge=bridge,
        )


def test_timeout_raises_with_seconds_in_message():
    bridge = FakeBridge()

    with pytest.raises(TimeoutError) as excinfo:
        app._run_db_query_with_progress(
            "thread-timeout", "dbconn01", SlowAdapter(seconds=5), {}, "SELECT 1",
            timeout_seconds=0.3, bridge=bridge,
        )

    message = str(excinfo.value)
    assert "0.3 秒" in message
    assert "可加 WHERE 过滤或改用采样" in message


def test_web_query_timeout_defaults_to_120(monkeypatch):
    monkeypatch.delenv("DB_QUERY_TIMEOUT_SECONDS", raising=False)
    assert app._db_query_timeout_from_env() == 120


def test_sqlite_run_select_raises_query_cancelled(tmp_path):
    db_path = tmp_path / "cancel.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(i,) for i in range(500)])
        conn.commit()
    finally:
        conn.close()
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(QueryCancelled):
        db.run_select(
            db_path,
            "SELECT COUNT(*) FROM t a, t b",
            max_rows=10,
            timeout=30,
            cancel_event=cancel_event,
        )
