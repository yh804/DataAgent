"""运行归档补强测试:db_* 数据集归档 source.csv 与 db_query 字段。"""

from __future__ import annotations

import json

import archive
import db_connections

DB_CSV = "db_MySQL_20261113_101500.csv"


def _prepare(tmp_path, monkeypatch):
    """隔离归档目录、审计文件与数据目录。"""
    monkeypatch.setattr(archive, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(
        db_connections, "QUERIES_PATH", tmp_path / "outputs" / "db_queries.jsonl"
    )
    data_dir = tmp_path / "processed"
    data_dir.mkdir()
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(data_dir))
    return data_dir


def test_db_dataset_archive_has_source_and_db_query(tmp_path, monkeypatch):
    data_dir = _prepare(tmp_path, monkeypatch)
    (data_dir / DB_CSV).write_text("渠道,GMV\n自然流量,120\n", encoding="utf-8-sig")
    db_connections.record_query(
        {
            "db_id": "db000001",
            "db_label": "MySQL",
            "question": "按渠道统计 GMV",
            "sql": "SELECT 渠道, SUM(GMV) FROM orders GROUP BY 渠道",
            "ok": True,
            "status": "ok",
            "row_count": 1,
            "elapsed_ms": 9,
            "csv_name": DB_CSV,
        }
    )

    run = {"dataset": DB_CSV, "question": "按渠道统计 GMV"}
    archive._write_run_archive("rid_db", run, "# 报告", "")

    run_dir = tmp_path / "runs" / "rid_db"
    assert (run_dir / "source.csv").is_file()
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["db_query"] == {
        "db_label": "MySQL",
        "sql": "SELECT 渠道, SUM(GMV) FROM orders GROUP BY 渠道",
        "row_count": 1,
        "csv_name": DB_CSV,
    }
    assert result["db_label"] == "MySQL"


def test_plain_dataset_archive_has_no_db_query(tmp_path, monkeypatch):
    data_dir = _prepare(tmp_path, monkeypatch)
    (data_dir / "sales.csv").write_text("a,b\n1,2\n", encoding="utf-8-sig")

    archive._write_run_archive("rid_plain", {"dataset": "sales.csv"}, "# 报告", "")

    run_dir = tmp_path / "runs" / "rid_plain"
    assert not (run_dir / "source.csv").exists()
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert "db_query" not in result
