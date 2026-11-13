"""HTML 报告渲染测试:转义、图表内嵌、章节完整性。"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from html_report import render_html_report  # noqa: E402

# 一张 1x1 像素的合法 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

BASE_RUN = {
    "run_id": "run_test",
    "question": "测试问题",
    "dataset": "测试.csv",
    "now": "2026-11-05 10:00:00",
    "fingerprint": "测试.csv@abc123",
    "schema": {"row_count": 100, "encoding": "utf-8"},
    "summary": {
        "headline": "测试结论",
        "findings": ["发现1"],
        "recommendations": ["建议1"],
        "caveats": ["局限1"],
        "method": "按分组求和",
    },
    "warnings": [],
    "stdout": "print output",
    "code": "print(1)",
    "charts": [],
}

CORE_SECTIONS = ["一句话结论", "关键发现", "建议", "局限与数据质量", "口径与数据版本"]


def test_html_escapes_script_tags():
    """summary 里的 <script> 必须被转义,不能成为可执行标签。"""
    run = {
        **BASE_RUN,
        "summary": {"headline": "<script>alert(1)</script>", "findings": []},
        "question": "问题 <img src=x onerror=alert(2)>",
    }
    html_text = render_html_report(run, [], PROJECT_ROOT / "outputs")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "<script>alert(1)</script>" not in html_text
    # 问题里的注入同样被转义
    assert "&lt;img src=x onerror=alert(2)&gt;" in html_text


def test_html_inlines_chart_as_base64(tmp_path: Path):
    """存在的图表内嵌为 data:image/png;base64,;缺失图表跳过并产生 warning。"""
    outputs_dir = tmp_path / "outputs"
    chart_dir = outputs_dir / "runs" / "run_test" / "charts"
    chart_dir.mkdir(parents=True)
    (chart_dir / "ok.png").write_bytes(PNG_BYTES)

    run = {**BASE_RUN, "charts": ["runs/run_test/charts/ok.png", "runs/run_test/charts/missing.png"]}
    html_text = render_html_report(run, run["charts"], outputs_dir)

    assert "data:image/png;base64," in html_text
    assert base64.b64encode(PNG_BYTES).decode("ascii") in html_text
    assert "图表缺失,已跳过: missing.png" in html_text


def test_html_core_sections_present():
    """五个核心章节标题必须全部出现。"""
    html_text = render_html_report(BASE_RUN, [], PROJECT_ROOT / "outputs")
    for section in CORE_SECTIONS:
        assert section in html_text


def test_html_no_external_references():
    """离线可开:不出现 http:// 或 https:// 外部引用。"""
    html_text = render_html_report(BASE_RUN, [], PROJECT_ROOT / "outputs")
    assert "http://" not in html_text
    assert "https://" not in html_text
    assert "@import" not in html_text
