"""审批减负 + 风险分级的接口级离线测试:不联网、不调 LLM、不起 Docker。

覆盖(对应需求验收):
    1. 已信任数据集再提问:SSE 出现 plan_auto 而非 plan,answer.approved_by == "policy";
    2. 人工批准会写 outputs/approvals.jsonl(decided_by=human);
    3. POST /trust 登记后,下一轮同数据集提问走自动通过(plan_auto);
    4. POST /approval 非法 action 仍为 400(回归);
    5. 未信任数据集:策略 confirm,SSE 出现 plan 事件且带 reasons;
       审计记录 decided_by=policy / action=confirm。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import web.app as web_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出/数据目录指向 tmp_path,不碰真实 outputs/ 与检查点。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "outputs" / "sessions.jsonl")
    monkeypatch.setattr(web_app, "APPROVALS_PATH", tmp_path / "outputs" / "approvals.jsonl")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("APPROVAL_POLICY", raising=False)
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "outputs").mkdir()
    # 一个真实的小 csv:describe_dataset 离线可读,供策略判定与列名自检
    (tmp_path / "data" / "orders.csv").write_text("组,收入\nA,1\nB,2\n", encoding="utf-8")
    # 不碰真实检查点:全部按「全新会话」处理
    monkeypatch.setattr(web_app, "_session_exists", lambda tid: False)
    monkeypatch.setattr(web_app, "load_pending_interrupt", lambda tid: None)
    return TestClient(web_app.app)


def _sse_events(text: str) -> list[tuple[str, dict]]:
    """把 SSE 响应文本解析成 [(event, data), ...]。"""
    events: list[tuple[str, dict]] = []
    for frame in text.split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith(":") or not line:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


def _fake_run_done(captured: dict):
    """伪造 run_agent:直接跑完(记录 auto_approve 实参供断言)。"""
    def _run(question, thread_id="", continue_session=False, auto_approve=False,
             progress_callback=None, last_run_id=""):
        captured["auto_approve"] = auto_approve
        return {
            "run_id": "r1",
            "final_answer": "完成",
            "error_analysis": "",
            "warnings": [],
            "plan": ["读取 orders.csv", "统计收入均值"],
            "dataset_name": "orders.csv",
        }
    return _run


def _fake_run_pending(question, thread_id="", continue_session=False, auto_approve=False,
                      progress_callback=None, last_run_id=""):
    """伪造 run_agent:暂停在 review_plan,等待审批。"""
    pending = {
        "question": question,
        "dataset_name": "orders.csv",
        "plan": ["读取 orders.csv", "统计收入均值"],
        "reason": "需要先看分布",
    }
    return {
        "__interrupt__": (SimpleNamespace(value=pending),),
        "run_id": "",
        "final_answer": "",
        "error_analysis": "",
        "warnings": [],
    }


def _read_audits(client) -> list[dict]:
    path = web_app.APPROVALS_PATH
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------- 1. 已信任 -> plan_auto 而非 plan ----------

def test_trusted_second_round_auto_approves(client, monkeypatch):
    # 预置信任:sessions.jsonl 里该 thread 已有 trusted_datasets
    web_app.SESSIONS_PATH.write_text(
        json.dumps({"thread_id": "t1", "title": "旧轮", "updated_at": "2026-01-01T00:00:00",
                    "last_run_id": "r0", "trusted_datasets": ["orders.csv"]},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "_session_dataset", lambda tid: "orders.csv")
    captured: dict = {}
    monkeypatch.setattr(web_app, "run_agent", _fake_run_done(captured))

    resp = client.post("/api/sessions/t1/messages", json={"question": "统计 orders.csv 的收入均值"})
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    names = [e for e, _ in events]

    # 策略自动放行:图根本没产生中断,run_agent 必须收到 auto_approve=True
    assert captured["auto_approve"] is True
    assert "plan_auto" in names
    assert "plan" not in names  # 不能弹人工审批卡

    plan_auto = next(d for e, d in events if e == "plan_auto")
    assert plan_auto["approved_by"] == "policy"
    assert plan_auto["dataset_name"] == "orders.csv"
    assert any("本会话已批准该数据集" in r for r in plan_auto["reasons"])

    answer = next(d for e, d in events if e == "answer")
    assert answer["approved_by"] == "policy"
    assert answer["plan"] == ["读取 orders.csv", "统计收入均值"]

    audits = _read_audits(client)
    assert len(audits) == 1
    assert audits[0]["decided_by"] == "policy" and audits[0]["action"] == "auto"


# ---------- 2. 人工批准写 approvals.jsonl ----------

def test_human_approval_writes_audit(client, monkeypatch):
    pending = {
        "question": "统计 orders.csv 的收入均值",
        "dataset_name": "orders.csv",
        "plan": ["读取 orders.csv", "统计收入均值"],
        "reason": "需要先看分布",
    }
    monkeypatch.setattr(web_app, "load_pending_interrupt", lambda tid: dict(pending))
    monkeypatch.setattr(
        web_app, "resume_agent",
        lambda tid, decision, progress_callback=None: {
            "run_id": "r2", "final_answer": "完成", "error_analysis": "",
            "warnings": [], "plan": pending["plan"], "question": pending["question"],
        },
    )

    resp = client.post("/api/sessions/t2/approval", json={"action": "approve"})
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    answer = next(d for e, d in events if e == "answer")
    assert answer["approved_by"] == "human"

    audits = _read_audits(client)
    assert len(audits) == 1
    row = audits[0]
    assert row["decided_by"] == "human" and row["action"] == "approve"
    assert row["dataset"] == "orders.csv" and row["thread_id"] == "t2"
    assert row["reasons"]  # 人工行也带策略理由:讲清楚当时为什么被问


# ---------- 3. trust 接口登记 -> 下一轮自动通过 ----------

def test_trust_endpoint_enables_auto_next_round(client, monkeypatch):
    resp = client.post("/api/sessions/t3/trust",
                       json={"dataset": "orders.csv", "trusted": True})
    assert resp.status_code == 200
    assert resp.json()["trusted_datasets"] == ["orders.csv"]

    monkeypatch.setattr(web_app, "_session_dataset", lambda tid: "orders.csv")
    monkeypatch.setattr(web_app, "run_agent", _fake_run_done({}))
    resp = client.post("/api/sessions/t3/messages", json={"question": "统计 orders.csv 的收入均值"})
    names = [e for e, _ in _sse_events(resp.text)]
    assert "plan_auto" in names and "plan" not in names

    # 取消信任后回到人工审批
    resp = client.post("/api/sessions/t3/trust",
                       json={"dataset": "orders.csv", "trusted": False})
    assert resp.json()["trusted_datasets"] == []
    monkeypatch.setattr(web_app, "run_agent", _fake_run_pending)
    resp = client.post("/api/sessions/t3/messages", json={"question": "统计 orders.csv 的收入均值"})
    names = [e for e, _ in _sse_events(resp.text)]
    assert "plan" in names and "plan_auto" not in names


# ---------- 4. 非法 action 回归 ----------

def test_approval_rejects_bad_action(client):
    resp = client.post("/api/sessions/t4/approval", json={"action": "hack"})
    assert resp.status_code == 400


# ---------- 5. 未信任 -> plan 事件带 reasons,审计 action=confirm ----------

def test_untrusted_emits_plan_with_reasons(client, monkeypatch):
    monkeypatch.setattr(web_app, "run_agent", _fake_run_pending)
    resp = client.post("/api/sessions/t5/messages", json={"question": "统计 orders.csv 的收入均值"})
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    names = [e for e, _ in events]
    assert "plan" in names and "plan_auto" not in names

    plan = next(d for e, d in events if e == "plan")
    assert any("首次分析该数据集" in r for r in plan["reasons"])
    assert "收入" in plan["columns"]  # 列名自检数据随事件下发

    audits = _read_audits(client)
    assert len(audits) == 1
    assert audits[0]["decided_by"] == "policy" and audits[0]["action"] == "confirm"
