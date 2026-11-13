"""数据库管理 API 测试:CRUD、表/字段浏览、409/404/400 语义。"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import db_connections
import web.app as web_app
import web.routers.databases as databases_router
from db_adapter import AdapterError


class FakeAdapter:
    """可配置的适配器替身,避免测试真的建连。"""

    def __init__(self):
        self.last_runtime: dict = {}

    def test(self, runtime):
        self.last_runtime = runtime
        return {"ok": True, "version": "8.0.36", "elapsed_ms": 4.2}

    def list_tables(self, runtime):
        self.last_runtime = runtime
        return [
            {
                "name": "orders",
                "type": "BASE TABLE",
                "columns": [
                    {"name": "订单号", "type": "varchar", "nullable": False, "key": "PRI"},
                    {"name": "支付金额", "type": "decimal", "nullable": True, "key": ""},
                ],
            },
            {"name": "v_daily_gmv", "type": "VIEW", "columns": []},
        ]

    def describe_table(self, runtime, name):
        for table in self.list_tables(runtime):
            if table["name"] == name:
                return table
        raise ValueError(f"表或视图不存在: {name}")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_connections, "CONNECTIONS_PATH", tmp_path / "outputs" / "databases.jsonl")
    monkeypatch.setattr(db_connections, "CREDENTIALS_PATH", tmp_path / "outputs" / "db_credentials.json")
    monkeypatch.setattr(db_connections, "QUERIES_PATH", tmp_path / "outputs" / "db_queries.jsonl")
    fake = FakeAdapter()
    monkeypatch.setattr(databases_router, "get_adapter", lambda _kind: fake)
    with TestClient(web_app.app) as test_client:
        yield test_client, fake


def _mysql_payload(**overrides):
    payload = {
        "label": "电商生产库",
        "kind": "mysql",
        "host": "127.0.0.1",
        "port": 3306,
        "database": "ecommerce",
        "user": "agent_ro",
        "password": "secret",
        "ssl": False,
    }
    payload.update(overrides)
    return payload


def test_crud_and_metadata_browsing(client):
    http, fake = client

    created = http.post("/api/databases", json=_mysql_payload())
    assert created.status_code == 200
    record = created.json()
    assert record["password_set"] is True
    assert "password" not in record

    listed = http.get("/api/databases")
    assert listed.status_code == 200
    assert listed.json()[0]["label"] == "电商生产库"
    assert "secret" not in listed.text

    tested = http.post("/api/databases/test", json={"id": record["id"], "password": ""})
    assert tested.status_code == 200
    body = tested.json()
    assert body["version"] == "8.0.36"
    assert fake.last_runtime["password"] == "secret"  # 密码留空时沿用服务端保存值

    tables = http.get(f"/api/databases/{record['id']}/tables")
    assert tables.status_code == 200
    assert tables.json()["tables"][0]["name"] == "orders"
    assert tables.json()["tables"][1]["type"] == "VIEW"

    columns = http.get(f"/api/databases/{record['id']}/tables/orders")
    assert columns.status_code == 200
    assert columns.json()["table"]["columns"][0]["key"] == "PRI"

    status = http.get("/api/databases").json()[0]["last_status"]
    assert status == "ok"

    deleted = http.delete(f"/api/databases/{record['id']}")
    assert deleted.status_code == 200
    assert http.get(f"/api/databases/{record['id']}/tables").status_code == 404


def test_duplicate_label_returns_409(client):
    http, _fake = client
    assert http.post("/api/databases", json=_mysql_payload()).status_code == 200

    duplicated = http.post("/api/databases", json=_mysql_payload(host="other-host"))
    assert duplicated.status_code == 409
    assert "已存在" in duplicated.json()["detail"]


def test_invalid_id_and_missing_resources(client):
    http, _fake = client

    assert http.get("/api/databases/bad!/tables").status_code == 400
    assert http.get("/api/databases/doesnotexist/tables").status_code == 404
    assert http.delete("/api/databases/doesnotexist").status_code == 404

    record = http.post("/api/databases", json=_mysql_payload()).json()
    assert http.get(f"/api/databases/{record['id']}/tables/nope").status_code == 404


def test_validation_errors_are_400(client):
    http, _fake = client

    assert http.post("/api/databases", json=_mysql_payload(label="")).status_code == 400
    assert http.post("/api/databases", json=_mysql_payload(database="")).status_code == 400
    assert (
        http.post(
            "/api/databases/test",
            json={"label": "x", "kind": "mysql", "host": "h", "user": "", "database": "d"},
        ).status_code
        == 400
    )


def test_connection_error_is_400_not_500(client, monkeypatch):
    http, _fake = client

    class BrokenAdapter(FakeAdapter):
        def test(self, runtime):
            raise AdapterError("MySQL 用户名或密码错误(1045),请检查账号与密码")

    monkeypatch.setattr(databases_router, "get_adapter", lambda _kind: BrokenAdapter())
    response = http.post("/api/databases/test", json=_mysql_payload())
    assert response.status_code == 400
    assert "1045" in response.json()["detail"]

    record = http.post("/api/databases", json=_mysql_payload()).json()
    failed = http.post("/api/databases/test", json={"id": record["id"], "password": ""})
    assert failed.status_code == 400
    assert http.get("/api/databases").json()[0]["last_status"] == "error"


def test_update_requires_existing_id_and_keeps_password(client):
    http, _fake = client
    record = http.post("/api/databases", json=_mysql_payload()).json()

    updated = http.put(
        f"/api/databases/{record['id']}",
        json=_mysql_payload(label="改名后的库", password=""),
    )
    assert updated.status_code == 200
    assert updated.json()["label"] == "改名后的库"
    assert db_connections.get_password(record["id"]) == "secret"

    assert http.put("/api/databases/doesnotexist", json=_mysql_payload()).status_code == 404
