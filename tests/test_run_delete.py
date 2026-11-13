"""单条运行删除接口的离线回归测试(不联网、不起 Docker、不调 LLM)。

覆盖:
    1. 正常删除:归档目录消失、index 行移除、会话 last_run_id 清空、审计留痕;
    2. 重复删除 → 404;
    3. run_id 非法/含路径穿越 → 404,且不碰任何文件;
    4. 所属会话正在执行 → 409,归档与索引保持原样。
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

import web.app as web_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出目录与审计路径都指向 tmp_path。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "outputs" / "sessions.jsonl")
    monkeypatch.setattr(web_app, "DELETIONS_PATH", tmp_path / "outputs" / "deletions.jsonl")
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


def _seed_run(tmp_path, run_id: str = "20260101_000000_abcd", thread_id: str = "t-run"):
    """造一份最小运行归档 + index/sessions 引用,返回 (run_dir, 归档字节数)。"""
    outputs = tmp_path / "outputs"
    run_dir = outputs / "runs" / run_id
    charts = run_dir / "charts"
    charts.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps({"run_id": run_id, "thread_id": thread_id, "status": "success"}),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text("# 报告\n", encoding="utf-8")
    (charts / "chart.png").write_bytes(b"\x89PNG")
    bytes_freed = sum(p.stat().st_size for p in run_dir.rglob("*") if p.is_file())

    index_lines = [
        {"run_id": run_id, "thread_id": thread_id, "question": "问题"},
        {"run_id": "another_run", "thread_id": thread_id, "question": "另一个"},
    ]
    (outputs / "index.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in index_lines) + "\n",
        encoding="utf-8",
    )
    (outputs / "sessions.jsonl").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "title": "原始标题",
                "trusted_datasets": ["demo.csv"],
                "last_run_id": run_id,
                "updated_at": "2026-01-01T00:00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir, bytes_freed


def test_delete_run_removes_archive_index_and_session_ref(client, tmp_path):
    run_id = "20260101_000000_abcd"
    run_dir, expected_bytes = _seed_run(tmp_path, run_id=run_id)

    resp = client.delete(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["deleted"]["index_rows"] == 1
    assert body["deleted"]["sessions_touched"] == 1
    assert body["deleted"]["bytes_freed"] == expected_bytes

    # 归档目录已删
    assert not run_dir.exists()

    # index.jsonl 不再包含该 run,另一条保留
    index_rows = [
        json.loads(line)
        for line in (tmp_path / "outputs" / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["run_id"] for r in index_rows] == ["another_run"]

    # 会话只清空 last_run_id,其余字段原样保留
    sessions = [
        json.loads(line)
        for line in (tmp_path / "outputs" / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sessions[0]["last_run_id"] == ""
    assert sessions[0]["title"] == "原始标题"
    assert sessions[0]["trusted_datasets"] == ["demo.csv"]
    assert sessions[0]["updated_at"] == "2026-01-01T00:00:00"

    # 审计新增一条 kind="run"
    audit = [
        json.loads(line)
        for line in (tmp_path / "outputs" / "deletions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(audit) == 1
    assert audit[0]["kind"] == "run"
    assert audit[0]["run_id"] == run_id
    assert audit[0]["thread_id"] == "t-run"
    assert audit[0]["index_rows_removed"] == 1
    assert audit[0]["sessions_touched"] == 1
    assert audit[0]["bytes_freed"] == expected_bytes
    assert "ts" in audit[0]


def test_delete_run_twice_returns_404(client, tmp_path):
    run_id = "20260101_000000_abcd"
    _seed_run(tmp_path, run_id=run_id)

    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert client.delete(f"/api/runs/{run_id}").status_code == 404
    # 已删除后 GET 也自然 404,列表不再包含它
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    listed = [item["run_id"] for item in client.get("/api/runs").json()]
    assert run_id not in listed


def test_delete_run_rejects_invalid_and_traversal_ids(client, tmp_path):
    run_id = "20260101_000000_abcd"
    run_dir, _ = _seed_run(tmp_path, run_id=run_id)

    # 点号不在白名单内:直接 404,且不能动任何文件
    assert client.delete("/api/runs/bad.id").status_code == 404
    # 编码后的 ../:无论路由层是否接住,校验都必须拒绝
    assert client.delete("/api/runs/" + quote("../evil", safe="")).status_code == 404

    assert run_dir.exists()
    index_text = (tmp_path / "outputs" / "index.jsonl").read_text(encoding="utf-8")
    assert run_id in index_text


def test_delete_run_conflicts_when_session_running(client, tmp_path):
    run_id = "20260101_000000_abcd"
    thread_id = "t-running"
    run_dir, _ = _seed_run(tmp_path, run_id=run_id, thread_id=thread_id)

    web_app._RUNNING.add(thread_id)
    try:
        resp = client.delete(f"/api/runs/{run_id}")
        assert resp.status_code == 409
        assert "正在执行" in resp.json()["detail"]
        assert run_dir.exists()
    finally:
        web_app._RUNNING.discard(thread_id)
