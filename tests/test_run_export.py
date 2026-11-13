"""运行归档导出接口的离线回归测试(不联网、不起 Docker、不调 LLM)。

覆盖:
    1. 归档后 runs/<id>/report.html 存在且 > 1KB;
    2. GET /api/runs/{id}/report.html 返回 200 且 content-type 含 text/html;
    3. GET /api/runs/{id}/result.csv 有文件 200、无文件 404;
    4. 路径穿越 report.html/../x 与 result.csv/../../x 都 404。
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import web.app as web_app

VALID_RUN_ID = "20260101_000000_abcd"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出目录指向 tmp_path。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


def _seed_run(tmp_path, run_id: str = VALID_RUN_ID, with_csv: bool = True) -> None:
    """造一份最小运行归档,包含 report.html 与 result.csv。"""
    run_dir = tmp_path / "outputs" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps({"run_id": run_id, "status": "success", "result_csv": with_csv}),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text("# 报告\n", encoding="utf-8")
    # 超过 1KB 的假 HTML,只验证存在性与大小
    (run_dir / "report.html").write_text("<html>" + "x" * 2048 + "</html>", encoding="utf-8")
    if with_csv:
        (run_dir / "result.csv").write_text("a,b\n1,2\n", encoding="utf-8")


def test_report_html_archived_and_served(client, tmp_path):
    _seed_run(tmp_path)
    run_dir = tmp_path / "outputs" / "runs" / VALID_RUN_ID
    assert (run_dir / "report.html").is_file()
    assert (run_dir / "report.html").stat().st_size > 1024

    resp = client.get(f"/api/runs/{VALID_RUN_ID}/report.html")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_result_csv_present_and_missing(client, tmp_path):
    _seed_run(tmp_path, run_id=VALID_RUN_ID, with_csv=True)
    resp = client.get(f"/api/runs/{VALID_RUN_ID}/result.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")

    # 无 CSV 的旧运行 → 404
    _seed_run(tmp_path, run_id="20260101_000001_efgh", with_csv=False)
    resp = client.get("/api/runs/20260101_000001_efgh/result.csv")
    assert resp.status_code == 404


def test_path_traversal_rejected(client, tmp_path):
    _seed_run(tmp_path)
    for bad_url in [
        f"/api/runs/{VALID_RUN_ID}/report.html/../x",
        f"/api/runs/{VALID_RUN_ID}/result.csv/../../x",
        "/api/runs/..%2F..%2Fetc/report.html",
    ]:
        resp = client.get(bad_url)
        assert resp.status_code == 404, bad_url
