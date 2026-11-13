"""MyISAM -> InnoDB 迁移脚本测试:不连接真实 MySQL。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import migrate_to_innodb as migrate

CONFIG = {
    "id": "2f79f5f5",
    "kind": "mysql",
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "database": "bizdata",
    "password": "secret",
}


class FakeClient:
    """按 SQL 片段返回预置结果的迁移连接替身。"""

    def __init__(
        self,
        *,
        datadir: Path,
        engine: str = "MyISAM",
        rows: int = 494_024,
        data_length: int = 60 * 1024 * 1024,
        index_length: int = 9 * 1024 * 1024,
        numeric=("Age", "AnnualIncome"),
        sums=(26_751_917.12, 34_115_767.45),
        fulltext=(),
        post_engine: str = "InnoDB",
        post_rows: int | None = None,
        post_sums=None,
    ) -> None:
        self.datadir = datadir
        self.engine = engine
        self.rows = rows
        self.data_length = data_length
        self.index_length = index_length
        self.numeric = list(numeric)
        self.sums = list(sums)
        self.fulltext = list(fulltext)
        self.post_engine = post_engine
        self.post_rows = rows if post_rows is None else post_rows
        self.post_sums = list(sums) if post_sums is None else list(post_sums)
        self.executed: list[str] = []
        self.altered = False
        self.timed_runs = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def close(self) -> None:
        pass

    def fetch_one(self, sql: str, params: tuple | None = None):
        if "information_schema.tables" in sql:
            return (
                self.post_engine if self.altered else self.engine,
                self.rows,
                self.data_length,
                self.index_length,
            )
        if "@@datadir" in sql:
            return (str(self.datadir),)
        if "ROUND(SUM(" in sql:
            return tuple(self.post_sums if self.altered else self.sums)
        if sql.strip().upper().startswith("SELECT COUNT(*)"):
            return (self.post_rows if self.altered else self.rows,)
        raise AssertionError(f"未预期的查询: {sql}")

    def fetch_all(self, sql: str, params: tuple | None = None):
        if "information_schema.statistics" in sql:
            return [(name,) for name in self.fulltext]
        if "information_schema.columns" in sql:
            return [(name,) for name in self.numeric]
        if "ROUND(SUM(" in sql:
            self.timed_runs += 1
            return [tuple(self.post_sums if self.altered else self.sums)]
        raise AssertionError(f"未预期的查询: {sql}")

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if sql.upper().startswith("ALTER TABLE"):
            self.altered = True


@pytest.fixture()
def install(monkeypatch, tmp_path):
    """安装假连接与假连接配置,返回可定制 FakeClient 的工厂。"""

    def factory(**overrides) -> FakeClient:
        client = FakeClient(datadir=tmp_path, **overrides)
        monkeypatch.setattr(
            migrate,
            "_open_client",
            lambda config, *, timeout: client,
        )
        monkeypatch.setattr(migrate, "_load_config", lambda _db_id: dict(CONFIG))
        monkeypatch.setattr(migrate, "MIGRATIONS_PATH", tmp_path / "migrations.jsonl")
        return client

    return factory


def _run(argv: list[str]) -> int:
    return migrate.main(argv)


def test_dry_run_prints_rows_engine_and_estimate(install, capsys):
    client = install()

    code = _run(["--db-id", "2f79f5f5", "--table", "customer_purchase"])

    output = capsys.readouterr().out
    assert code == 0
    assert "494024" in output
    assert "MyISAM" in output
    assert "预计耗时" in output
    assert "额外空间需求" in output
    assert "dry-run" in output
    assert client.executed == []


def test_dry_run_reports_fulltext_index(install, capsys):
    install(fulltext=("ft_note",))

    assert _run(["--db-id", "2f79f5f5", "--table", "customer_purchase"]) == 0
    output = capsys.readouterr().out
    assert "FULLTEXT 索引: ft_note" in output
    assert "需重建" in output


def test_already_innodb_exits_zero_without_backup(install, monkeypatch, capsys):
    client = install(engine="InnoDB")
    monkeypatch.setattr(
        migrate,
        "_run_mysqldump",
        lambda *_args, **_kwargs: pytest.fail("已是 InnoDB 时不应备份"),
    )

    code = _run(["--db-id", "2f79f5f5", "--table", "customer_purchase", "--apply"])

    assert code == 0
    assert "无需迁移" in capsys.readouterr().out
    assert client.executed == []


def test_dump_failure_stops_before_alter(install, monkeypatch, tmp_path, capsys):
    client = install()
    dump_exe = tmp_path / "mysqldump.exe"
    dump_exe.write_bytes(b"stub")
    monkeypatch.setattr(migrate, "MYSQLDUMP_PATH", dump_exe)
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=1, stderr=b"dump failed")

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)

    code = _run(
        [
            "--db-id",
            "2f79f5f5",
            "--table",
            "customer_purchase",
            "--apply",
            "--backup-dir",
            str(tmp_path / "backups"),
        ]
    )

    output = capsys.readouterr().out
    assert code == 1
    assert "备份失败" in output
    assert all("ALTER TABLE" not in sql for sql in client.executed)
    assert captured["env"]["MYSQL_PWD"] == "secret"
    assert all("secret" not in str(item) for item in captured["args"])


def test_verification_mismatch_exits_two_with_restore_hint(
    install, monkeypatch, tmp_path, capsys
):
    client = install(post_sums=(26_751_917.99, 34_115_767.45))
    backup = tmp_path / "customer_purchase_20261112_210000.sql"
    backup.write_text("-- dump", encoding="utf-8")
    monkeypatch.setattr(migrate, "_run_mysqldump", lambda *_args, **_kwargs: backup)

    code = _run(["--db-id", "2f79f5f5", "--table", "customer_purchase", "--apply"])

    output = capsys.readouterr().out
    assert code == 2
    assert "校验失败" in output
    assert "请用备份恢复" in output
    assert f"mysql -h 127.0.0.1 -P 3306 -u root -p bizdata < {backup}" in output
    assert any(sql.upper().startswith("ALTER TABLE") for sql in client.executed)


def test_successful_apply_writes_audit(install, monkeypatch, tmp_path, capsys):
    client = install()
    backup = tmp_path / "customer_purchase_20261112_210000.sql"
    backup.write_text("-- dump", encoding="utf-8")
    monkeypatch.setattr(migrate, "_run_mysqldump", lambda *_args, **_kwargs: backup)

    code = _run(["--db-id", "2f79f5f5", "--table", "customer_purchase", "--apply"])

    output = capsys.readouterr().out
    assert code == 0
    assert "迁移完成" in output
    assert "第二次" in output
    assert client.timed_runs == 2
    assert any(sql.startswith("ANALYZE TABLE") for sql in client.executed)
    record = json.loads((tmp_path / "migrations.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["table"] == "bizdata.customer_purchase"
    assert record["from_engine"] == "MyISAM"
    assert record["to_engine"] == "InnoDB"
    assert record["rows"] == 494_024
    assert record["verify_ok"] is True
    assert record["backup"] == str(backup)


def test_backup_filename_uses_table_and_timestamp(tmp_path):
    path = migrate._backup_path(tmp_path, "customer_purchase")

    assert path.parent == tmp_path
    assert path.name.startswith("customer_purchase_")
    assert path.suffix == ".sql"
