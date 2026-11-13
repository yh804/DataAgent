"""数据库问答主流程测试:CSV 落盘、问题前缀、sql 事件与 answer 字段。"""

from __future__ import annotations

import json

import pandas as pd

import ask_db
import web.app as app


class FakeBridge:
    """收集 push_threadsafe 事件,替代真实 SSE。"""

    def __init__(self):
        self.events: list[dict] = []

    def push_threadsafe(self, event):
        self.events.append(event)

    def events_of(self, name: str):
        return [item["data"] for item in self.events if item["event"] == name]


class FakeAdapter:
    """返回固定 schema 与 3 行结果。"""

    def __init__(self):
        self.list_calls = 0
        self.select_calls: list[str] = []

    def list_tables(self, _runtime):
        self.list_calls += 1
        return [
            {
                "name": "orders",
                "type": "BASE TABLE",
                "columns": [
                    {"name": "订单号", "type": "varchar", "nullable": False, "key": "PRI"},
                    {"name": "渠道", "type": "varchar", "nullable": True, "key": ""},
                    {"name": "支付金额", "type": "decimal", "nullable": True, "key": ""},
                ],
            }
        ]

    def run_select(self, _runtime, sql, max_rows):
        self.select_calls.append(sql)
        frame = pd.DataFrame(
            [
                {"渠道": "自然流量", "GMV": 120.5},
                {"渠道": "信息流", "GMV": 300.0},
                {"渠道": "搜索", "GMV": 80.25},
            ]
        )
        return {"dataframe": frame, "row_count": len(frame), "truncated": False}

    def explain(self, _runtime, _sql):
        """执行计划校验:测试替身始终通过。"""
        return ""


def _patch_flow(monkeypatch, tmp_path, adapter=None):
    """把数据库、LLM、Agent 与数据目录全部替换成测试替身。"""
    adapter = adapter or FakeAdapter()
    captured: dict = {"audit": []}

    monkeypatch.setattr(
        app.db_connections,
        "get_connection",
        lambda _id: {"id": "dbconn01", "label": "电商库", "kind": "mysql", "host": "h", "database": "d"},
    )
    monkeypatch.setattr(app.db_connections, "get_password", lambda _id: "super-secret")
    monkeypatch.setattr(app.db_connections, "append_query_audit", captured["audit"].append)
    monkeypatch.setattr(app, "get_adapter", lambda _kind: adapter)
    monkeypatch.setattr(
        ask_db,
        "generate_sql",
        lambda *_args, **_kwargs: type("Sql", (), {"sql": 'SELECT "渠道", SUM("支付金额") AS GMV FROM orders GROUP BY "渠道"'})(),
    )
    monkeypatch.setattr(app, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(app, "_load_trusted", lambda _thread_id: set())
    monkeypatch.setattr(app, "_question_datasets", lambda _question: [])
    monkeypatch.setattr(app, "_session_dataset", lambda _thread_id: "")
    monkeypatch.setattr(app, "_session_last_run_id", lambda _thread_id: "")
    monkeypatch.setattr(app, "_record_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "get_pending_interrupt", lambda _state: None)
    # 术语检测需要真实 LLM;流程测试固定返回「无未知词」
    monkeypatch.setattr(app, "_detect_unknown_terms", lambda *_args, **_kwargs: [])

    def fake_run_agent(question, **_kwargs):
        captured["agent_question"] = question
        return {
            "run_id": "run-db-1",
            "dataset_name": "db_电商库_test.csv",
            "final_answer": "渠道 GMV 前三为信息流、自然流量、搜索。",
            "plan": [],
            "warnings": [],
        }

    monkeypatch.setattr(app, "run_agent", fake_run_agent)
    return adapter, captured


def test_database_question_exports_csv_and_returns_sql(monkeypatch, tmp_path):
    adapter, captured = _patch_flow(monkeypatch, tmp_path)
    bridge = FakeBridge()

    app._execute_question(
        "thread-db",
        "按渠道统计 GMV",
        False,
        bridge,
        display_question="按渠道统计 GMV",
        database_id="dbconn01",
    )

    # 1) CSV 落到临时数据目录,utf-8-sig 可被 pandas 正常读回
    csv_files = list(tmp_path.glob("db_*.csv"))
    assert len(csv_files) == 1
    frame = pd.read_csv(csv_files[0], encoding="utf-8-sig")
    assert list(frame.columns) == ["渠道", "GMV"]
    assert len(frame) == 3

    # 2) 交给 Agent 的是「分析 <csv>：<原问题>」
    assert captured["agent_question"] == f"分析 {csv_files[0].name}：按渠道统计 GMV"

    # 3) sql 事件先于 answer,并带 SQL 与行数
    sql_events = bridge.events_of("sql")
    assert len(sql_events) == 1
    assert sql_events[0]["ok"] is True
    assert sql_events[0]["row_count"] == 3
    assert sql_events[0]["db_label"] == "电商库"
    assert "SUM" in sql_events[0]["sql"]

    # 4) answer 携带结果卡字段
    answers = bridge.events_of("answer")
    assert len(answers) == 1
    assert answers[0]["status"] == "success"
    assert answers[0]["sql"] == sql_events[0]["sql"]
    assert answers[0]["sql_row_count"] == 3
    assert answers[0]["db_label"] == "电商库"

    # 5) 审计落库且不含密码
    assert len(captured["audit"]) == 1
    assert captured["audit"][0]["ok"] is True
    assert captured["audit"][0]["row_count"] == 3
    assert "super-secret" not in json.dumps(bridge.events, ensure_ascii=False)
    assert "super-secret" not in json.dumps(captured["audit"], ensure_ascii=False)
    assert adapter.select_calls


def test_database_query_failure_reports_sql_and_reason(monkeypatch, tmp_path):
    class BrokenAdapter(FakeAdapter):
        def run_select(self, _runtime, sql, max_rows):
            self.select_calls.append(sql)
            raise app.AdapterError("查询失败: Unknown column '支付金额'")

    _adapter, captured = _patch_flow(monkeypatch, tmp_path, BrokenAdapter())
    bridge = FakeBridge()

    app._execute_question(
        "thread-db-fail",
        "按渠道统计 GMV",
        False,
        bridge,
        display_question="按渠道统计 GMV",
        database_id="dbconn01",
    )

    assert "agent_question" not in captured  # 查询失败不进入 Agent
    assert list(tmp_path.glob("db_*.csv")) == []

    sql_events = bridge.events_of("sql")
    assert sql_events[0]["ok"] is False
    assert "Unknown column" in sql_events[0]["error"]

    answers = bridge.events_of("answer")
    assert answers[0]["status"] == "error"
    assert answers[0]["sql_row_count"] == 0
    assert "Unknown column" in answers[0]["error_analysis"]
    assert answers[0]["db_label"] == "电商库"
    assert "super-secret" not in json.dumps(bridge.events, ensure_ascii=False)
