"""db.validate_select 的安全校验与规范化测试(离线,不连数据库)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db import validate_select  # noqa: E402


def _sql(text: str) -> str:
    """把结果压成单行,便于断言末尾 LIMIT。"""
    return " ".join(text.split())


def test_plain_select_gets_limit():
    """正常 SELECT 未写 LIMIT 时自动补上。"""
    out = validate_select("SELECT * FROM orders")
    assert _sql(out).endswith("LIMIT 10000")


def test_with_cte_is_allowed_with_limit():
    """WITH(CTE)同样是只读查询,允许并自动补 LIMIT。"""
    out = validate_select("WITH t AS (SELECT * FROM orders) SELECT * FROM t")
    assert out.upper().startswith("WITH")
    assert _sql(out).endswith("LIMIT 10000")


def test_multiple_statements_rejected():
    """分号后仍有语句:多语句必须拒绝。"""
    with pytest.raises(ValueError, match="多条语句"):
        validate_select("SELECT * FROM orders; SELECT 1")


def test_line_comment_is_stripped():
    """行注释先剔除,注释里的关键字不触发拦截。"""
    out = validate_select("-- 只看 orders\nSELECT * FROM orders")
    assert "--" not in out
    assert "SELECT * FROM orders" in out


def test_block_comment_is_stripped():
    """块注释整体剔除,不参与关键字匹配。"""
    out = validate_select("/* drop table orders */ SELECT * FROM orders")
    assert "/*" not in out
    assert "DROP" not in out.upper()


def test_keyword_inside_string_literal_is_conservatively_rejected():
    """字符串字面量里出现 delete 时按保守策略拒绝。

    为什么保留这个行为:_strip_comments 刻意保留引号内容,
    _FORBIDDEN_RE 会一并扫描,宁可误杀也不放过任何可能的写操作。
    """
    with pytest.raises(ValueError, match="禁止关键字"):
        validate_select("SELECT 'delete' AS note FROM orders")


def test_trailing_limit_is_kept():
    """末尾已有 LIMIT 时不再重复追加。"""
    out = validate_select("SELECT * FROM orders LIMIT 50")
    assert _sql(out).endswith("LIMIT 50")
    assert "LIMIT 10000" not in _sql(out)


def test_trailing_limit_offset_is_kept():
    """LIMIT ... OFFSET 也视为已有行数限制。"""
    out = validate_select("SELECT * FROM orders LIMIT 20 OFFSET 10")
    assert "LIMIT 20 OFFSET 10" in _sql(out)


def test_subquery_limit_does_not_count_as_outer_limit():
    """子查询里的 LIMIT 不能限制整体行数,外层仍要补。"""
    out = validate_select("SELECT * FROM (SELECT * FROM orders LIMIT 5)")
    assert _sql(out).endswith("LIMIT 10000")


def test_pragma_rejected():
    """PRAGMA 属于副作用语句,直接拒绝。"""
    with pytest.raises(ValueError):
        validate_select("PRAGMA table_info(orders)")


def test_attach_rejected():
    """ATTACH 会挂载其他库,拒绝。"""
    with pytest.raises(ValueError):
        validate_select("ATTACH DATABASE 'x.db' AS other")


def test_write_statement_rejected():
    """UPDATE 等写操作拒绝。"""
    with pytest.raises(ValueError, match="SELECT / WITH"):
        validate_select("UPDATE orders SET amount = 0")


def test_chinese_table_name_allowed():
    """中文表名 / 列名是合法标识符,必须放行。"""
    out = validate_select('SELECT * FROM "订单表"')
    assert "订单表" in out
    assert _sql(out).endswith("LIMIT 10000")


def test_trailing_semicolon_is_stripped():
    """允许结尾单个分号,去掉后再补 LIMIT。"""
    out = validate_select("SELECT * FROM orders;")
    assert ";" not in out
    assert _sql(out).endswith("LIMIT 10000")


def test_empty_statement_rejected():
    """空语句 / 纯注释拒绝执行。"""
    with pytest.raises(ValueError, match="SQL 为空"):
        validate_select("   ")
    with pytest.raises(ValueError, match="SQL 为空"):
        validate_select("-- 只有注释")
