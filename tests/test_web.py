"""Web 层(FastAPI)离线测试:不联网、不起 Docker、不调 LLM。

覆盖:
    1. health 在 docker 缺失时返回 docker=false 且 200;
    2. datasets 只返回 .csv 且按 mtime 倒序;
    3. upload 拒绝 .exe 与超限文件,文件名清洗 + 重名自动加后缀;
    4. messages 的 SSE 依次出现 status / plan / done(monkeypatch 执行函数);
    5. approval 非法 action → 400,edit 空 plan → 400;
    6. charts 路径穿越(../、绝对路径、%2e%2e)→ 404;
    7. runs 列表遇到坏 JSON 行跳过不崩;
    8. _invoke_with_trace 合并调用方 callbacks 的回归测试。
"""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import main
import web.app as web_app
from trace import TraceCallbackHandler


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出目录与数据目录都指向 tmp_path,不碰真实 outputs/。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "outputs" / "sessions.jsonl")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


# ---------- 1. health ----------

def test_health_without_docker(client, monkeypatch):
    """docker CLI 缺失时 docker=false,接口仍 200,不抛 500。"""
    monkeypatch.setattr(web_app.shutil, "which", lambda name: None)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["docker"] is False
    assert body["checkpointer"] in ("sqlite", "memory")
    assert isinstance(body["llm_configured"], bool)
    assert isinstance(body["summary_enabled"], bool)


# ---------- 2. datasets ----------

def test_datasets_only_csv_sorted_by_mtime(client, tmp_path):
    data_dir = tmp_path / "data"
    old = data_dir / "old.csv"
    new = data_dir / "new.csv"
    txt = data_dir / "ignore.txt"
    old.write_bytes(b"a")
    txt.write_bytes(b"x")
    new.write_bytes(b"b")
    # 控制 mtime:old 早于 new,断言返回按 mtime 倒序
    os.utime(old, (1000000000, 1000000000))
    os.utime(new, (1700000000, 1700000000))

    resp = client.get("/api/datasets")
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()]
    assert names == ["new.csv", "old.csv"]  # txt 被过滤,且新的在前


# ---------- 3. upload ----------

def test_upload_rejects_non_csv(client):
    resp = client.post(
        "/api/datasets",
        files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "csv" in resp.json()["detail"].lower()


def test_upload_rejects_oversize(client, monkeypatch):
    # 把上限调小,避免测试里真分配 20MB
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 10)
    resp = client.post(
        "/api/datasets",
        files={"file": ("big.csv", io.BytesIO(b"x" * 11), "text/csv")},
    )
    assert resp.status_code == 400
    assert "上限" in resp.json()["detail"]


