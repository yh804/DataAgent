"""术语表与待澄清记录存储测试。"""

from __future__ import annotations

import json

import pytest

import db_glossary


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(db_glossary, "GLOSSARY_PATH", tmp_path / "db_glossary.json")
    monkeypatch.setattr(db_glossary, "PENDING_PATH", tmp_path / "db_pending.jsonl")
    return tmp_path


def test_upsert_and_terms_for_prompt(paths):
    db_glossary.upsert_term("conn1", "客单价", "成交金额 / 订单数", "orders.amount")
    db_glossary.upsert_term("conn1", "  活跃用户 ", "近 30 天有下单的用户", "")

    terms = db_glossary.terms_for_prompt("conn1")
    assert [item["term"] for item in terms] == ["客单价", "活跃用户"]
    assert terms[0]["definition"] == "成交金额 / 订单数"
    assert terms[0]["sql_hint"] == "orders.amount"
    # 连接之间互相隔离
    assert db_glossary.terms_for_prompt("conn2") == []

    payload = json.loads(db_glossary.GLOSSARY_PATH.read_text(encoding="utf-8"))
    assert payload["databases"]["conn1"]["terms"]["客单价"]["definition"] == "成交金额 / 订单数"


def test_upsert_ignores_empty_term(paths):
    assert db_glossary.upsert_term("conn1", "   ", "x") == {}
    assert db_glossary.get_terms("conn1") == {}


def test_pending_overwrite_and_clear(paths):
    db_glossary.save_pending(
        {"thread_id": "t1", "database_id": "c1", "question": "q1", "terms": [{"term": "a"}]}
    )
    db_glossary.save_pending(
        {"thread_id": "t2", "database_id": "c1", "question": "q2", "terms": []}
    )
    db_glossary.save_pending(
        {"thread_id": "t1", "database_id": "c1", "question": "q3", "terms": []}
    )

    assert db_glossary.load_pending("t1")["question"] == "q3"
    assert db_glossary.load_pending("t2")["question"] == "q2"

    db_glossary.clear_pending("t1")
    assert db_glossary.load_pending("t1") is None
    assert db_glossary.load_pending("t2") is not None
