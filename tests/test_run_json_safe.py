"""运行归档 JSON 安全测试:NaN / Infinity 不能出现在 result.json 里。

为什么单独测:
    Python 的 json.dumps 默认允许 NaN,会写出裸 NaN 字面量;
    浏览器 fetch().json() 遇到它会直接抛错,前端表现为"运行归档读取失败"。
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import archive
import main
import web.app as web_app


def test_json_safe_converts_non_finite():
    """NaN / 正负无穷递归替换为 None,普通值原样保留。"""
    payload = {
        "nan": float("nan"),
        "inf": float("inf"),
        "neg": float("-inf"),
        "ok": 1.5,
        "nested": [float("nan"), {"deep": float("inf")}],
        "text": "NaN 只是字符串",
    }
    safe = main.json_safe(payload)
    assert safe["nan"] is None
    assert safe["inf"] is None
    assert safe["neg"] is None
    assert safe["ok"] == 1.5
    assert safe["nested"][0] is None
    assert safe["nested"][1]["deep"] is None
    assert safe["text"] == "NaN 只是字符串"


def test_write_run_archive_has_no_nan(tmp_path, monkeypatch):
    """写归档时必须过滤 NaN,保证 result.json 是合法 JSON。"""
    monkeypatch.setattr(archive, "RUNS_DIR", tmp_path / "runs")
    archive._write_run_archive("rid1", {"mean": float("nan"), "n": 3}, "# 报告", "")
    text = (tmp_path / "runs" / "rid1" / "result.json").read_text(encoding="utf-8")
    assert "NaN" not in text
    assert json.loads(text)["mean"] is None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """离线 TestClient:输出目录指向 tmp_path。"""
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(web_app, "SESSIONS_PATH", tmp_path / "outputs" / "sessions.jsonl")
    (tmp_path / "outputs").mkdir()
    return TestClient(web_app.app)


def test_get_run_sanitizes_legacy_nan(client, tmp_path):
    """历史归档里残留的裸 NaN 读取时要被替换,否则浏览器解析失败。"""
    run_dir = tmp_path / "outputs" / "runs" / "rid_nan"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        '{"result": {"mean": NaN, "n": 3}, "status": "success"}',
        encoding="utf-8",
    )
    resp = client.get("/api/runs/rid_nan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["result"]["mean"] is None
    assert body["result"]["result"]["n"] == 3
