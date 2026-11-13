"""MySQL 真实集成测试:仅在设置 MYSQL_TEST_DSN 时运行。

DSN 形如:mysql://agent_ro:password@127.0.0.1:3306/ecommerce
"""

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

import pytest

from db_adapter import MysqlAdapter, get_adapter

MYSQL_TEST_DSN = os.getenv("MYSQL_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not MYSQL_TEST_DSN, reason="未设置 MYSQL_TEST_DSN,跳过真实 MySQL 集成测试"
)


def _dsn_config() -> dict:
    """把 DSN 解析成适配器运行时配置。"""
    parsed = urlparse(MYSQL_TEST_DSN)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "/").lstrip("/") or "ecommerce",
    }


def test_real_mysql_test_and_metadata():
    adapter = get_adapter("mysql")
    config = _dsn_config()

    result = adapter.test(config)
    assert result["ok"] is True
    assert result["version"]

    tables = adapter.list_tables(config)
    names = {table["name"] for table in tables}
    assert "orders" in names
    assert "order_items" in names


def test_real_mysql_query_matches_reference_gmv():
    adapter = MysqlAdapter()
    config = _dsn_config()

    orders = adapter.run_select(config, "SELECT COUNT(*) AS cnt FROM orders", max_rows=10000)
    assert int(orders["dataframe"].iloc[0]["cnt"]) == 2000

    gmv = adapter.run_select(
        config,
        "SELECT ROUND(SUM(`支付金额`), 2) AS gmv FROM `orders` "
        "WHERE `支付时间` IS NOT NULL "
        "AND `订单状态` IN ('已支付','已发货','已完成','退款中','已退款') "
        "AND `是否测试单` = '否' AND `是否刷单` = '否'",
        max_rows=10000,
    )
    assert float(gmv["dataframe"].iloc[0]["gmv"]) == pytest.approx(1193284.77, abs=0.01)


def test_real_mysql_rejects_write_sql():
    adapter = MysqlAdapter()
    with pytest.raises(ValueError):
        adapter.run_select(_dsn_config(), "UPDATE orders SET 订单状态 = '已取消'", max_rows=100)
