"""会话硬删除的离线测试:不联网、不碰真实 outputs / 检查点。

覆盖(对应需求验收):
    1. 正常删除:sessions 两行 + 两个 trace(一个目标) + 一条 approvals;
       DELETE -> 200,计数正确;sessions.jsonl 只剩其他 thread;目标 trace 删除,
       其他 trace 保留;approvals 目标行 question 清空且 redacted=true;
    2. 检查点 delete_thread 被以正确 thread_id 调用;
    3. 非法 thread_id -> 400;thread 在 _RUNNING 中 -> 409;
    4. 会话与运行记录绑定:删会话连带删该 thread 的 runs 目录与 index 行,
       其他 thread 的运行不受影响;旧的 ?purge_reports=false 不再生效;
    5. 重复删除 -> 404;
    6. list_sessions 对含 ?? 的损坏标题返回兜底 title_display,正常标题原样返回;
    7. deletions.jsonl 追加一行且不含问题文本。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import web.app as web_app


class _FakeSnapshot:
    def __init__(self, values):
        self.values = values


class _FakeCheckpointer:
    def __init__(self, existing: set[str]):
        self.deleted: list[str] = []
        self._existing = existing

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)
        self._existing.discard(thread_id)


class _FakeGraph:
    """伪造图:只暴露 delete_session 需要的 get_state / checkpointer。"""

    def __init__(self, existing_tids: set[str]):
        self._existing = existing_tids
        self.checkpointer = _FakeCheckpointer(existing_tids)

    def get_state(self, config):
        tid = config["configurable"]["thread_id"]
        return _FakeSnapshot({"question": "q"} if tid in self._existing else {})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出/数据目录指向 tmp_path,检查点用伪造图。"""
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(web_app, "SESSIONS_PATH", outputs / "sessions.jsonl")
    monkeypatch.setattr(web_app, "APPROVALS_PATH", outputs / "approvals.jsonl")
    monkeypatch.setattr(web_app, "DELETIONS_PATH", outputs / "deletions.jsonl")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    outputs.mkdir()

    # 默认目标会话在检查点里存在
    fake = _FakeGraph(existing_tids={"t-target"})
    monkeypatch.setattr(web_app.agent_main, "_get_graph", lambda: fake)
    web_app._fake_graph = fake  # 测试里可访问
    return TestClient(web_app.app)


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------- 1. 正常删除 ----------

def test_delete_normal(client, tmp_path):
    outputs = tmp_path / "outputs"
    # sessions:目标 + 其他
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-target", "title": "会话一", "updated_at": "2026-01-01T00:00:00", "last_run_id": "r1"},
        {"thread_id": "t-other", "title": "会话二", "updated_at": "2026-01-01T00:01:00", "last_run_id": "r2"},
    ])
    # traces:目标 + 其他
    traces = outputs / "traces"
    traces.mkdir()
    (traces / "target_trace.json").write_text(
        json.dumps({"thread_id": "t-target", "steps": []}, ensure_ascii=False), encoding="utf-8")
    (traces / "other_trace.json").write_text(
        json.dumps({"thread_id": "t-other", "steps": []}, ensure_ascii=False), encoding="utf-8")
    # approvals:目标 + 其他
    _write_jsonl(web_app.APPROVALS_PATH, [
        {"ts": "2026-01-01T00:00:00", "thread_id": "t-target", "dataset": "x.csv",
         "question": "目标会话的问题原文", "level": "auto", "reasons": ["r"],
         "decided_by": "policy", "action": "auto"},
        {"ts": "2026-01-01T00:01:00", "thread_id": "t-other", "dataset": "y.csv",
         "question": "其他会话的问题原文", "level": "confirm", "reasons": ["首次"],
         "decided_by": "human", "action": "approve"},
    ])

    resp = client.delete("/api/sessions/t-target")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == {"sessions": 1, "traces": 1, "runs": 0, "approvals_redacted": 1}

    # sessions.jsonl 只剩其他 thread
    sessions = _read_jsonl(web_app.SESSIONS_PATH)
    assert [r["thread_id"] for r in sessions] == ["t-other"]

    # 目标 trace 删除,其他 trace 保留
    traces_left = {p.name for p in traces.glob("*.json")}
    assert traces_left == {"other_trace.json"}

    # approvals:目标行 question 清空 + redacted=true,其他行不变
    approvals = _read_jsonl(web_app.APPROVALS_PATH)
    target = next(r for r in approvals if r["thread_id"] == "t-target")
    assert target["question"] == "" and target["redacted"] is True
    assert target["reasons"] == ["r"]  # 决策理由保留
    other = next(r for r in approvals if r["thread_id"] == "t-other")
    assert other["question"] == "其他会话的问题原文"
    assert "redacted" not in other


# ---------- 2. 检查点 delete_thread 调用 ----------

