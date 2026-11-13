"""术语澄清流程测试:先问一次、记住后不再问、跳过按默认口径。"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from starlette.testclient import TestClient

import ask_db
import web.app as app


class FakeBridge:
    """收集事件,替代真实 SSE。"""

    def __init__(self):
        self.events: list[dict] = []

    def push_threadsafe(self, event):
        self.events.append(event)

    def events_of(self, name):
        return [item["data"] for item in self.events if item["event"] == name]


class FakeAdapter:
    """两张表都有「支付金额」,用于模拟口径不明。"""

    def list_tables(self, _runtime):
        column = {"name": "支付金额", "type": "decimal", "nullable": True, "key": ""}
        return [
            {
                "name": "orders",
                "type": "BASE TABLE",
                "columns": [{"name": "渠道", "type": "varchar", "nullable": True, "key": ""}, column],
            },
            {
                "name": "refunds",
                "type": "BASE TABLE",
                "columns": [{"name": "渠道", "type": "varchar", "nullable": True, "key": ""}, dict(column)],
            },
        ]

    def run_select(self, _runtime, sql, max_rows):
        frame = pd.DataFrame([{"渠道": "自然流量", "客单价": 120.5}])
        return {"dataframe": frame, "row_count": 1, "truncated": False}

    def explain(self, _runtime, _sql):
        return ""


def _patch(monkeypatch, tmp_path, detect):
    """替换连接、LLM、Agent 与所有落盘路径。"""
    monkeypatch.setattr(
        app.db_connections,
        "get_connection",
        lambda _id: {"id": "dbconn01", "label": "电商库", "kind": "mysql"},
    )
    monkeypatch.setattr(app.db_connections, "get_password", lambda _id: "pwd")
    monkeypatch.setattr(app.db_connections, "append_query_audit", lambda _record: None)
    monkeypatch.setattr(app, "get_adapter", lambda _kind: FakeAdapter())
    monkeypatch.setattr(app.db_glossary, "GLOSSARY_PATH", tmp_path / "db_glossary.json")
    monkeypatch.setattr(app.db_glossary, "PENDING_PATH", tmp_path / "db_pending.jsonl")
    monkeypatch.setattr(app.db_semantics, "SEMANTICS_PATH", tmp_path / "db_semantics.json")
    monkeypatch.setattr(app, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(app, "_load_trusted", lambda _thread_id: set())
    monkeypatch.setattr(app, "_question_datasets", lambda _question: [])
    monkeypatch.setattr(app, "_session_dataset", lambda _thread_id: "")
    monkeypatch.setattr(app, "_session_last_run_id", lambda _thread_id: "")
    monkeypatch.setattr(app, "_record_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "get_pending_interrupt", lambda _state: None)
    monkeypatch.setattr(app, "_detect_unknown_terms", detect)

    captured: dict = {"generate": [], "agent": []}

    def fake_generate(question, tables, config=None, dialect="sqlite", glossary=None):
        captured["generate"].append({"question": question, "glossary": glossary})
        return type("Sql", (), {"sql": "SELECT 1 AS x", "explanation": ""})()

    def fake_run_agent(question, **_kwargs):
        captured["agent"].append(question)
        return {
            "run_id": "run-1",
            "final_answer": "ok",
            "plan": [],
            "warnings": [],
            "exec_result": {"exit_status": "success"},
        }

    monkeypatch.setattr(ask_db, "generate_sql", fake_generate)
    monkeypatch.setattr(app, "run_agent", fake_run_agent)
    return captured


def _ask(thread_id: str, question: str) -> FakeBridge:
    bridge = FakeBridge()
    app._execute_question(
        thread_id, question, False, bridge,
        display_question=question, database_id="dbconn01",
    )
    return bridge


def test_unknown_term_asks_and_does_not_generate_sql(monkeypatch, tmp_path):
    detect = lambda *_args: [
        {"term": "客单价", "reason": "多张表都有支付金额", "candidates": ["orders.支付金额", "refunds.支付金额"]}
    ]
    captured = _patch(monkeypatch, tmp_path, detect)

    bridge = _ask("thread-clarify", "按渠道看客单价")

    clarify = bridge.events_of("clarify")
    assert len(clarify) == 1
    assert clarify[0]["terms"][0]["term"] == "客单价"
    assert clarify[0]["terms"][0]["candidates"][0] == "orders.支付金额"
    # 本轮不生成 SQL、不执行、也不进入分析
    assert captured["generate"] == []
    assert captured["agent"] == []
    assert bridge.events_of("sql") == []
    assert bridge.events_of("answer") == []

    pending = app.db_glossary.load_pending("thread-clarify")
    assert pending["question"] == "按渠道看客单价"
    assert pending["database_id"] == "dbconn01"


def test_remembered_term_is_reused_without_asking_again(monkeypatch, tmp_path):
    def detect(_question, _tables, glossary):
        return [] if any(item["term"] == "客单价" for item in glossary) else [
            {"term": "客单价", "reason": "口径不明", "candidates": ["orders.支付金额"]}
        ]

    captured = _patch(monkeypatch, tmp_path, detect)

    # 第一轮:弹澄清卡
    assert _ask("thread-remember", "按渠道看客单价").events_of("clarify")

    # 用户确认并记住:术语写入术语表,待澄清记录清除
    app.db_glossary.upsert_term("dbconn01", "客单价", "成交金额 / 订单数", "orders.支付金额")
    app.db_glossary.clear_pending("thread-remember")

    bridge = _ask("thread-remember", "按渠道看客单价")
    assert bridge.events_of("clarify") == []
    assert len(captured["generate"]) == 1
    assert captured["generate"][0]["glossary"][0]["term"] == "客单价"
    assert bridge.events_of("answer")[0]["status"] == "success"


def test_clear_pending_then_skip_uses_default_candidate(monkeypatch, tmp_path):
    """跳过路径:问题前拼「按默认口径」,不写术语表。"""
    _patch(monkeypatch, tmp_path, lambda *_args: [])
    app.db_glossary.save_pending(
        {
            "thread_id": "thread-skip",
            "database_id": "dbconn01",
            "question": "按渠道看客单价",
            "terms": [
                {
                    "term": "客单价",
                    "reason": "口径不明",
                    "candidates": ["orders.支付金额", "refunds.支付金额"],
                }
            ],
        }
    )

    captured: dict = {}
    monkeypatch.setattr(app, "_session_exists", lambda _thread_id: False)

    def fake_execute(thread_id, question, continue_session, bridge, **kwargs):
        captured["question"] = question
        captured["kwargs"] = kwargs
        bridge.push_threadsafe({"event": "done", "data": {"thread_id": thread_id}})

    monkeypatch.setattr(app, "_execute_question", fake_execute)

    with TestClient(app.app) as client:
        body = client.post(
            "/api/sessions/thread-skip/clarify",
            json={"answers": [], "skip": True},
        )
        assert body.status_code == 200
        assert "event: done" in body.text

    assert captured["question"].startswith(
        "(以下术语按默认口径: 客单价→orders.支付金额)"
    )
    assert "按渠道看客单价" in captured["question"]
    assert captured["kwargs"]["display_question"] == "按渠道看客单价"
    assert captured["kwargs"]["skip_check"] is True
    assert app.db_glossary.load_pending("thread-skip") is None
    assert app.db_glossary.get_terms("dbconn01") == {}


def test_clarify_without_pending_is_404(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, lambda *_args: [])
    with TestClient(app.app) as client:
        response = client.post(
            "/api/sessions/thread-none/clarify", json={"answers": [], "skip": True}
        )
    assert response.status_code == 404
    assert "没有待澄清" in response.json()["detail"]


def test_confirmed_answer_is_persisted_only_when_remember(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, lambda *_args: [])
    app.db_glossary.save_pending(
        {
            "thread_id": "thread-keep",
            "database_id": "dbconn01",
            "question": "按渠道看客单价",
            "terms": [{"term": "客单价", "reason": "", "candidates": ["orders.支付金额"]}],
        }
    )
    captured: dict = {}
    monkeypatch.setattr(app, "_session_exists", lambda _thread_id: False)

    def fake_execute(thread_id, question, continue_session, bridge, **kwargs):
        captured["kwargs"] = kwargs
        bridge.push_threadsafe({"event": "done", "data": {"thread_id": thread_id}})

    monkeypatch.setattr(app, "_execute_question", fake_execute)

    with TestClient(app.app) as client:
        response = client.post(
            "/api/sessions/thread-keep/clarify",
            json={
                "answers": [
                    {
                        "term": "客单价",
                        "definition": "成交金额 / 订单数",
                        "sql_hint": "orders.支付金额",
                        "remember": False,
                    }
                ],
                "skip": False,
            },
        )
        assert response.status_code == 200

    # remember=false 只本轮生效:不落术语表,而是作为临时口径透传
    assert app.db_glossary.get_terms("dbconn01") == {}
    assert captured["kwargs"]["extra_terms"][0]["term"] == "客单价"
    assert json.dumps(captured["kwargs"]["extra_terms"], ensure_ascii=False)
