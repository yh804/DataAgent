"""make_demo_db 脚本的端到端测试:行数、完整性、口径参考值与 --force 语义。

所有测试都通过子进程调用脚本,只依赖标准库 sqlite3 与 pandas,不触碰真实
examples/ecommerce.db,库文件写到 pytest 的临时目录。
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "make_demo_db.py"
EXAMPLES_DIR = PROJECT_ROOT / "domains" / "ecommerce" / "examples"

TABLES = ("orders", "order_items", "users", "products", "traffic")

# 来自 domains/ecommerce/check_package.py 的参考值
EXPECTED_DAILY_GMV = 1193284.77
EXPECTED_REPURCHASE_RATE = 0.4583


def _run_script(out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """调用一次 make_demo_db 脚本并返回子进程结果。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), *extra],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _dump_checksum(path: Path) -> str:
    """用 sqlite3 iterdump 计算库内容校验和,验证幂等性。"""
    with closing(sqlite3.connect(path)) as conn:
        payload = "\n".join(conn.iterdump())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Path:
    """在临时目录生成一份演示库,供多个用例复用。"""
    out = tmp_path_factory.mktemp("demo_db") / "ecommerce.db"
    result = _run_script(out)
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def test_table_rows_match_csv(demo_db: Path):
    """五张表的行数必须与 CSV 行数完全一致。"""
    with closing(sqlite3.connect(demo_db)) as conn:
        for table in TABLES:
            csv_rows = len(pd.read_csv(EXAMPLES_DIR / f"{table}.csv", encoding="utf-8-sig"))
            db_rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            assert db_rows == csv_rows, f"{table}: DB {db_rows} 行, CSV {csv_rows} 行"


def test_foreign_keys_and_integrity(demo_db: Path):
    """外键检查为空,integrity_check 为 ok。"""
    with closing(sqlite3.connect(demo_db)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_daily_gmv_total(demo_db: Path):
    """v_daily_gmv 的支付金额总额等于参考值,允许 0.01 误差。"""
    with closing(sqlite3.connect(demo_db)) as conn:
        total = conn.execute('SELECT SUM("支付金额") FROM v_daily_gmv').fetchone()[0]
    assert total == pytest.approx(EXPECTED_DAILY_GMV, abs=0.01)


def test_repurchase_rate(demo_db: Path):
    """v_repurchase 的复购率等于参考值,允许 0.001 误差。"""
    with closing(sqlite3.connect(demo_db)) as conn:
        rate = conn.execute(
            "SELECT 1.0*SUM(CASE WHEN n>=2 THEN 1 ELSE 0 END)/COUNT(*) FROM v_repurchase"
        ).fetchone()[0]
    assert rate == pytest.approx(EXPECTED_REPURCHASE_RATE, abs=0.001)


def test_existing_file_requires_force(tmp_path: Path):
    """目标已存在且未加 --force 时必须非零退出,且原文件保持不变。"""
    out = tmp_path / "ecommerce.db"
    sentinel = b"not a database"
    out.write_bytes(sentinel)

    result = _run_script(out)

    assert result.returncode != 0
    assert out.read_bytes() == sentinel


def test_force_overwrites_existing_file(tmp_path: Path):
    """加 --force 后覆盖已有文件,并生成可用的库。"""
    out = tmp_path / "ecommerce.db"
    out.write_bytes(b"not a database")

    result = _run_script(out, "--force")

    assert result.returncode == 0, result.stdout + result.stderr
    with closing(sqlite3.connect(out)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute('SELECT COUNT(*) FROM "orders"').fetchone()[0] == 2000


def test_rebuild_is_idempotent(tmp_path: Path):
    """同一份 CSV 重建两次,库内容校验和必须一致。"""
    out = tmp_path / "ecommerce.db"

    first = _run_script(out)
    assert first.returncode == 0, first.stdout + first.stderr
    first_checksum = _dump_checksum(out)

    second = _run_script(out, "--force")
    assert second.returncode == 0, second.stdout + second.stderr
    second_checksum = _dump_checksum(out)

    assert first_checksum == second_checksum