def test_delete_invokes_checkpointer_delete_thread(client):
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-target", "title": "x", "updated_at": "2026-01-01T00:00:00"},
    ])
    resp = client.delete("/api/sessions/t-target")
    assert resp.status_code == 200
    assert web_app._fake_graph.checkpointer.deleted == ["t-target"]


# ---------- 3. 非法 id / 正在运行 ----------

def test_delete_invalid_thread_id_400(client):
    resp = client.delete("/api/sessions/ab")  # 长度不足
    assert resp.status_code == 400
    resp2 = client.delete("/api/sessions/has space!")
    assert resp2.status_code == 400


def test_delete_running_session_409(client, monkeypatch):
    web_app._RUNNING.add("t-target")
    try:
        resp = client.delete("/api/sessions/t-target")
        assert resp.status_code == 409
        assert "正在执行" in resp.json()["detail"]
    finally:
        web_app._RUNNING.discard("t-target")


# ---------- 4. 删除会话连带删除运行记录 ----------

def _setup_runs(outputs: Path):
    runs = outputs / "runs"
    (runs / "r1" / "charts").mkdir(parents=True)
    (runs / "r1" / "result.json").write_text("{}", encoding="utf-8")
    (runs / "r2" / "charts").mkdir(parents=True)
    (runs / "r2" / "result.json").write_text("{}", encoding="utf-8")
    _write_jsonl(outputs / "index.jsonl", [
        {"run_id": "r1", "thread_id": "t-target", "status": "success", "question": "q1"},
        {"run_id": "r2", "thread_id": "t-other", "status": "success", "question": "q2"},
    ])


def test_delete_session_removes_its_runs(client, tmp_path):
    """会话与运行记录绑定:删会话必须连带删 runs 目录与 index 行。"""
    outputs = tmp_path / "outputs"
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-target", "title": "x", "updated_at": "2026-01-01T00:00:00"},
    ])
    _setup_runs(outputs)

    resp = client.delete("/api/sessions/t-target")
    assert resp.status_code == 200
    assert resp.json()["deleted"]["runs"] == 1
    assert not (outputs / "runs" / "r1").exists()  # 目标 run 目录被删
    assert (outputs / "runs" / "r2").is_dir()  # 其他会话的 run 不受影响
    index = _read_jsonl(outputs / "index.jsonl")
    assert {r["run_id"] for r in index} == {"r2"}  # 目标 index 行消失


def test_delete_session_ignores_legacy_purge_flag(client, tmp_path):
    """兼容旧链接:?purge_reports=false 不再能保留运行记录。"""
    outputs = tmp_path / "outputs"
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-target", "title": "x", "updated_at": "2026-01-01T00:00:00"},
    ])
    _setup_runs(outputs)

    resp = client.delete("/api/sessions/t-target?purge_reports=false")
    assert resp.status_code == 200
    assert resp.json()["deleted"]["runs"] == 1
    assert not (outputs / "runs" / "r1").exists()


# ---------- 5. 重复删除 -> 404 ----------

def test_delete_twice_returns_404(client, tmp_path):
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-target", "title": "x", "updated_at": "2026-01-01T00:00:00"},
    ])
    assert client.delete("/api/sessions/t-target").status_code == 200
    # 第二次:检查点里已不存在,sessions 里也没有 -> 404
    assert client.delete("/api/sessions/t-target").status_code == 404


# ---------- 6. 列表标题兜底 ----------

def test_list_sessions_corrupted_title_fallback(client, tmp_path):
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-bad12345", "title": "??? ????.csv ?????", "updated_at": "2026-01-01T00:00:00"},
        {"thread_id": "t-good", "title": "正常标题", "updated_at": "2026-01-01T00:01:00"},
    ])
    items = client.get("/api/sessions").json()
    by_id = {it["thread_id"]: it for it in items}
    assert by_id["t-bad12345"]["title_display"] == "会话 t-bad1"
    assert by_id["t-bad12345"]["title_corrupted"] is True
    # 库里原值不变
    assert by_id["t-bad12345"]["title"] == "??? ????.csv ?????"
    # 正常标题原样返回
    assert by_id["t-good"]["title_display"] == "正常标题"
    assert by_id["t-good"]["title_corrupted"] is False


# ---------- 7. deletions.jsonl 审计不含问题文本 ----------

def test_deletions_audit_has_no_question_text(client, tmp_path):
    _write_jsonl(web_app.SESSIONS_PATH, [
        {"thread_id": "t-target", "title": "x", "updated_at": "2026-01-01T00:00:00"},
    ])
    client.delete("/api/sessions/t-target")
    rows = _read_jsonl(web_app.DELETIONS_PATH)
    assert len(rows) == 1
    row = rows[0]
    assert row["thread_id"] == "t-target"
    assert "sessions_removed" in row and "traces_removed" in row
    # 审计行里绝对不能出现问题原文(只有 id 与计数)
    blob = json.dumps(row, ensure_ascii=False)
    assert "问题" not in blob
    assert "question" not in blob
