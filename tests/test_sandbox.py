"""sandbox 的纯函数测试:结果构造 / 产物回收 / 未知后端降级(不启 Docker)。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import sandbox  # noqa: E402
from sandbox import _collect_outputs, get_sandbox, make_result  # noqa: E402


def test_make_result_truncates_stdout_tail():
    """stdout 只保留尾部 STDOUT_LIMIT 字,避免大输出撑爆上下文。"""
    long_stdout = "a" * 3000
    result = make_result(stdout=long_stdout)

    assert len(result["stdout"]) == sandbox.STDOUT_LIMIT
    # 截断保留尾部:结尾字符必须仍在,开头字符已被丢弃
    assert result["stdout"].endswith("a")
    assert result["stdout"] == long_stdout[-sandbox.STDOUT_LIMIT:]
    assert result["exit_status"] == "error"
    assert result["charts"] == []
    assert result["files"] == []


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def test_collect_outputs_includes_new_file(tmp_path: Path):
    """baseline 里没有的文件 = 本次新增,要回收。"""
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    (out_dir / "new.png").write_bytes(b"png")

    files, charts = _collect_outputs(out_dir, baseline={})
    assert files == ["new.png"]
    assert charts == ["new.png"]


def test_collect_outputs_includes_overwritten_file(tmp_path: Path):
    """同名覆盖(mtime/size 变化)也算本次产物,不能漏掉重绘的图。"""
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    target = out_dir / "same.png"
    target.write_bytes(b"v2")
    # baseline 记录的是执行前的旧签名
    baseline = {"same.png": (0, 1)}

    files, charts = _collect_outputs(out_dir, baseline=baseline)
    assert files == ["same.png"]
    assert charts == ["same.png"]


def test_collect_outputs_skips_unchanged_file(tmp_path: Path):
    """执行前后签名一致 = 历史文件,不回收。"""
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    target = out_dir / "old.png"
    target.write_bytes(b"old")
    baseline = {"old.png": _signature(target)}

    files, charts = _collect_outputs(out_dir, baseline=baseline)
    assert files == []
    assert charts == []


def test_collect_outputs_missing_dir_is_empty(tmp_path: Path):
    """产物目录不存在时返回空清单,不抛异常。"""
    files, charts = _collect_outputs(tmp_path / "no_such_dir", baseline={})
    assert files == []
    assert charts == []


def test_unknown_backend_returns_config_error(monkeypatch):
    """SANDBOX_BACKEND 配置错误时返回结构化 ConfigError,而不是崩溃。"""
    monkeypatch.setattr(sandbox, "SANDBOX_BACKEND", "unknown-backend")

    result = get_sandbox().execute("print(1)")
    assert result["exit_status"] == "error"
    assert "ConfigError" in result["stderr"]
    assert "unknown-backend" in result["stderr"]
