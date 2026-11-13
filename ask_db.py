"""命令行入口：SQLite 数据库自然语言问答 → 导出 CSV → 复用分析 Agent 出报告。

用法：
    python ask_db.py "examples/ecommerce.db" "按渠道统计 GMV,并列出 Top5 商品"

流程：
    1. 校验数据库文件存在;
    2. 主进程读取表结构(只读连接);
    3. LLM 生成 SQL 并打印(用户可核对);
    4. run_select 执行查询,结果写成 UTF-8 CSV 到临时目录;
    5. SANDBOX_DATA_DIR 指向临时目录,调用 run_agent;
    6. 复用 analyze.build_report / write_report 生成报告(含「执行的 SQL」);
    7. 无论成功失败都清理临时目录。

为什么数据库访问只在主进程：
    沙箱以 --network none 启动,无法连接数据库;Agent 只消费导出的 CSV。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import analyze
import db_connections
import db_glossary
import db_semantics
from db import DEFAULT_MAX_ROWS, list_tables, run_select
from llm import GeneratedSQL, get_llm
from prompts import build_sql_system_prompt, build_sql_user_prompt
from trace import TraceCallbackHandler, trace_enabled, write_trace

# 导出给分析 Agent 的固定文件名：不含中文 / 空格,避免沙箱路径问题
RESULT_CSV_NAME = "query_result.csv"


def generate_sql(
    question: str,
    tables_schema: list[dict],
    config: dict | None = None,
    dialect: str = "sqlite",
    glossary: list[dict] | None = None,
    feedback: list[str] | None = None,
) -> GeneratedSQL:
    """调用 LLM 生成只读 SQL(结构化输出)。

    参数:
        question: 用户的自然语言问题。
        tables_schema: list_tables 的返回值(表名 / 列名 / 类型 / 人工说明)。
        config: 运行配置;传入含 TraceCallbackHandler 的 callbacks 时,
            本次 LLM 调用的耗时与 token 会被记录到 trace。
        dialect: SQL 方言,"sqlite"(默认)或 "mysql";Web 侧连接 MySQL 时传 "mysql"。
        glossary: 用户确认过的业务术语口径;为空时提示词与旧版一致。
        feedback: 执行前体检给出的风险原因;非空时要求按「先过滤/采样再聚合」重写。

    返回:
        GeneratedSQL(sql + explanation)。

    异常:
        Exception: LLM 调用失败或返回结构非法,由调用方转为退出码 2。
    """
    system_prompt = build_sql_system_prompt(dialect)
    user_prompt = build_sql_user_prompt(
        question, tables_schema, dialect=dialect, glossary=glossary
    )
    if feedback:
        reasons = "\n".join(f"- {reason}" for reason in feedback)
        user_prompt = (
            f"{user_prompt}\n\n"
            f"【执行前体检未通过】上一版 SQL 存在以下大表/重查询风险:\n{reasons}\n"
            "请重写 SQL:先按时间或状态 WHERE 过滤,或先 SELECT DISTINCT 取维度值再 JOIN 聚合;"
            "确需全表统计时用 LIMIT 采样,并在说明里注明「基于 N 行抽样」。"
        )
    # method="function_calling"：与项目其他结构化输出保持一致,DeepSeek 兼容
    structured_llm = get_llm().with_structured_output(
        GeneratedSQL, method="function_calling"
    )
    result = structured_llm.invoke(
        [
            ("system", system_prompt),
            ("human", user_prompt),
        ],
        config=config,
    )
    if result is None:
        raise RuntimeError("LLM 未返回可解析的 SQL")
    return result


def _connection_id_for_path(db_path: Path) -> str:
    """找出与该 SQLite 文件对应的已保存连接 id,用于读取表 / 列业务说明。

    Args:
        db_path: 已 resolve 的 SQLite 文件绝对路径。

    Returns:
        匹配的连接 id;没有登记过该文件时返回空串(说明为空,提示词行为不变)。
    """
    for record in db_connections.list_connections():
        if record.get("kind") != "sqlite":
            continue
        raw = str(record.get("path") or "")
        if not raw:
            continue
        try:
            if Path(raw).expanduser().resolve() == db_path:
                return str(record.get("id") or "")
        except OSError:
            # 路径非法(不存在 / 权限)直接跳过,不影响命令行主流程
            continue
    return ""


def main() -> int:
    """命令行入口。

    返回：
        0 = 分析成功;1 = 分析执行失败(报告仍会生成);
        2 = 参数错误 / SQL 安全校验或执行失败(未进入分析)。
    """
    parser = argparse.ArgumentParser(description="用自然语言查询 SQLite 并生成分析报告")
    parser.add_argument("db", help="SQLite 数据库文件路径(.db / .sqlite)")
    parser.add_argument("question", help="自然语言分析问题")
    args = parser.parse_args()

    # 先解析为绝对路径,避免后续 chdir 改变相对路径的含义
    db_path = Path(args.db).expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path = db_path.resolve()

    if not db_path.is_file():
        print(f"[ask_db] 数据库文件不存在: {db_path}", file=sys.stderr)
        return 2

    # 统一以项目根目录为工作目录：报告与沙箱产物稳定落到项目 outputs/
    os.chdir(analyze.PROJECT_ROOT)

    # ---------- 观测：本入口此前没有 trace,这里把 SQL 生成的 LLM 调用也记下来 ----------
    handler = TraceCallbackHandler() if trace_enabled() else None
    trace_config = {"callbacks": [handler]} if handler else None
    started_at = datetime.now()

    work_dir: Path | None = None
    try:
        # ---------- 1. 表结构：只读连接,供 LLM 生成 SQL ----------
        try:
            tables = list_tables(db_path)
        except (FileNotFoundError, sqlite3.Error) as exc:
            print(f"[ask_db] 读取数据库失败: {exc}", file=sys.stderr)
            return 2

        if not tables:
            print(f"[ask_db] 数据库中没有用户表: {db_path}", file=sys.stderr)
            return 2

        print(f"[ask_db] 检测到 {len(tables)} 张表: {[t['name'] for t in tables]}")

        # 该文件若在 Web 端登记过连接,合并人工填写的表 / 列说明与术语口径
        connection_id = _connection_id_for_path(db_path)
        glossary = db_glossary.terms_for_prompt(connection_id) if connection_id else None
        if connection_id:
            tables = db_semantics.merge_semantics(connection_id, tables)

        # ---------- 2. LLM 生成 SQL,先打印供用户核对 ----------
        try:
            generated = generate_sql(
                args.question, tables, config=trace_config, glossary=glossary
            )
        except Exception as exc:
            print(f"[ask_db] 生成 SQL 失败: {exc}", file=sys.stderr)
            return 2

        sql = generated.sql
        print(f"\n[ask_db] 生成的 SQL：\n{sql}\n")
        if generated.explanation:
            print(f"[ask_db] SQL 思路：{generated.explanation}")

        # ---------- 3. 主进程执行查询(安全校验 + 超时 + 行数上限) ----------
        work_dir = analyze.create_work_dir()
        try:
            result = run_select(db_path, sql, max_rows=DEFAULT_MAX_ROWS)
        except ValueError as exc:
            # SQL 安全校验失败：绝不执行
            print(f"[ask_db] SQL 安全校验失败: {exc}", file=sys.stderr)
            return 2
        except TimeoutError as exc:
            print(f"[ask_db] 查询超时: {exc}", file=sys.stderr)
            return 2
        except sqlite3.Error as exc:
            print(f"[ask_db] SQL 执行失败: {exc}", file=sys.stderr)
            return 2

        frame = result["dataframe"]
        csv_path = work_dir / RESULT_CSV_NAME
        frame.to_csv(csv_path, index=False, encoding="utf-8")
        print(f"[ask_db] 查询结果: {result['row_count']} 行 -> {csv_path.name}")

        # ---------- 4. 交给现有分析 Agent(沙箱只消费 CSV) ----------
        os.environ["SANDBOX_DATA_DIR"] = str(work_dir)

        from main import run_agent

        agent_question = f"分析 {RESULT_CSV_NAME}：{args.question}"
        print(f"[ask_db] 问题: {agent_question}")

        state = run_agent(agent_question, auto_approve=True)

        # ---------- 5. 报告：复用 analyze 的构建与落盘(含「执行的 SQL」) ----------
        now = datetime.now()
        report_text = analyze.build_report(
            db_path,
            args.question,
            state,
            now,
            analysis_name=RESULT_CSV_NAME,
            sql=sql,
        )
        report_path = analyze.write_report(report_text, now)

        final_answer = state.get("final_answer") or ""
        if final_answer:
            print(f"\n[ask_db] 结论：\n{final_answer}\n")
        elif state.get("error_analysis"):
            print(f"\n[ask_db] 分析失败：\n{state['error_analysis']}\n", file=sys.stderr)

        print(f"[ask_db] 报告: {report_path}")

        # 成功判定与沙箱执行结果对齐：失败时仍生成报告,但返回非零退出码
        success = (
            (state.get("exec_result") or {}).get("exit_status") == "success"
            and bool(final_answer)
        )
        return 0 if success else 1
    finally:
        # trace 只记元数据;写失败由 write_trace 自行吞掉,不影响退出码
        if handler is not None:
            write_trace(handler, "ask_db", args.question, started_at)
        if work_dir is not None:
            analyze.cleanup_data_dir(work_dir)


if __name__ == "__main__":
    sys.exit(main())
