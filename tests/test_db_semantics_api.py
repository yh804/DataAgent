"""表 / 列说明接口测试:读写、404 与非法列 400。"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import db_connections
import db_semantics
import web.app as web_app
import web.routers.databases as databases_router


class FakeAdapter:
    """固定返回 orders 表与两个字段,避免真实建连。"""

    def list_tables(self, _runtime):
        return [
            {
                "name": "orders",
                "type": "BASE TABLE",
                "columns": [
                    {"name": "订单号", "type": "varchar", "nullable": False, "key": "PRI"},
                    {"name": "支付金额", "type": "decimal", "nullable": True, "key": ""},
                ],
            }
        ]

    def describe_table(self, runtime, name):
        for table in self.list_tables(runtime):
            if table["name"] == name:
                return table
        raise ValueError(f"表或视图不存在: {name}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_connections, "CONNECTIONS_PATH", tmp_path / "databases.jsonl")
    monkeypatch.setattr(db_connections, "CREDENTIALS_PATH", tmp_path / "db_credentials.json")
    monkeypatch.setattr(db_connections, "QUERIES_PATH", tmp_path / "db_queries.jsonl")
    monkeypatch.setattr(db_semantics, "SEMANTICS_PATH", tmp_path / "db_semantics.json")
    monkeypatch.setattr(databases_router, "get_adapter", lambda _kind: FakeAdapter())
    with TestClient(web_app.app) as test_client:
        yield test_client


def _create(client) -> str:
    response = client.post(
        "/api/databases",
        json={
            "label": "电商库",
            "kind": "mysql",
            "host": "127.0.0.1",
            "database": "ecommerce",
            "user": "ro",
            "password": "secret",
        },
    )
    return response.json()["id"]


def test_put_get_delete_semantics(client):
    connection_id = _create(client)

    empty = client.get(f"/api/databases/{connection_id}/semantics")
    assert empty.status_code == 200
    assert empty.json()["tables"] == {}

    saved = client.put(
        f"/api/databases/{connection_id}/semantics/orders",
        json={"description": "订单主表", "columns": {"支付金额": "成交金额"}},
    )
    assert saved.status_code == 200
    assert saved.json()["description"] == "订单主表"

    loaded = client.get(f"/api/databases/{connection_id}/semantics").json()
    assert loaded["tables"]["orders"]["description"] == "订单主表"
    assert loaded["tables"]["orders"]["columns"]["支付金额"] == "成交金额"

    deleted = client.delete(f"/api/databases/{connection_id}/semantics/orders")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/databases/{connection_id}/semantics").json()["tables"] == {}


def test_missing_connection_is_404(client):
    assert client.get("/api/databases/doesnotexist/semantics").status_code == 404
    assert (
        client.put(
            "/api/databases/doesnotexist/semantics/orders",
            json={"description": "x"},
        ).status_code
        == 404
    )
    assert client.delete("/api/databases/doesnotexist/semantics/orders").status_code == 404


def test_missing_table_is_404(client):
    connection_id = _create(client)
    response = client.put(
        f"/api/databases/{connection_id}/semantics/not_exist",
        json={"description": "x"},
    )
    assert response.status_code == 404


def test_unknown_column_is_400_with_names(client):
    connection_id = _create(client)
    response = client.put(
        f"/api/databases/{connection_id}/semantics/orders",
        json={"description": "订单", "columns": {"金额": "成交金额"}},
    )
    assert response.status_code == 400
    assert "金额" in response.json()["detail"]
    # 校验失败时不应落盘任何内容
    assert client.get(f"/api/databases/{connection_id}/semantics").json()["tables"] == {}
