"""describe_dataset 表头探测的确定性测试。

覆盖四种输入:
1. 无表头 CSV(首行即数据):has_header=False,列名按位置生成 col_1..col_n;
2. 正常 CSV:has_header=True,列名与真实表头一致(行为与改动前一致);
3. 表头被首行数据顶掉的「污染」文件:判定为 headerless 并给出 header_note;
4. 重复列名:quality 里出现警告。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools import describe_dataset  # noqa: E402


def _describe(tmp_path: Path, monkeypatch, filename: str, content: str) -> dict:
    """把内容写入临时数据目录,再走 describe_dataset 的真实读取路径。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: 用于覆盖 SANDBOX_DATA_DIR,避免读到项目里的真实数据。
        filename: 文件名(必须以 .csv 结尾)。
        content: 文件正文(UTF-8)。

    Returns:
        describe_dataset 的返回字典。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / filename).write_text(content, encoding="utf-8")
    monkeypatch.setenv("SANDBOX_DATA_DIR", str(data_dir))
    return describe_dataset.invoke({"name": filename})


def test_headerless_csv_uses_positional_columns(tmp_path: Path, monkeypatch):
    info = _describe(
        tmp_path,
        monkeypatch,
        "nohdr.csv",
        "2026-01-01,西南,线下门店,361.57\n"
        "2026-01-02,华中,线上商城,420.09\n"
        "2026-01-03,华南,小程序,381.26\n",
    )

    assert info["has_header"] is False
    assert [column["name"] for column in info["columns"]] == [
        "col_1",
        "col_2",
        "col_3",
        "col_4",
    ]
    # 列名与样本必须来自同一种读法:样本的 key 也是 col_1..col_n
    assert set(info["sample"][0]) == {"col_1", "col_2", "col_3", "col_4"}
    assert "无表头" in info["header_note"]
    assert "header=None" in info["header_note"]
    # 行数按物理数据行统计,首行没有被当成表头吞掉
    assert info["row_count"] == 3


def test_normal_csv_keeps_real_header(tmp_path: Path, monkeypatch):
    info = _describe(
        tmp_path,
        monkeypatch,
        "normal.csv",
        "订单日期,地区,渠道,订单金额\n"
        "2026-01-01,西南,线下门店,361.57\n"
        "2026-01-02,华中,线上商城,420.09\n",
    )

    assert info["has_header"] is True
    assert [column["name"] for column in info["columns"]] == [
        "订单日期",
        "地区",
        "渠道",
        "订单金额",
    ]
    assert info["header_note"] == ""
    assert info["row_count"] == 2


def test_polluted_header_equal_to_first_data_row_is_headerless(tmp_path: Path, monkeypatch):
    # 表头行与紧随其后的数据行完全相同:说明表头已被数据行顶掉
    info = _describe(
        tmp_path,
        monkeypatch,
        "polluted.csv",
        "订单日期,地区,渠道,订单金额\n"
        "订单日期,地区,渠道,订单金额\n"
        "2026-01-01,西南,线下门店,361.57\n",
    )

    assert info["has_header"] is False
    assert [column["name"] for column in info["columns"]] == [
        "col_1",
        "col_2",
        "col_3",
        "col_4",
    ]
    assert "无表头" in info["header_note"]


def test_duplicate_columns_warn_in_quality(tmp_path: Path, monkeypatch):
    info = _describe(
        tmp_path,
        monkeypatch,
        "duplicate.csv",
        "金额,金额,地区\n"
        "1,2,华北\n"
        "3,4,华南\n",
    )

    # 重复列名仍是正常表头,但 quality 必须提示 pandas 的 a / a.1 改写
    assert info["has_header"] is True
    assert "列名重复" in info["quality"]
