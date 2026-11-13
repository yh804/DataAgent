"""MySQL / SQLite 共用查询结果缓存测试。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pandas as pd
import pytest

import db as sqlite_db
import db_adapter
import db_cache
from db_adapter import MysqlAdapter, SqliteAdapter


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    path = tmp_path / "db_cache"
    monkeypatch.setattr(db_cache, "CACHE_DIR", path)
    monkeypatch.setenv("DB_CACHE", "1")
    monkeypatch.setenv("DB_CACHE_TTL_SECONDS", "600")
    return path


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        if sql.startswith("SELECT id, name FROM orders"):
            self.description = [("id",), ("name",)]
            self.rows = [(1, "A"), (2, "B")]
        else:
            self.description = None
            self.rows = []

    def fetchmany(self, size):
        return self.rows[:size]


class FakeConnection:
    def __init__(self):
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def test_mysql_same_normalized_sql_hits_cache_without_reconnecting(cache_dir, monkeypatch):
    connection = FakeConnection()
    connect_calls = []

    def fake_connect(**kwargs):
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(db_adapter, "pymysql", SimpleNamespace(connect=fake_connect))
    runtime = {"id": "mysql-test", "host": "h", "database": "d", "user": "u"}
    adapter = MysqlAdapter()

    first = adapter.run_select(runtime, "SELECT id, name FROM orders")
    second = adapter.run_select(runtime, "  SELECT   id, name\nFROM orders  ")

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["row_count"] == 2
    assert len(connect_calls) == 1
    pd.testing.assert_frame_equal(first["dataframe"], second["dataframe"])
    key = db_cache.cache_key("mysql-test", "SELECT id, name FROM orders")
    assert (cache_dir / "mysql-test" / f"{key}.csv").is_file()
    metadata = json.loads((cache_dir / "mysql-test" / f"{key}.json").read_text(encoding="utf-8"))
    assert metadata["sql"] == "SELECT id, name FROM orders"
    assert metadata["row_count"] == 2
    assert metadata["created_at"]


def test_sqlite_same_sql_hits_cache_without_requery(cache_dir, tmp_path, monkeypatch):
    db_path = tmp_path / "cache.db"
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        connection.executemany("INSERT INTO items VALUES (?, ?)", [(1, "A"), (2, "B")])
        connection.commit()
    runtime = {"id": "sqlite-test", "path": str(db_path)}
    adapter = SqliteAdapter()

    first = adapter.run_select(runtime, "SELECT id, name FROM items")

    def fail_if_queried(*_args, **_kwargs):
        raise AssertionError("缓存命中后不应再次访问 SQLite")

    monkeypatch.setattr(sqlite_db, "run_select", fail_if_queried)
    second = adapter.run_select(runtime, " SELECT   id, name\nFROM items ")

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["row_count"] == 2
    pd.testing.assert_frame_equal(first["dataframe"], second["dataframe"])


def test_expired_cache_does_not_hit(cache_dir, monkeypatch):
    sql = "SELECT 1"
    assert db_cache.save("db-test", sql, pd.DataFrame({"id": [1]})) is True
    _csv_path, metadata_path = db_cache._paths("db-test", sql)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["created_at"] = "2000-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setenv("DB_CACHE_TTL_SECONDS", "600")

    assert db_cache.load("db-test", sql) is None


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO items VALUES (1)",
        "UPDATE items SET name = 'x'",
        "DELETE FROM items",
        "CREATE TABLE other (id INTEGER)",
    ],
)
def test_write_statements_are_never_cached(cache_dir, sql):
    frame = pd.DataFrame({"id": [1]})

    assert db_cache.is_cacheable(sql) is False
    assert db_cache.save("db-test", sql, frame) is False
    assert db_cache.load("db-test", sql) is None
    assert not cache_dir.exists()


def test_result_over_50mb_limit_is_not_written(cache_dir, monkeypatch):
    assert db_cache.MAX_CACHE_BYTES == 50 * 1024 * 1024
    monkeypatch.setattr(db_cache, "MAX_CACHE_BYTES", 64)  # 用小阈值覆盖同一超限分支
    frame = pd.DataFrame({"payload": ["x" * 256]})

    assert db_cache.save("db-test", "SELECT payload FROM items", frame) is False
    assert not cache_dir.exists()


def test_db_cache_zero_disables_read_and_write(cache_dir, monkeypatch):
    sql = "SELECT 1"
    assert db_cache.save("db-test", sql, pd.DataFrame({"id": [1]})) is True
    csv_path, metadata_path = db_cache._paths("db-test", sql)
    original_csv = csv_path.read_bytes()
    original_metadata = metadata_path.read_bytes()
    monkeypatch.setenv("DB_CACHE", "0")

    assert db_cache.load("db-test", sql) is None
    assert db_cache.save("db-test", sql, pd.DataFrame({"id": [2]})) is False
    assert csv_path.read_bytes() == original_csv
    assert metadata_path.read_bytes() == original_metadata
