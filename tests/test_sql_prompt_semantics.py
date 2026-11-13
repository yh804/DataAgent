"""SQL 提示词的表 / 列说明测试:有说明才注入,无说明逐字节不变。"""

from __future__ import annotations

from prompts import build_sql_user_prompt

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
    """改造前的实现,用来锁死无说明时的逐字节兼容。"""
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


def test_prompt_without_semantics_is_byte_identical():
    for dialect in ("sqlite", "mysql"):
        assert build_sql_user_prompt("按渠道看客单价", TABLES, dialect) == _legacy_prompt(
            "按渠道看客单价", TABLES, dialect
        )
    assert build_sql_user_prompt("空表", [], "sqlite") == _legacy_prompt("空表", [], "sqlite")


def test_prompt_injects_table_and_column_descriptions():
    tables = [
        {
            "name": "orders",
            "type": "BASE TABLE",
            "description": "订单主表",
            "columns": [
                {"name": "id", "type": "INT"},
                {"name": "amount", "type": "REAL", "description": "成交金额"},
            ],
        }
    ]
    prompt = build_sql_user_prompt("按渠道看客单价", tables, "mysql")

    assert "BASE TABLE orders (说明:订单主表):id (INT)、amount (REAL), 成交金额" in prompt
    assert "未标注的表列只按名称与类型推断,不得臆造业务含义。" in prompt


def test_empty_description_is_treated_as_absent():
    tables = [
        {
            "name": "orders",
            "type": "BASE TABLE",
            "description": "   ",
            "columns": [
                {"name": "id", "type": "INT", "description": ""},
            ],
        }
    ]
    assert build_sql_user_prompt("x", tables) == _legacy_prompt(
        "x",
        [
            {
                "name": "orders",
                "type": "BASE TABLE",
                "columns": [{"name": "id", "type": "INT"}],
            }
        ],
    )
