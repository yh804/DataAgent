"""密钥不落盘测试:trace / index.jsonl / sessions.jsonl 里不得出现 sk- 原文。"""

from __future__ import annotations

from datetime import datetime

import archive
import trace
import web.app as web_app

KEY = "sk-leak1234567890abcdef"


def test_trace_masks_key(tmp_path, monkeypatch):
    """trace 步骤里的异常文本带 key 时,落盘前必须脱敏。"""
    monkeypatch.setattr(trace, "TRACE_DIR", tmp_path / "traces")
    handler = trace.TraceCallbackHandler()
    handler._steps.append(
        {
            "type": "llm",
            "name": "deepseek-chat",
            "run_id": "r1",
            "duration_ms": 1,
            "status": "error",
            "error": f"AuthenticationError: invalid key {KEY}",
            "tokens": None,
            "parent_id": None,
        }
    )
    path = trace.write_trace(
        handler,
        "thread-0001",
        f"用 {KEY} 提问",
        datetime.now(),
    )
    assert path is not None
    text = (tmp_path / "traces").glob("*.json")
    content = next(text).read_text(encoding="utf-8")
    assert KEY not in content
    assert "sk-***" in content


def test_run_index_masks_key(tmp_path, monkeypatch):
    """index.jsonl 的问题字段带 key 时,写入行必须脱敏。"""
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(archive, "OUTPUT_DIR", outputs)
    archive._append_run_index(
        "run-1",
        "thread-1",
        {"question": f"帮我分析 {KEY}", "status": "success"},
    )
    content = (outputs / "index.jsonl").read_text(encoding="utf-8")
    assert KEY not in content
    assert "sk-***" in content


def test_sessions_masks_key(tmp_path, monkeypatch):
    """sessions.jsonl 的标题来自用户问题,带 key 时也必须脱敏。"""
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "sessions.jsonl")
    web_app._record_session("thread-0002", f"用 {KEY} 跑一下", "run-2")
    content = (tmp_path / "sessions.jsonl").read_text(encoding="utf-8")
    assert KEY not in content
    assert "sk-***" in content
