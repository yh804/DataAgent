"""db_connections 单元测试:增删查、凭据分离、坏 JSONL 容错。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import db_connections


def test_default_paths_live_under_project_root():
    """默认存储路径必须落在项目根内:曾把根算成上级目录,建连时报 PermissionError。"""
    project_root = Path(db_connections.__file__).resolve().parent
    for path in (
        db_connections.CONNECTIONS_PATH,
        db_connections.CREDENTIALS_PATH,
        db_connections.QUERIES_PATH,
    ):
        assert path.parent == project_root / "outputs"


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """把三个存储文件都指向临时目录,避免污染真实 outputs。"""
    monkeypatch.setattr(db_connections, "CONNECTIONS_PATH", tmp_path / "outputs" / "databases.jsonl")
    monkeypatch.setattr(db_connections, "CREDENTIALS_PATH", tmp_path / "outputs" / "db_credentials.json")
    monkeypatch.setattr(db_connections, "QUERIES_PATH", tmp_path / "outputs" / "db_queries.jsonl")
    return tmp_path / "outputs"


def test_create_list_and_password_never_in_metadata(isolated_paths):
    record = db_connections.create_connection(
        {
            "label": "电商生产库",
            "kind": "mysql",
            "host": "127.0.0.1",
            "port": 3306,
            "database": "ecommerce",
            "user": "agent_ro",
        },
        "super-secret",
    )

    assert record["password_set"] is True
    assert "password" not in record

    connections = db_connections.list_connections()
    assert len(connections) == 1
    assert connections[0]["label"] == "电商生产库"
    assert connections[0]["password_set"] is True

    metadata_text = (isolated_paths / "databases.jsonl").read_text(encoding="utf-8")
    assert "super-secret" not in metadata_text
    assert db_connections.get_password(record["id"]) == "super-secret"


def test_update_with_blank_password_keeps_secret(isolated_paths):
    record = db_connections.create_connection(
        {"label": "库 A", "kind": "mysql", "host": "h", "database": "d", "user": "u"},
        "old-secret",
    )

    updated = db_connections.update_connection(
        record["id"],
        {"label": "库 A2", "kind": "mysql", "host": "h2", "database": "d", "user": "u"},
        password=None,
    )

    assert updated["label"] == "库 A2"
    assert updated["host"] == "h2"
    assert db_connections.get_password(record["id"]) == "old-secret"

    db_connections.update_connection(
        record["id"],
        {"label": "库 A2", "kind": "mysql", "host": "h2", "database": "d", "user": "u"},
        password="new-secret",
    )
    assert db_connections.get_password(record["id"]) == "new-secret"


def test_delete_removes_metadata_and_password(isolated_paths):
    record = db_connections.create_connection(
        {"label": "待删库", "kind": "mysql", "host": "h", "database": "d", "user": "u"},
        "secret",
    )

    assert db_connections.delete_connection(record["id"]) is True
    assert db_connections.list_connections() == []
    assert db_connections.get_password(record["id"]) == ""
    credentials = json.loads((isolated_paths / "db_credentials.json").read_text(encoding="utf-8"))
    assert credentials == {}


def test_duplicate_label_rejected(isolated_paths):
    payload = {"label": "重复名", "kind": "mysql", "host": "h", "database": "d", "user": "u"}
    db_connections.create_connection(payload, "s1")

    with pytest.raises(db_connections.DuplicateConnectionError):
        db_connections.create_connection(payload, "s2")


def test_bad_jsonl_lines_are_skipped(isolated_paths):
    path = isolated_paths / "databases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {"id": "abcd1234", "label": "好连接", "kind": "mysql", "last_status": "ok"}
    path.write_text(
        json.dumps(good, ensure_ascii=False) + "\n"
        "这不是 JSON\n"
        '{"id": "broken"\n',
        encoding="utf-8",
    )

    connections = db_connections.list_connections()
    assert len(connections) == 1
    assert connections[0]["id"] == "abcd1234"


def test_query_audit_has_no_password(isolated_paths):
    db_connections.append_query_audit(
        {
            "db_id": "abcd1234",
            "question": "按渠道统计 GMV",
            "sql": "SELECT 1",
            "row_count": 3,
            "ok": True,
            "cached": True,
            "password": "must-not-appear",
        }
    )

    lines = (isolated_paths / "db_queries.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["db_id"] == "abcd1234"
    assert record["row_count"] == 3
    assert record["cached"] is True
    assert "password" not in record
    assert "must-not-appear" not in lines[0]


def test_invalid_payload_raises(isolated_paths):
    with pytest.raises(ValueError, match="库名"):
        db_connections.create_connection(
            {"label": "坏库", "kind": "mysql", "host": "h", "database": "", "user": "u"},
            "s",
        )
