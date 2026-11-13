"""指标采集脚本的离线测试:不联网、不起 Docker、不调 LLM。

覆盖:
    1. p50 / 均值 / token 均值跳过 null 的统计口径;
    2. auto 占比、confirm 占比、人工拒绝计数;
    3. 会话数与已删除会话去重计数;
    4. 最新 eval 报告按文件名选取;
    5. 坏行跳过、缺文件时输出"暂无数据"且退出码 0;
    6. README 标记替换只改 start / end 之间的内容,其余文本不动。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# scripts/ 不是包:把目录加入 sys.path 才能直接 import collect_metrics
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import collect_metrics  # noqa: E402


@pytest.fixture()
def metrics_mod(monkeypatch):
    """隔离外部依赖:docker 版本探测打桩,不触发任何 Docker 调用。"""
    monkeypatch.setattr(collect_metrics, "docker_version", lambda: "未检测到(Docker 未安装或不在 PATH)")
    return collect_metrics


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_outputs(tmp_path: Path) -> Path:
    """造一份含 4 条 run / 5 条审批 / 2 个会话 / 1 个删除 / 2 份 eval 的 outputs/。"""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    _write_jsonl(
        outputs / "index.jsonl",
        [
            '{"run_id": "r1", "status": "success", "duration_ms": 100, "token_used": 100}',
            '{"run_id": "r2", "status": "error", "duration_ms": 300, "token_used": null}',
            '{"run_id": "r3", "status": "success", "duration_ms": 200, "token_used": 300}',
            '{"run_id": "r4", "status": "success", "duration_ms": 400, "token_used": 200}',
            "{坏行",  # 必须被跳过,不参与统计
        ],
    )
    _write_jsonl(
        outputs / "approvals.jsonl",
        [
            '{"level": "auto", "action": "auto", "decided_by": "policy"}',
            '{"level": "auto", "action": "auto", "decided_by": "policy"}',
            '{"level": "confirm", "action": "confirm", "decided_by": "policy"}',
            '{"level": "confirm", "action": "reject", "decided_by": "human"}',
            '{"level": "confirm", "action": "approve", "decided_by": "human"}',
        ],
    )
    _write_jsonl(
        outputs / "sessions.jsonl",
        [
            '{"thread_id": "t1"}',
            '{"thread_id": "t1"}',
            '{"thread_id": "t2"}',
        ],
    )
    _write_jsonl(outputs / "deletions.jsonl", ['{"thread_id": "t2"}'])

    # 文件名带时间戳:较新的那份必须被选中
    # eval/ 子目录放两份历史报告;latest_eval_report 从 outputs_dir / "eval" 读
    eval_dir = outputs / "eval"
    eval_dir.mkdir()
    (eval_dir / "eval_report_20260101_000000.json").write_text(
        json.dumps({"timestamp": "2026-01-01T00:00:00", "total": 5, "passed": 3, "failed": 2}),
        encoding="utf-8",
    )
    (eval_dir / "eval_report_20261101_120000.json").write_text(
        json.dumps({"timestamp": "2026-11-01T12:00:00", "total": 8, "passed": 8, "failed": 0}),
        encoding="utf-8",
    )
    return outputs


def test_statistics_are_exact(metrics_mod, tmp_path):
    """4 条 run / 5 条审批的统计口径必须与手算一致。"""
    metrics = metrics_mod.compute_metrics(_build_outputs(tmp_path), tests_collected=110)

    assert metrics["tests"]["collected"] == 110
    assert metrics["runs"]["total"] == 4
    assert metrics["runs"]["success"] == 3
    assert metrics["runs"]["success_rate"] == pytest.approx(0.75)
    # duration [100, 300, 200, 400] -> 排序后中间两个平均 = 250
    assert metrics["runs"]["duration_p50_ms"] == pytest.approx(250)
    assert metrics["runs"]["duration_mean_ms"] == pytest.approx(250)
    assert metrics["runs"]["duration_samples"] == 4
    # token [100, 300, 200]:null 被跳过,均值 200
    assert metrics["runs"]["token_mean"] == pytest.approx(200)
    assert metrics["runs"]["token_samples"] == 3

    assert metrics["approvals"]["total"] == 5
    assert metrics["approvals"]["auto"] == 2
    assert metrics["approvals"]["auto_ratio"] == pytest.approx(0.4)
    assert metrics["approvals"]["confirm"] == 3
    assert metrics["approvals"]["confirm_ratio"] == pytest.approx(0.6)
    assert metrics["approvals"]["human_reject"] == 1

    assert metrics["sessions"]["total"] == 2  # t1 两条记录去重
    assert metrics["sessions"]["deleted"] == 1

    assert metrics["eval"]["total"] == 8
    assert metrics["eval"]["passed"] == 8
    assert metrics["eval"]["failed"] == 0
    assert metrics["eval"]["file"] == "eval_report_20261101_120000.json"


def test_percentile_edge_cases(metrics_mod):
    """奇数样本取正中,偶数样本取中间平均,空样本返回 None。"""
    assert metrics_mod.p50([100, 200, 300]) == pytest.approx(200)
    assert metrics_mod.p50([100, 200]) == pytest.approx(150)
    assert metrics_mod.p50([]) is None
    assert metrics_mod.mean([]) is None


def test_bad_lines_are_skipped(metrics_mod, tmp_path):
    """坏 JSON 行不进入统计,也不会让脚本报错。"""
    outputs = _build_outputs(tmp_path)
    metrics = metrics_mod.compute_metrics(outputs)
    # 5 行里 1 行是坏行 -> 只统计 4 条
    assert metrics["runs"]["total"] == 4
    assert metrics["runs"]["duration_samples"] == 4


def test_missing_files_render_no_data_and_exit_zero(metrics_mod, tmp_path, monkeypatch):
    """新 clone 没有 outputs/:指标为 None,文档写"暂无数据",退出码 0。"""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    docs = tmp_path / "docs"
    monkeypatch.setattr(metrics_mod, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(metrics_mod, "DOCS_DIR", docs)
    monkeypatch.setattr(metrics_mod, "README_PATH", tmp_path / "README.md")

    metrics = metrics_mod.compute_metrics(outputs)
    assert metrics["runs"]["total"] == 0
    assert metrics["runs"]["success_rate"] is None
    assert metrics["runs"]["duration_p50_ms"] is None
    assert metrics["approvals"]["auto_ratio"] is None
    assert metrics["sessions"]["total"] == 0
    assert metrics["sessions"]["deleted"] == 0
    assert metrics["eval"] is None

    markdown = metrics_mod.render_metrics_md(metrics)
    assert "暂无数据" in markdown

    assert metrics_mod.main(["--no-readme"]) == 0
    assert (docs / "metrics.json").is_file()
    assert (docs / "metrics.md").is_file()
    assert "暂无数据" in (docs / "metrics.md").read_text(encoding="utf-8")


def test_readme_marker_replacement_only_touches_middle(metrics_mod, tmp_path):
    """README 替换只改标记之间,前缀与后缀逐字保留。"""
    readme = tmp_path / "README.md"
    prefix = "# 标题\n\n这段前缀必须保持不变。\n\n"
    suffix = "\n\n这段后缀也必须保持不变。\n"
    readme.write_text(
        prefix
        + "<!-- metrics:start -->\n旧指标\n<!-- metrics:end -->"
        + suffix
        + "<!-- hero:start -->\n旧数字\n<!-- hero:end -->\n",
        encoding="utf-8",
    )

    metrics = metrics_mod.compute_metrics(_build_outputs(tmp_path), tests_collected=110)
    new_text, ok = metrics_mod.replace_marked(
        readme.read_text(encoding="utf-8"),
        metrics_mod.METRICS_START,
        metrics_mod.METRICS_END,
        "新指标",
    )
    assert ok is True
    assert "新指标" in new_text and "旧指标" not in new_text
    assert new_text.startswith(prefix + "<!-- metrics:start -->\n新指标")
    assert new_text.endswith(suffix + "<!-- hero:start -->\n旧数字\n<!-- hero:end -->\n")

    # update_readme 同时替换两块,外层文本仍逐字保留
    metrics_ok, hero_ok = metrics_mod.update_readme(readme, metrics)
    assert metrics_ok and hero_ok
    updated = readme.read_text(encoding="utf-8")
    assert updated.startswith(prefix)
    assert suffix in updated
    assert "旧指标" not in updated and "旧数字" not in updated
    assert "8/8" in updated


def test_marks_missing_do_not_rewrite_readme(metrics_mod, tmp_path):
    """没有标记时不改写 README(避免把整篇替换掉)。"""
    readme = tmp_path / "README.md"
    original = "# 标题\n\n没有 metrics 标记。\n"
    readme.write_text(original, encoding="utf-8")
    metrics = metrics_mod.compute_metrics(_build_outputs(tmp_path))
    metrics_ok, hero_ok = metrics_mod.update_readme(readme, metrics)
    assert metrics_ok is False and hero_ok is False
    assert readme.read_text(encoding="utf-8") == original


def test_hero_contains_four_numbers(metrics_mod, tmp_path):
    """顶部摘要必须出现测试数 / eval / 耗时 / 入口数四个数字。"""
    metrics = metrics_mod.compute_metrics(_build_outputs(tmp_path), tests_collected=110)
    hero = metrics_mod.render_hero(metrics)
    assert "110" in hero
    assert "8/8" in hero
    assert "0.2 s" in hero  # p50=250ms
    assert f"{metrics['entries']}" in hero