def test_upload_sanitizes_and_dedups(client, tmp_path):
    # 文件名含路径与非法字符:清洗后只剩白名单字符
    resp1 = client.post(
        "/api/datasets",
        files={"file": ("../我的 数据?.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
    )
    assert resp1.status_code == 200
    name1 = resp1.json()["name"]
    assert name1.endswith(".csv")
    assert "/" not in name1 and "?" not in name1 and " " not in name1
    assert (tmp_path / "data" / name1).is_file()

    # 同名再传一次:自动加后缀而不是覆盖
    resp2 = client.post(
        "/api/datasets",
        files={"file": ("../我的 数据?.csv", io.BytesIO(b"c,d\n"), "text/csv")},
    )
    assert resp2.status_code == 200
    name2 = resp2.json()["name"]
    assert name2 != name1
    assert name2.endswith(".csv")
    assert (tmp_path / "data" / name2).is_file()


# ---------- 4. messages SSE ----------

def _fake_run_agent_pending(question, thread_id, continue_session, progress_callback, last_run_id=""):
    """伪造 run_agent:推两个进度事件后返回「暂停在审批」的状态。"""
    progress_callback({"node": "planner", "phase": "start"})
    progress_callback({"node": "planner", "phase": "end"})
    pending = {
        "question": question,
        "dataset_name": "测试数据.csv",
        "plan": ["读取数据", "统计收入分布"],
        "reason": "需要先看分布",
    }
    return {
        "__interrupt__": (SimpleNamespace(value=pending),),
        "run_id": "",
        "final_answer": "",
        "error_analysis": "",
        "warnings": [],
    }


def test_messages_sse_emits_status_plan_done(client, monkeypatch):
    monkeypatch.setattr(web_app, "run_agent", _fake_run_agent_pending)
    # 不让测试碰真实检查点:伪造「会话不存在」走全新会话分支
    monkeypatch.setattr(web_app, "_session_exists", lambda tid: False)
    monkeypatch.setattr(web_app, "load_pending_interrupt", lambda tid: None)

    resp = client.post("/api/sessions/t1/messages", json={"question": "看一下分布"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    text = resp.text
    i_status = text.find("event: status")
    i_plan = text.find("event: plan")
    i_done = text.find("event: done")
    assert -1 < i_status < i_plan < i_done
    assert '"node": "planner"' in text
    assert '"dataset_name": "测试数据.csv"' in text


# ---------- 5. approval 参数校验 ----------

def test_approval_rejects_bad_action(client):
    resp = client.post("/api/sessions/t1/approval", json={"action": "hack"})
    assert resp.status_code == 400


def test_approval_edit_requires_nonempty_plan(client):
    resp = client.post("/api/sessions/t1/approval", json={"action": "edit", "plan": []})
    assert resp.status_code == 400
    resp2 = client.post(
        "/api/sessions/t1/approval", json={"action": "edit", "plan": ["", "  "]}
    )
    assert resp2.status_code == 400


# ---------- 6. charts 路径穿越 ----------

def _prepare_run(client, tmp_path):
    run_dir = tmp_path / "outputs" / "runs" / "rid1" / "charts"
    run_dir.mkdir(parents=True)
    (run_dir / "a.png").write_bytes(b"png")
    (tmp_path / "outputs" / "runs" / "rid1" / "result.json").write_text(
        "{}", encoding="utf-8"
    )
    # 目录外放一个「机密」文件,穿越成功就会读到它
    (tmp_path / "outputs" / "secret.txt").write_text("secret", encoding="utf-8")


def test_charts_path_traversal_blocked(client, tmp_path):
    _prepare_run(client, tmp_path)
    # 正常文件可以读
    assert client.get("/api/runs/rid1/charts/a.png").status_code == 200
    # 三种穿越形态全部 404
    assert client.get("/api/runs/rid1/charts/%2e%2e").status_code == 404
    assert client.get("/api/runs/rid1/charts/%2e%2e%2Fsecret.txt").status_code == 404
    assert client.get("/api/runs/rid1/charts/C%3A%5CWindows%5Cwin.ini").status_code == 404


# ---------- 7. runs 坏行容忍 ----------

def test_runs_skips_bad_json_lines(client, tmp_path):
    outputs = tmp_path / "outputs"
    (outputs / "index.jsonl").write_text(
        '{"run_id": "r1", "status": "success"}\n'
        "{坏行\n"
        '{"run_id": "r2", "status": "error"}\n',
        encoding="utf-8",
    )
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    ids = [item["run_id"] for item in resp.json()]
    assert ids == ["r2", "r1"]  # 倒序,坏行被跳过


# ---------- 8. _invoke_with_trace 合并 callbacks 回归 ----------

def test_invoke_with_trace_merges_callbacks(monkeypatch):
    """回归:旧实现用 callbacks=[handler] 覆盖,会把调用方注入的回调丢掉。"""
    captured = {}

    class _FakeGraph:
        def invoke(self, init_state, config=None):
            captured["callbacks"] = list(config.get("callbacks") or [])
            return {"ok": True}

    monkeypatch.setattr(main, "_get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(main, "write_trace", lambda *a, **k: "")
    monkeypatch.setattr(main, "trace_enabled", lambda: True)

    existing = object()  # 调用方注入的任意 handler(用哨兵对象区分于 trace handler)
    events: list[dict] = []
    main._invoke_with_trace(
        {}, {"callbacks": [existing]}, "tid", "q", progress_callback=events.append
    )
    callbacks = captured["callbacks"]
    # 调用方注入的 handler 保留在最前,进度回调与 trace handler 依次追加在后
    assert callbacks[0] is existing
    assert any(isinstance(c, main.ProgressCallbackHandler) for c in callbacks)
    assert sum(isinstance(c, TraceCallbackHandler) for c in callbacks) == 1


def test_progress_handler_filters_wrapper_chains():
    """进度回调只报业务节点(planner 等),LangGraph 包装层一律过滤。"""
    events: list[dict] = []
    handler = main.ProgressCallbackHandler(events.append)
    handler.on_chain_start({"name": "planner"}, {}, run_id="r1")
    # LangGraph 实际把节点名放在 kwargs["name"](serialized 为空),必须也能识别
    handler.on_chain_start({}, {}, run_id="r3", name="summarize")
    handler.on_chain_start({"name": "LangGraph"}, {}, run_id="r2")
    handler.on_chain_end({}, run_id="r1")
    handler.on_chain_end({}, run_id="r3")
    handler.on_chain_end({}, run_id="r2")
    assert events == [
        {"node": "planner", "phase": "start"},
        {"node": "summarize", "phase": "start"},
        {"node": "planner", "phase": "end"},
        {"node": "summarize", "phase": "end"},
    ]
