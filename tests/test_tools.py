"""tools 的编码探测 / 数据发现安全边界 / 指纹稳定性测试(离线)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools import _detect_encoding, describe_dataset, fingerprint_text  # noqa: E402


def test_detect_encoding_utf8_sig(tmp_path: Path):
    """带 BOM 的 UTF-8 必须识别为 utf-8-sig,否则首列名会残留 \\ufeff。"""
    path = tmp_path / "bom.csv"
    path.write_text("名称,数值\n甲,1\n", encoding="utf-8-sig")
    assert _detect_encoding(path) == "utf-8-sig"


def test_detect_encoding_gb18030(tmp_path: Path):
    """GB18030 编码的中文 CSV 不能被误判为 UTF-8。"""
    path = tmp_path / "gbk.csv"
    path.write_text("名称,数值\n测试,1\n", encoding="gb18030")
    assert _detect_encoding(path) == "gb18030"


def test_detect_encoding_plain_utf8(tmp_path: Path):
    """纯 ASCII / 无 BOM 的 UTF-8 至少能被某个候选编码成功解析。"""
    path = tmp_path / "plain.csv"
    path.write_text("name,value\nabc,1\n", encoding="utf-8")
    assert _detect_encoding(path) in ("utf-8-sig", "utf-8")
    path.read_text(encoding=_detect_encoding(path))


def test_describe_dataset_rejects_path_traversal(tmp_path: Path, monkeypatch):
    """越出数据目录的相对路径必须拒绝(先发现,后读取)。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "ok.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    # 目标文件真实存在于数据目录之外
    (tmp_path / "outside.csv").write_text("a\n1\n", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(data_dir))

    with pytest.raises(FileNotFoundError):
        describe_dataset.invoke({"name": "../outside.csv"})


def test_describe_dataset_rejects_non_csv(tmp_path: Path, monkeypatch):
    """非 .csv 后缀明确报错,而不是尝试解析。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "notes.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(data_dir))

    with pytest.raises(ValueError, match="csv"):
        describe_dataset.invoke({"name": "notes.txt"})


def test_describe_dataset_empty_file_returns_empty_structure(tmp_path: Path, monkeypatch):
    """空文件不算异常:返回 0 行 / 空列,并带上数据版本元信息。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "empty.csv").write_text("", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(data_dir))

    info = describe_dataset.invoke({"name": "empty.csv"})
    assert info["row_count"] == 0
    assert info["columns"] == []
    assert info["sample"] == []
    assert len(info["sha256"]) == 64


def test_fingerprint_text_is_stable_and_short():
    """指纹稳定可复现,sha 片段固定 12 位。"""
    schema = {"name": "测试数据.csv", "sha256": "a1b2c3d4e5f6" + "0" * 52}
    first = fingerprint_text(schema)
    second = fingerprint_text(dict(schema))
    assert first == second == "测试数据.csv@a1b2c3d4e5f6"

    sha_part = first.split("@", 1)[1]
    assert len(sha_part) == 12


def test_fingerprint_text_without_meta_is_empty():
    """元信息缺失时返回空串,不拼出 "@" 这类脏值。"""
    assert fingerprint_text({}) == ""
