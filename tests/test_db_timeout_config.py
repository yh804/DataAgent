"""数据库查询超时环境变量与 MySQL 超时提示测试。"""

from __future__ import annotations

import pytest

import db
import db_adapter


def test_mysql_timeout_defaults_to_30000(monkeypatch):
    monkeypatch.delenv("MYSQL_QUERY_TIMEOUT_MS", raising=False)

    assert db_adapter._mysql_timeout_from_env() == 30000


def test_mysql_timeout_environment_override(monkeypatch):
    monkeypatch.setenv("MYSQL_QUERY_TIMEOUT_MS", "45000")

    assert db_adapter._mysql_timeout_from_env() == 45000


@pytest.mark.parametrize("value", ["", "abc", "1.5", "0", "999", "300001", "-1000"])
def test_mysql_timeout_invalid_values_fall_back_to_30000(monkeypatch, value):
    monkeypatch.setenv("MYSQL_QUERY_TIMEOUT_MS", value)

    assert db_adapter._mysql_timeout_from_env() == 30000


@pytest.mark.parametrize(("value", "expected"), [("1000", 1000), ("300000", 300000)])
def test_mysql_timeout_accepts_configured_boundaries(monkeypatch, value, expected):
    monkeypatch.setenv("MYSQL_QUERY_TIMEOUT_MS", value)

    assert db_adapter._mysql_timeout_from_env() == expected


def test_mysql_socket_read_timeout_is_five_seconds_later():
    assert db_adapter.MYSQL_READ_TIMEOUT == db_adapter.MYSQL_QUERY_TIMEOUT_MS / 1000 + 5


def test_mysql_timeout_message_contains_elapsed_stats_and_advice():
    error = db_adapter._translate_mysql_error(
        Exception(3024, "timeout"),
        elapsed=15.23,
        table_stats=(1234567, 768.5),
    )

    message = str(error)
    assert isinstance(error, TimeoutError)
    assert "已等待 15.2s" in message
    assert "该表约 1234567 行/768.5MB" in message
    assert "建议加 WHERE 缩小范围" in message
    assert "innodb_buffer_pool_size" in message


def test_mysql_timeout_message_omits_unavailable_table_stats():
    error = db_adapter._translate_mysql_error(Exception(3024, "timeout"), elapsed=30.0)

    message = str(error)
    assert "已等待 30.0s" in message
    assert "该表约" not in message
    assert "建议加 WHERE 缩小范围" in message


def test_sqlite_timeout_defaults_to_30(monkeypatch):
    monkeypatch.delenv("DB_QUERY_TIMEOUT_SECONDS", raising=False)

    assert db._sqlite_timeout_from_env() == 30


def test_sqlite_timeout_environment_override(monkeypatch):
    monkeypatch.setenv("DB_QUERY_TIMEOUT_SECONDS", "12.5")

    assert db._sqlite_timeout_from_env() == 12.5


@pytest.mark.parametrize("value", ["", "abc", "0", "-1", "nan", "inf"])
def test_sqlite_timeout_invalid_values_fall_back_to_30(monkeypatch, value):
    monkeypatch.setenv("DB_QUERY_TIMEOUT_SECONDS", value)

    assert db._sqlite_timeout_from_env() == 30
