"""数据库查询历史 API 测试:倒序、无密码、run_id 补写与清空审计。"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import db_connections
import web.app as web_app
import web.routers.databases as databases_router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离审计文件与删除审计,避免污染真实 outputs。"""
    monkeypatch.setattr(
        db_connections, "QUERIES_PATH", tmp_path / "outputs" / "db_queries.jsonl"
    )
    monkeypatch.setattr(
        databases_router, "DELETIONS_PATH", tmp_path / "outputs" / "deletions.jsonl"
    )
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


def _write_records(monkeypatch) -> tuple[str, str]:
    """写入一成功一失败两条记录,时间戳递增,返回 (成功 id, 失败 id)。"""
    stamps = iter(["2026-11-13T10:00:01+08:00", "2026-11-13T10:00:02+08:00"])
    monkeypatch.setattr(db_connections, "_now", lambda: next(stamps))
    ok_id = db_connections.record_query(
        {
            "db_id": "db000001",
            "db_label": "MySQL",
            "question": "按渠道统计 GMV",
            "sql": "SELECT 渠道, SUM(金额) FROM orders GROUP BY 渠道",
            "ok": True,
            "status": "ok",
            "row_count": 3,
            "elapsed_ms": 12,
            "error": "",
            "csv_name": "db_MySQL_20261113_100001.csv",
        }
    )
    fail_id = db_connections.record_query(
        {
            "db_id": "db000001",
            "db_label": "MySQL",
            "question": "按渠道统计退款",
            "sql": "SELECT 退款 FROM orders",
            "ok": False,
            "status": "failed",
            "row_count": 0,
            "elapsed_ms": 8,
            "error": "Unknown column '退款'",
            # 敏感字段必须被白名单挡掉
            "password": "super-secret",
            "dsn": "mysql://u:super-secret@127.0.0.1:3306/ecommerce",
        }
    )
    return ok_id, fail_id


def test_queries_desc_and_without_password(client, monkeypatch):
    ok_id, fail_id = _write_records(monkeypatch)

    resp = client.get("/api/db/queries")
    assert resp.status_code == 200
    body = resp.json()

    # 时间倒序:后写入的失败记录在前
    assert [item["query_id"] for item in body] == [fail_id, ok_id]
    assert body[0]["status"] == "failed"
    assert body[0]["error"] == "Unknown column '退款'"
    assert body[1]["csv_name"] == "db_MySQL_20261113_100001.csv"
    assert body[1]["db_label"] == "MySQL"

    # 任何接口返回都不能出现密码 / 连接串
    text = json.dumps(body, ensure_ascii=False)
    assert "super-secret" not in text
    assert "password" not in text
    assert "dsn" not in text


def test_success_query_gets_run_id_once(client, monkeypatch):
    ok_id, _fail_id = _write_records(monkeypatch)

    assert db_connections.attach_run_id(ok_id, "20261113_100010_abcdef") is True
    # 同一个 run 只补一次:第二次(无论 run 是否相同)不再改写
    assert db_connections.attach_run_id(ok_id, "20261113_100011_abcdef") is False

    body = client.get("/api/db/queries").json()
    success = next(item for item in body if item["query_id"] == ok_id)
    assert success["run_id"] == "20261113_100010_abcdef"


def test_limit_and_db_filter(client, monkeypatch):
    ok_id, fail_id = _write_records(monkeypatch)

    body = client.get("/api/db/queries?limit=1").json()
    assert [item["query_id"] for item in body] == [fail_id]

    assert client.get("/api/db/queries?db_id=not-exist").json() == []
    assert len(client.get("/api/db/queries?db_id=db000001").json()) == 2


def test_delete_connection_keeps_history_marked(tmp_path, monkeypatch):
    """删除连接不删查询历史,只在记录上标记 db_deleted。"""
    monkeypatch.setattr(db_connections, "QUERIES_PATH", tmp_path / "db_queries.jsonl")
    monkeypatch.setattr(db_connections, "CONNECTIONS_PATH", tmp_path / "databases.jsonl")
    monkeypatch.setattr(db_connections, "CREDENTIALS_PATH", tmp_path / "credentials.json")

    connection = db_connections.create_connection(
        {
            "label": "MySQL",
            "kind": "mysql",
            "host": "127.0.0.1",
            "user": "reader",
            "database": "ecommerce",
        },
        "super-secret",
    )
    db_connections.record_query(
        {
            "db_id": connection["id"],
            "db_label": "MySQL",
            "question": "统计订单数",
            "sql": "SELECT COUNT(*) FROM orders",
            "ok": True,
            "status": "ok",
        }
    )

    assert db_connections.delete_connection(connection["id"]) is True

    rows = db_connections.read_queries()
    assert len(rows) == 1
    assert rows[0]["db_deleted"] is True
    assert "super-secret" not in json.dumps(rows, ensure_ascii=False)


def test_delete_clears_audit_only(client, monkeypatch, tmp_path):
    _write_records(monkeypatch)

    resp = client.delete("/api/db/queries")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert client.get("/api/db/queries").json() == []

    # 删除审计:只记条数与类型,便于回溯
    lines = (
        tmp_path / "outputs" / "deletions.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["kind"] == "db_queries"
    assert record["removed"] == 2
