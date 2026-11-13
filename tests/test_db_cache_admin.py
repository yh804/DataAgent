"""数据库缓存治理脚本测试:统计、过期清理、指定连接清空与 dry-run。"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db_cache_admin as admin

import db_cache


@pytest.fixture()
def cache_root(tmp_path, monkeypatch):
    """把缓存目录指向临时目录,并固定 TTL / 上限。"""
    root = tmp_path / "db_cache"
    monkeypatch.setattr(db_cache, "CACHE_DIR", root)
    monkeypatch.setenv("DB_CACHE", "1")
    monkeypatch.setenv("DB_CACHE_TTL_SECONDS", "600")
    monkeypatch.setenv("DB_CACHE_MAX_MB", "200")
    return root


def _seed(db_id: str, sql: str, created_at: str | None = None) -> tuple[Path, Path]:
    """写入一条缓存;可改写 created_at 模拟过期条目。"""
    assert db_cache.save(db_id, sql, pd.DataFrame({"id": [1]})) is True
    csv_path, metadata_path = db_cache._paths(db_id, sql)
    if created_at is not None:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["created_at"] = created_at
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return csv_path, metadata_path


def test_stats_reports_count_size_and_groups(cache_root, capsys):
    _seed("dbaaaa", "SELECT 1", "2026-11-01T10:00:00+00:00")
    _seed("dbaaaa", "SELECT 2", "2026-11-02T10:00:00+00:00")
    _seed("dbbbbb", "SELECT 3", "2026-11-03T10:00:00+00:00")

    assert admin.main(["--stats"]) == 0

    output = capsys.readouterr().out
    assert "条目数: 3" in output
    assert "dbaaaa: 2 条" in output
    assert "dbbbbb: 1 条" in output
    assert "最早 created_at: 2026-11-01T10:00:00+00:00" in output
    assert "最晚 created_at: 2026-11-03T10:00:00+00:00" in output


def test_purge_only_removes_expired_entries(cache_root, capsys):
    expired_csv, expired_json = _seed("dbaaaa", "SELECT 1", "2000-01-01T00:00:00+00:00")
    fresh_csv, fresh_json = _seed("dbaaaa", "SELECT 2")

    assert admin.main(["--purge-expired", "--apply"]) == 0

    assert not expired_csv.exists()
    assert not expired_json.exists()
    assert fresh_csv.exists()
    assert fresh_json.exists()
    assert "过期条目: 删除 1 条" in capsys.readouterr().out


def test_dry_run_deletes_nothing(cache_root, capsys):
    expired_csv, expired_json = _seed("dbaaaa", "SELECT 1", "2000-01-01T00:00:00+00:00")

    assert admin.main(["--purge-expired"]) == 0

    assert expired_csv.exists()
    assert expired_json.exists()
    output = capsys.readouterr().out
    assert "[dry-run] 过期条目: 将删除 1 条" in output


def test_clear_only_removes_given_db(cache_root, capsys):
    _seed("dbaaaa", "SELECT 1")
    other_csv, other_json = _seed("dbbbbb", "SELECT 1")

    assert admin.main(["--clear", "--db-id", "dbaaaa", "--apply"]) == 0

    assert not (cache_root / "dbaaaa").exists()
    assert other_csv.exists()
    assert other_json.exists()
    assert "清空 dbaaaa: 删除 1 条" in capsys.readouterr().out


def test_clear_all_without_db_id(cache_root):
    _seed("dbaaaa", "SELECT 1")
    _seed("dbbbbb", "SELECT 1")

    assert admin.main(["--clear", "--apply"]) == 0

    assert not (cache_root / "dbaaaa").exists()
    assert not (cache_root / "dbbbbb").exists()


def test_size_limit_trims_oldest_entries(cache_root, monkeypatch):
    now = datetime.now(UTC)
    old_csv, old_json = _seed(
        "dbaaaa", "SELECT 1", (now - timedelta(seconds=60)).isoformat()
    )
    mid_csv, _mid_json = _seed(
        "dbaaaa", "SELECT 2", (now - timedelta(seconds=30)).isoformat()
    )
    new_csv, _new_json = _seed(
        "dbaaaa", "SELECT 3", (now - timedelta(seconds=5)).isoformat()
    )
    total = sum(path.stat().st_size for path in cache_root.rglob("*") if path.is_file())
    oldest = old_csv.stat().st_size + old_json.stat().st_size
    limit_bytes = total - oldest + 1
    monkeypatch.setenv("DB_CACHE_MAX_MB", str(limit_bytes / (1024 * 1024)))

    assert admin.main(["--purge-expired", "--apply"]) == 0

    assert not old_csv.exists()
    assert not old_json.exists()
    assert mid_csv.exists()
    assert new_csv.exists()
    remaining = sum(path.stat().st_size for path in cache_root.rglob("*") if path.is_file())
    assert remaining <= limit_bytes


def test_invalid_db_id_rejected(cache_root, capsys):
    assert admin.main(["--clear", "--db-id", "../etc", "--apply"]) == 1
    assert "非法连接 id" in capsys.readouterr().out
