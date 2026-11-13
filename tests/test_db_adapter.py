"""db_adapter 单元测试:只读会话、元数据、截断、类型转换与异常翻译。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import db_adapter
from db_adapter import AdapterError, MysqlAdapter


class FakeCursor:
    """最小 pymysql cursor 替身:记录 execute,按 SQL 返回预置结果。"""

    def __init__(self, connection: "FakeConnection", rows=None):
        self.connection = connection
        self.description = None
        self._rows = list(rows or [])

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        self._rows = self.connection.responses.get(sql, self.connection.default_rows)
        if sql.startswith("SELECT"):
            self.description = self.connection.description

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def fetchmany(self, size):
        return self._rows[:size]


class FakeConnection:
    """最小 pymysql connection 替身。"""

    def __init__(self, responses=None, default_rows=None):
        self.executed: list[tuple[str, object]] = []
        self.responses = responses or {}
        self.default_rows = default_rows
        self.query_sql = ""
        self.description = [("id",), ("amount",), ("d",), ("ts",), ("raw",)]
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def _install(monkeypatch, connection):
    """把 db_adapter.pymysql 换成假模块,并捕获 connect 参数。"""
    captured: dict = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return connection

    monkeypatch.setattr(db_adapter, "pymysql", SimpleNamespace(connect=fake_connect))
    return captured


def test_mysql_connect_uses_readonly_session(monkeypatch):
    connection = FakeConnection(default_rows=[("8.0.36",)])
    captured = _install(monkeypatch, connection)

    result = MysqlAdapter().test(
        {"host": "127.0.0.1", "port": 3307, "user": "agent_ro", "password": "secret", "database": "ecommerce"}
    )

    assert result["version"] == "8.0.36"
    assert captured["charset"] == "utf8mb4"
    assert captured["connect_timeout"] == 5
    assert captured["read_timeout"] == 35
    assert captured["autocommit"] is False
    assert captured["port"] == 3307
    assert connection.executed[0][0] == "SET SESSION TRANSACTION READ ONLY"
    assert connection.closed is True


def test_mysql_list_tables_marks_views_and_columns(monkeypatch):
    connection = FakeConnection(
        responses={
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME": [
                ("orders", "BASE TABLE"),
                ("v_daily_gmv", "VIEW"),
            ],
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY "
            "FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION": [
                ("orders", "订单号", "varchar", "NO", "PRI"),
                ("orders", "支付金额", "decimal", "YES", ""),
            ],
        }
    )
    _install(monkeypatch, connection)

    tables = MysqlAdapter().list_tables(
        {"host": "h", "user": "u", "password": "p", "database": "ecommerce"}
    )

    assert [table["name"] for table in tables] == ["orders", "v_daily_gmv"]
    assert tables[0]["type"] == "BASE TABLE"
    assert tables[1]["type"] == "VIEW"
    assert tables[0]["columns"][0] == {"name": "订单号", "type": "varchar", "nullable": False, "key": "PRI"}
    assert tables[0]["columns"][1]["nullable"] is True


def test_mysql_run_select_truncates_and_converts_types(monkeypatch):
    monkeypatch.setenv("DB_CACHE", "0")  # 本用例只验证驱动结果转换,不写真实缓存目录
    rows = [
        (1, Decimal("12.50"), date(2024, 1, 2), datetime(2024, 1, 2, 3, 4, 5), b"abc"),
        (2, Decimal("0"), date(2024, 1, 3), datetime(2024, 1, 3, 0, 0, 0), b"def"),
        (3, Decimal("1"), date(2024, 1, 4), datetime(2024, 1, 4, 0, 0, 0), b"ghi"),
    ]
    connection = FakeConnection(default_rows=rows)
    _install(monkeypatch, connection)

    result = MysqlAdapter().run_select(
        {"host": "h", "user": "u", "password": "p", "database": "d"},
        "SELECT id, amount, d, ts, raw FROM t",
        max_rows=2,
    )

    assert result["truncated"] is True
    assert result["row_count"] == 2
    frame = result["dataframe"]
    assert list(frame.columns) == ["id", "amount", "d", "ts", "raw"]
    assert frame.iloc[0].tolist() == [
        1,
        "12.50",
        "2024-01-02",
        "2024-01-02 03:04:05",
        "abc",
    ]
    # validate_select 会自动追加 LIMIT;fetchmany 按 max_rows+1 取以判断截断
    assert connection.executed[-1][0].startswith("SELECT id, amount, d, ts, raw FROM t")
    assert connection.executed[-1][0].endswith("LIMIT 2")


@pytest.mark.parametrize(
    ("code", "keyword"),
    [(1045, "密码"), (2003, "无法连接"), (1049, "数据库不存在")],
)
def test_mysql_error_translation(monkeypatch, code, keyword):
    def fake_connect(**_kwargs):
        raise Exception(code, "driver message")

    monkeypatch.setattr(db_adapter, "pymysql", SimpleNamespace(connect=fake_connect))

    with pytest.raises(AdapterError) as exc_info:
        MysqlAdapter().test({"host": "h", "user": "u", "password": "p", "database": "d"})

    assert keyword in str(exc_info.value)


def test_mysql_missing_dependency(monkeypatch):
    monkeypatch.setattr(db_adapter, "pymysql", None)

    with pytest.raises(AdapterError) as exc_info:
        MysqlAdapter().test({"host": "h", "user": "u", "password": "p", "database": "d"})

    assert "pymysql" in str(exc_info.value)


def test_mysql_run_select_rejects_write_sql(monkeypatch):
    connection = FakeConnection(default_rows=[])
    _install(monkeypatch, connection)

    with pytest.raises(ValueError):
        MysqlAdapter().run_select(
            {"host": "h", "user": "u", "password": "p", "database": "d"},
            "DELETE FROM orders",
        )

    assert connection.executed == []
