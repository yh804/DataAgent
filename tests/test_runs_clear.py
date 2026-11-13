"""运行记录批量清空接口的离线测试(不联网、不起 Docker、不调 LLM)。

覆盖:
    1. DELETE /api/runs 清空:归档目录、index 行、sessions 里的 last_run_id 引用;
    2. 只存在于磁盘、index 里没有的孤儿目录也会被清掉;
    3. 有任何会话在跑 -> 409,文件一个都不动;
    4. 本来就是空的时候 -> 200 且计数全 0(幂等,便于前端无脑调用)。
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import web.app as web_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出目录、会话与审计路径全部指向 tmp_path。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "outputs" / "sessions.jsonl")
    monkeypatch.setattr(web_app, "DELETIONS_PATH", tmp_path / "outputs" / "deletions.jsonl")
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


def _mkdir_run(outputs, run_id: str) -> None:
    run_dir = outputs / "runs" / run_id
    (run_dir / "charts").mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps({"run_id": run_id, "status": "success"}), encoding="utf-8"
    )
    (run_dir / "report.md").write_text("# 报告\n", encoding="utf-8")


def _write_index(outputs, rows: list[dict]) -> None:
    (outputs / "index.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8"
    )


def _write_sessions(outputs, rows: list[dict]) -> None:
    (outputs / "sessions.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8"
    )


def _read_jsonl(path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_clear_all_runs_removes_archives_index_and_refs(client, tmp_path):
    outputs = tmp_path / "outputs"
    _mkdir_run(outputs, "r1")
    _mkdir_run(outputs, "r2")
    # 孤儿目录:磁盘上有、index 里没有,应该一起清掉
    _mkdir_run(outputs, "20260101_000000_orphan")
    _write_index(outputs, [
        {"run_id": "r1", "thread_id": "t-a", "status": "success"},
        {"run_id": "r2", "thread_id": "t-b", "status": "failed"},
    ])
    _write_sessions(outputs, [
        {"thread_id": "t-a", "title": "A", "last_run_id": "r1"},
        {"thread_id": "t-b", "title": "B", "last_run_id": "r2"},
        {"thread_id": "t-c", "title": "C", "last_run_id": ""},
    ])

    resp = client.delete("/api/runs")
    assert resp.status_code == 200
    deleted = resp.json()["deleted"]
    assert deleted["runs"] == 3
    assert deleted["index_rows"] == 2
    assert deleted["sessions_touched"] == 2
    assert deleted["bytes_freed"] > 0

    assert not (outputs / "runs" / "r1").exists()
    assert not (outputs / "runs" / "r2").exists()
    assert not (outputs / "runs" / "20260101_000000_orphan").exists()
    assert _read_jsonl(outputs / "index.jsonl") == []
    # 会话本身保留,只把 last_run_id 清空
    sessions = _read_jsonl(outputs / "sessions.jsonl")
    assert [s["thread_id"] for s in sessions] == ["t-a", "t-b", "t-c"]
    assert all(s["last_run_id"] == "" for s in sessions)
    assert sessions[0]["title"] == "A"

    audit = _read_jsonl(web_app.DELETIONS_PATH)
    assert len(audit) == 1
    assert audit[0]["kind"] == "all_runs"
    assert audit[0]["runs_removed"] == 3


def test_clear_all_runs_conflicts_when_anything_running(client, tmp_path):
    outputs = tmp_path / "outputs"
    _mkdir_run(outputs, "r1")
    _write_index(outputs, [{"run_id": "r1", "thread_id": "t-a"}])

    web_app._RUNNING.add("t-a")
    try:
        resp = client.delete("/api/runs")
        assert resp.status_code == 409
        assert "正在执行" in resp.json()["detail"]
    finally:
        web_app._RUNNING.discard("t-a")

    # 409 时一个文件都不能动
    assert (outputs / "runs" / "r1").is_dir()
    assert _read_jsonl(outputs / "index.jsonl") == [{"run_id": "r1", "thread_id": "t-a"}]


def test_clear_all_runs_is_idempotent_when_empty(client, tmp_path):
    outputs = tmp_path / "outputs"
    resp = client.delete("/api/runs")
    assert resp.status_code == 200
    assert resp.json()["deleted"]["runs"] == 0
    assert resp.json()["deleted"]["index_rows"] == 0
