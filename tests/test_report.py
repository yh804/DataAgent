"""报告渲染与图表归档测试(tmp_path,不触碰真实 outputs)。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import archive  # noqa: E402
import main  # noqa: E402
from reporting import render_report  # noqa: E402

# 五个核心章节的顺序固定,eval 与人工都按标题定位
CORE_SECTIONS = (
    "## 一句话结论",
    "## 关键发现",
    "## 建议",
    "## 局限与数据质量",
    "## 口径与数据版本",
)


def test_render_report_section_order():
    run = {
        "run_id": "20261031_120000_abc123",
        "question": "测试组和对照组收入差异",
        "dataset": "测试数据.csv",
        "summary": {
            "headline": "测试组收入均值 70.1,高于对照组 66.5",
            "findings": ["两组均值差 3.6"],
            "recommendations": ["按分组复核一次"],
            "caveats": ["仅为描述统计"],
            "method": "按分组求均值",
        },
        "warnings": [],
        "charts": [],
        "code": "import pandas as pd",
        "duration_ms": 1000,
        "iteration": 1,
        "fingerprint": "测试数据.csv@a1b2c3d4e5f6",
        "schema": {"row_count": 800, "encoding": "utf-8-sig"},
        "now": "2026-10-31 12:00:00",
    }
    text = render_report(run)

    assert text.startswith("# 分析报告")
    positions = [text.index(section) for section in CORE_SECTIONS]
    assert positions == sorted(positions), f"章节顺序错乱: {positions}"

    # 固定章节之后的必备章节也要在
    for section in ("## 图表", "## 生成代码", "## 耗时与尝试次数"):
        assert section in text

    # 关键内容与可复现信息都在
    assert "测试组收入均值 70.1" in text
    assert "测试数据.csv@a1b2c3d4e5f6" in text
    assert "行数：800" in text
    assert "编码：utf-8-sig" in text


def test_render_report_placeholders_when_empty():
    """内容为空时章节仍存在,用占位文本兜底(禁止静默失败)。"""
    run = {"summary": None, "warnings": [], "charts": [], "schema": {}}
    text = render_report(run)

    for section in CORE_SECTIONS:
        assert section in text
    assert "(无结论)" in text
    assert "(无)" in text        # 关键发现 / 建议 / 图表的占位
    assert "(未提供)" in text    # 局限与数据质量的占位
    # 显式 SQL 章节只在数据库入口传入时才出现
    assert "## 执行的 SQL" not in text


def test_render_report_includes_warnings_and_charts():
    run = {
        "summary": {"headline": "结论", "findings": [], "caveats": []},
        "warnings": ["汇总疑似自造数字: 12.5"],
        "charts": ["runs/rid/charts/a.png"],
        "schema": {},
    }
    text = render_report(run)

    assert "## 数据质量警告" in text
    assert "汇总疑似自造数字: 12.5" in text
    # 图表路径统一加 outputs/ 前缀,不出现 outputs/outputs/
    assert "outputs/runs/rid/charts/a.png" in text
    assert "outputs/outputs/" not in text


def test_archive_charts_moves_files_and_returns_new_path(tmp_path: Path, monkeypatch):
    """图表必须被「移动」到 runs/<id>/charts/,返回值是可写入报告的新路径。"""
    # 把模块级输出目录重定向到 tmp_path,避免污染真实 outputs/
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    runs_dir = output_dir / "runs"
    monkeypatch.setattr(archive, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(archive, "RUNS_DIR", runs_dir)

    source = output_dir / "chart_demo.png"
    source.write_bytes(b"png-bytes")

    moved, warnings = archive._archive_charts("run_abc", ["chart_demo.png"])

    assert warnings == []
    assert moved == ["runs/run_abc/charts/chart_demo.png"]
    # 源文件已移动(不再存在),目标文件存在
    assert not source.exists()
    assert (runs_dir / "run_abc" / "charts" / "chart_demo.png").is_file()


def test_archive_charts_missing_source_warns(tmp_path: Path, monkeypatch):
    """源图表缺失时记警告并跳过,不阻断归档。"""
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    monkeypatch.setattr(archive, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(archive, "RUNS_DIR", output_dir / "runs")

    moved, warnings = archive._archive_charts("run_abc", ["not_exist.png"])

    assert moved == []
    assert len(warnings) == 1
    assert "not_exist.png" in warnings[0]
