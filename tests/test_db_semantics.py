"""表 / 列业务说明存储测试:截断、空串删除、原子写与合并。"""

from __future__ import annotations

import json

import pytest

import db_semantics


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "outputs" / "db_semantics.json"
    monkeypatch.setattr(db_semantics, "SEMANTICS_PATH", path)
    return path


def test_upsert_and_get_roundtrip(store):
    saved = db_semantics.upsert_table_semantics(
        "conn1", "orders", "订单主表", {"amount": "成交金额"}
    )
    assert saved == {"description": "订单主表", "columns": {"amount": "成交金额"}}

    loaded = db_semantics.get_table_semantics("conn1", "orders")
    assert loaded == {"description": "订单主表", "columns": {"amount": "成交金额"}}
    assert db_semantics.get_table_semantics("conn1", "missing") is None

    payload = json.loads(store.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["databases"]["conn1"]["tables"]["orders"]["description"] == "订单主表"
    # 原子写不应留下临时文件
    assert [p.name for p in store.parent.iterdir()] == [store.name]


def test_truncation_limits(store):
    db_semantics.upsert_table_semantics(
        "conn1", "orders", "表" * 500, {"amount": "列" * 500}
    )
    saved = db_semantics.get_table_semantics("conn1", "orders")
    assert len(saved["description"]) == db_semantics.TABLE_DESC_MAX
    assert len(saved["columns"]["amount"]) == db_semantics.COLUMN_DESC_MAX


def test_empty_string_deletes_entries(store):
    db_semantics.upsert_table_semantics("conn1", "orders", "订单", {"amount": "金额"})
    # 只清空表说明:表记录保留,列说明仍在
    db_semantics.upsert_table_semantics("conn1", "orders", "", {"amount": "金额"})
    assert db_semantics.get_table_semantics("conn1", "orders") == {
        "description": "",
        "columns": {"amount": "金额"},
    }
    # 列说明也清空:整表记录移除
    db_semantics.upsert_table_semantics("conn1", "orders", "", {"amount": ""})
    assert db_semantics.get_table_semantics("conn1", "orders") is None
    assert db_semantics.load_semantics()["databases"] == {}


def test_delete_table_semantics(store):
    db_semantics.upsert_table_semantics("conn1", "orders", "订单")
    assert db_semantics.delete_table_semantics("conn1", "orders") is True
    assert db_semantics.delete_table_semantics("conn1", "orders") is False


def test_merge_semantics_does_not_mutate_input(store):
    db_semantics.upsert_table_semantics(
        "conn1", "orders", "订单主表", {"amount": "成交金额"}
    )
    tables = [
        {
            "name": "orders",
            "type": "BASE TABLE",
            "columns": [
                {"name": "id", "type": "INT"},
                {"name": "amount", "type": "DECIMAL"},
            ],
        },
        {"name": "other", "type": "VIEW", "columns": [{"name": "x", "type": "TEXT"}]},
    ]

    merged = db_semantics.merge_semantics("conn1", tables)

    assert merged[0]["description"] == "订单主表"
    assert merged[0]["columns"][1]["description"] == "成交金额"
    assert "description" not in merged[0]["columns"][0]
    assert "description" not in merged[1]
    assert "description" not in tables[0]


def test_load_semantics_tolerates_broken_file(store):
    store.parent.mkdir(parents=True)
    store.write_text("{not json", encoding="utf-8")
    assert db_semantics.load_semantics()["databases"] == {}
