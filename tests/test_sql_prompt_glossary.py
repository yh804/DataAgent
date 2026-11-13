"""SQL 提示词的术语段测试:无术语时逐字节不变,有术语时注入口径。"""

from __future__ import annotations

from prompts import build_glossary_user_prompt, build_sql_user_prompt

TABLES = [
    {
        "name": "orders",
        "type": "BASE TABLE",
        "columns": [
            {"name": "id", "type": "INT"},
            {"name": "amount", "type": "REAL"},
        ],
    }
]


def _legacy_prompt(question: str, tables: list[dict], dialect: str = "sqlite") -> str:
    """改造前的实现,锁死无术语时的逐字节兼容。"""
    schema_lines = []
    for table in tables:
        table_type = table.get("type") or "BASE TABLE"
        columns = "、".join(
            f"{column['name']} ({column['type']})" for column in table["columns"]
        )
        schema_lines.append(f"- {table_type} {table['name']}:{columns}")
    schema_text = "\n".join(schema_lines) if schema_lines else "(无可用表)"
    return (
        f"用户问题:{question}\n\n"
        f"目标数据库方言:{str(dialect or 'sqlite').lower()}\n\n"
        f"可用表结构:\n{schema_text}\n\n"
        f"请生成一条只读 SQL 查询。"
    )


def test_prompt_without_glossary_is_byte_identical():
    question = "按渠道看客单价"
    assert build_sql_user_prompt(question, TABLES, "mysql", None) == _legacy_prompt(
        question, TABLES, "mysql"
    )
    assert build_sql_user_prompt(question, TABLES, "mysql", []) == _legacy_prompt(
        question, TABLES, "mysql"
    )
    # 只有空词也视同没有术语
    assert build_sql_user_prompt(
        question, TABLES, "mysql", [{"term": "  ", "definition": "x"}]
    ) == _legacy_prompt(question, TABLES, "mysql")


def test_prompt_injects_confirmed_terms():
    glossary = [
        {"term": "客单价", "definition": "成交金额 / 订单数", "sql_hint": "orders.amount"},
        {"term": "活跃用户", "definition": "近 30 天有下单的用户", "sql_hint": ""},
    ]
    prompt = build_sql_user_prompt("按渠道看客单价", TABLES, "mysql", glossary)

    assert "业务术语(用户确认过,优先按此口径):" in prompt
    assert "- 客单价:成交金额 / 订单数(SQL 口径:orders.amount)" in prompt
    assert "- 活跃用户:近 30 天有下单的用户" in prompt
    assert "未列出的术语不得臆造;不确定时按字段名直译并说明假设。" in prompt


def test_glossary_check_prompt_lists_schema_and_terms():
    prompt = build_glossary_user_prompt(
        "按渠道看客单价", TABLES, [{"term": "客单价", "definition": "平均订单金额"}]
    )
    assert "BASE TABLE orders" in prompt
    assert "- 客单价:平均订单金额" in prompt

    empty = build_glossary_user_prompt("按渠道看客单价", TABLES, [])
    assert "已知术语表(已确认口径):(暂无)" in empty
