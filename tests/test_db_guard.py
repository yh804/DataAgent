"""执行前体检 db_guard.assess 的规则测试。"""

from __future__ import annotations

import db_guard

# customer_purchase 的真实规模口径(约 48.9 万行),文本列示例
CUSTOMER_STATS = {
    "rows": 494024,
    "size_bytes": 120 * 1024 * 1024,
    "text_columns": ["客户名称", "客户编号", "购买渠道"],
}
SMALL_STATS = {
    "rows": 1200,
    "size_bytes": 2 * 1024 * 1024,
    "text_columns": ["客户名称"],
}


def test_small_table_query_is_ok():
    sql = "SELECT 客户名称, AVG(金额) AS 平均收入 FROM customer_purchase GROUP BY 客户名称"

    result = db_guard.assess(sql, SMALL_STATS)

    assert result == {"level": "ok", "reasons": []}


def test_big_table_group_by_text_column_warns():
    sql = (
        "SELECT `客户名称`, AVG(`金额`) AS 平均收入 "
        "FROM `customer_purchase` WHERE `日期` >= '2025-01-01' GROUP BY `客户名称`"
    )

    result = db_guard.assess(sql, CUSTOMER_STATS)

    assert result["level"] == "warn"
    assert len(result["reasons"]) == 1
    assert "GROUP BY 了文本列" in result["reasons"][0]
    assert "客户名称" in result["reasons"][0]


def test_three_count_distinct_warns():
    sql = (
        "SELECT COUNT(DISTINCT 客户名称) AS a, COUNT(DISTINCT 客户编号) AS b, "
        "COUNT(DISTINCT 购买渠道) AS c FROM customer_purchase WHERE 日期 >= '2025-01-01'"
    )

    result = db_guard.assess(sql, SMALL_STATS)

    assert result["level"] == "warn"
    assert "COUNT(DISTINCT" in result["reasons"][0]


def test_big_table_without_where_or_limit_warns():
    sql = "SELECT AVG(金额) AS 平均收入 FROM customer_purchase"

    result = db_guard.assess(sql, CUSTOMER_STATS)

    assert result["level"] == "warn"
    assert "既无 WHERE 过滤也无 LIMIT 采样" in result["reasons"][0]


def test_two_rules_hit_block():
    # 大表 + GROUP BY 文本列 + 无 WHERE/LIMIT,同时命中规则 1 与规则 3
    sql = "SELECT 客户名称, AVG(金额) AS 平均收入 FROM customer_purchase GROUP BY 客户名称"

    result = db_guard.assess(sql, CUSTOMER_STATS)

    assert result["level"] == "block"
    assert len(result["reasons"]) == 2


def test_rewrite_with_filter_is_no_longer_blocked():
    # 体检后按「先过滤再聚合」重写的 SQL:不再是 block,可正常执行
    sql = (
        "SELECT 客户名称, AVG(金额) AS 平均收入 FROM customer_purchase "
        "WHERE 日期 >= '2025-01-01' GROUP BY 客户名称"
    )

    result = db_guard.assess(sql, CUSTOMER_STATS)

    assert result["level"] == "warn"


def test_literals_and_comments_do_not_trigger():
    sql = (
        "SELECT 'group by 客户名称' AS 说明, COUNT(DISTINCT 客户名称) AS a "
        "FROM customer_purchase -- where 条件写在注释里不算数\n"
        "/* limit 200000 也是注释 */"
    )

    result = db_guard.assess(sql, CUSTOMER_STATS)

    # 文本只在字符串与注释里出现,不能误判成 GROUP BY / WHERE / LIMIT
    assert result["level"] == "warn"
    assert "既无 WHERE 过滤也无 LIMIT 采样" in result["reasons"][0]
    assert not any("GROUP BY 了文本列" in reason for reason in result["reasons"])


def test_missing_table_stats_only_applies_count_distinct_rule():
    sql = (
        "SELECT COUNT(DISTINCT a) AS x, COUNT(DISTINCT b) AS y, "
        "COUNT(DISTINCT c) AS z FROM customer_purchase"
    )

    result = db_guard.assess(sql, None)

    assert result["level"] == "warn"
    assert len(result["reasons"]) == 1


def test_empty_sql_is_ok():
    assert db_guard.assess("", CUSTOMER_STATS) == {"level": "ok", "reasons": []}
