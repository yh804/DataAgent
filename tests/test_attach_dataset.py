"""附件数据集(输入框 📎)的 Web 层回归测试:离线,不联网、不起 Docker、不调 LLM。

覆盖:
    1. 带 dataset_name:Agent 收到「分析 <数据集>：<原始问题>」,会话标题只保留原始问题;
    2. 不带 dataset_name:问题原样传入,行为与改前一致。
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import web.app as web_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出/会话/审批目录都指向 tmp_path,不碰真实 outputs/。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "outputs" / "sessions.jsonl")
    monkeypatch.setattr(web_app, "APPROVALS_PATH", tmp_path / "outputs" / "approvals.jsonl")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


def _fake_run_agent(captured: dict):
    """伪造 run_agent:记录收到的 question,直接返回已完成状态(不产生中断)。"""

    def _run(question, thread_id, continue_session, progress_callback, last_run_id=""):
        captured["question"] = question
        return {
            "question": question,
            "dataset_name": captured.get("dataset_name", ""),
            "run_id": "r1",
            "final_answer": "ok",
            "error_analysis": "",
            "warnings": [],
            "plan": [],
        }

    return _run


def _read_sessions(tmp_path) -> list[dict]:
    path = tmp_path / "outputs" / "sessions.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_dataset_name_prefixes_agent_question_but_not_session_title(client, monkeypatch, tmp_path):
    captured = {"dataset_name": "demo_sales.csv"}
    monkeypatch.setattr(web_app, "run_agent", _fake_run_agent(captured))
    # 不碰真实检查点:无已锁定数据集、无待审批、无历史会话
    monkeypatch.setattr(web_app, "_session_dataset", lambda tid: "")
    monkeypatch.setattr(web_app, "_session_exists", lambda tid: False)
    monkeypatch.setattr(web_app, "load_pending_interrupt", lambda tid: None)

    resp = client.post(
        "/api/sessions/t-attach/messages",
        json={"question": "按月看订单金额趋势", "dataset_name": "demo_sales.csv"},
    )
    assert resp.status_code == 200

    # Agent 侧必须看到数据集前缀,planner 才能锁定附件
    assert captured["question"] == "分析 demo_sales.csv：按月看订单金额趋势"
    # 会话标题与会话记录只保留用户原始问题,不暴露内部前缀
    records = _read_sessions(tmp_path)
    record = next(r for r in records if r["thread_id"] == "t-attach")
    assert "分析 demo_sales.csv：" not in record["title"]
    assert record["title"].startswith("按月看订单金额趋势")


def test_without_dataset_name_question_passes_through(client, monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(web_app, "run_agent", _fake_run_agent(captured))
    monkeypatch.setattr(web_app, "_session_dataset", lambda tid: "")
    monkeypatch.setattr(web_app, "_session_exists", lambda tid: False)
    monkeypatch.setattr(web_app, "load_pending_interrupt", lambda tid: None)

    resp = client.post(
        "/api/sessions/t-plain/messages", json={"question": "看一下分布"}
    )
    assert resp.status_code == 200

    # 回归:不带附件时问题原样传入,行为与改前一致
    assert captured["question"] == "看一下分布"
    record = next(r for r in _read_sessions(tmp_path) if r["thread_id"] == "t-plain")
    assert record["title"] == "看一下分布"
