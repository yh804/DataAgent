"""执行前 SQL 校验测试:EXPLAIN 拦截不存在的表 / 列,最多修复一次。"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

import ask_db
import web.app as app
from db_adapter import SqliteAdapter


class FakeBridge:
    """收集事件,替代真实 SSE。"""

    def __init__(self):
        self.events: list[dict] = []

    def push_threadsafe(self, event):
        self.events.append(event)

    def events_of(self, name):
        return [item["data"] for item in self.events if item["event"] == name]


class GuardAdapter:
    """explain 结果由测试指定;记录调用次数。"""

    def __init__(self, explain_results):
        self.explain_results = list(explain_results)
        self.explain_calls: list[str] = []
        self.select_calls: list[str] = []

    def list_tables(self, _runtime):
        return [
            {
                "name": "orders",
                "type": "BASE TABLE",
                "columns": [
                    {"name": "渠道", "type": "varchar", "nullable": True, "key": ""},
                    {"name": "支付金额", "type": "decimal", "nullable": True, "key": ""},
                ],
            }
        ]

    def explain(self, _runtime, sql):
        self.explain_calls.append(sql)
        if not self.explain_results:
            return ""
        return self.explain_results.pop(0)

    def run_select(self, _runtime, sql, max_rows):
        self.select_calls.append(sql)
        frame = pd.DataFrame([{"渠道": "自然流量", "GMV": 120.5}])
        return {"dataframe": frame, "row_count": 1, "truncated": False}


def _patch(monkeypatch, tmp_path, adapter, sqls):
    """替换连接、LLM、Agent 与落盘路径。"""
    monkeypatch.setattr(
        app.db_connections,
        "get_connection",
        lambda _id: {"id": "dbconn01", "label": "电商库", "kind": "mysql"},
    )
    monkeypatch.setattr(app.db_connections, "get_password", lambda _id: "pwd")
    monkeypatch.setattr(app.db_connections, "append_query_audit", lambda _record: None)
    monkeypatch.setattr(app, "get_adapter", lambda _kind: adapter)
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
    monkeypatch.setattr(app, "_detect_unknown_terms", lambda *_args, **_kwargs: [])

    generated = list(sqls)
    questions: list[str] = []

    def fake_generate(question, tables, config=None, dialect="sqlite", glossary=None):
        questions.append(question)
        sql = generated.pop(0) if generated else "SELECT 1"
        return type("Sql", (), {"sql": sql, "explanation": ""})()

    monkeypatch.setattr(ask_db, "generate_sql", fake_generate)
    monkeypatch.setattr(
        app,
        "run_agent",
        lambda question, **_kwargs: {
            "run_id": "run-1",
            "final_answer": "ok",
            "plan": [],
            "warnings": [],
        },
    )
    return questions


def _run() -> FakeBridge:
    bridge = FakeBridge()
    app._execute_question(
        "thread-guard", "按渠道统计 GMV", False, bridge,
        display_question="按渠道统计 GMV", database_id="dbconn01",
    )
    return bridge


def test_valid_sql_explains_once_then_selects(monkeypatch, tmp_path):
    adapter = GuardAdapter([])
    _patch(monkeypatch, tmp_path, adapter, ["SELECT 渠道, SUM(支付金额) FROM orders"])

    bridge = _run()

    assert adapter.explain_calls == ["SELECT 渠道, SUM(支付金额) FROM orders"]
    assert adapter.select_calls == ["SELECT 渠道, SUM(支付金额) FROM orders"]
    assert bridge.events_of("answer")[0]["status"] == "success"


def test_unknown_column_retries_once_then_stops(monkeypatch, tmp_path):
    error = "MySQL 查询失败(1054): Unknown column '缺失列' in 'field list'"
    adapter = GuardAdapter([error, error])
    questions = _patch(
        monkeypatch,
        tmp_path,
        adapter,
        ["SELECT 缺失列 FROM orders", "SELECT 缺失列 FROM orders"],
    )

    bridge = _run()

    # 生成两次(含修复提示),run_select 一次都没被调用
    assert len(questions) == 2
    assert "修复要求" in questions[1]
    assert "Unknown column" in questions[1]
    assert len(adapter.explain_calls) == 2
    assert adapter.select_calls == []

    sql_events = bridge.events_of("sql")
    assert sql_events[0]["ok"] is False
    assert "已停止执行" in sql_events[0]["error"]

    answer = bridge.events_of("answer")[0]
    assert answer["status"] == "error"
    assert "生成的 SQL 引用了不存在的表/列,已停止执行" in answer["error_analysis"]
    assert "Unknown column" in answer["error_analysis"]


def test_repair_succeeds_and_runs_second_sql(monkeypatch, tmp_path):
    adapter = GuardAdapter(["Unknown column '缺失列'", ""])
    _patch(
        monkeypatch,
        tmp_path,
        adapter,
        ["SELECT 缺失列 FROM orders", "SELECT 支付金额 FROM orders"],
    )

    bridge = _run()

    assert len(adapter.explain_calls) == 2
    assert adapter.select_calls == ["SELECT 支付金额 FROM orders"]
    assert bridge.events_of("sql")[0]["sql"] == "SELECT 支付金额 FROM orders"
    assert bridge.events_of("answer")[0]["status"] == "success"


def test_sqlite_explain_detects_missing_table_and_column(tmp_path):
    db_path = tmp_path / "guard.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
    connection.commit()
    connection.close()

    adapter = SqliteAdapter()
    runtime = {"path": str(db_path)}

    assert adapter.explain(runtime, "SELECT amount FROM orders") == ""
    assert "no such column" in adapter.explain(runtime, "SELECT missing FROM orders")
    assert "no such table" in adapter.explain(runtime, "SELECT * FROM not_exist")


def test_sqlite_explain_rejects_non_select(tmp_path):
    db_path = tmp_path / "guard2.db"
    sqlite3.connect(db_path).close()
    adapter = SqliteAdapter()
    with pytest.raises(ValueError):
        adapter.explain({"path": str(db_path)}, "DELETE FROM orders")
