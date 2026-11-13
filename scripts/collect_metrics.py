"""采集项目可验证指标,生成 docs/metrics.json / docs/metrics.md 并更新 README 标记块。

设计目标:
    1. 所有数字都来自真实产物(outputs/*.jsonl、最新 eval 报告、pytest 收集结果),
       不调 LLM、不起容器、不访问网络;
    2. 缺文件时不报错:对应指标记为 None,文档渲染为"暂无数据",退出码 0;
    3. README 只替换 <!-- metrics:start --> 与 <!-- metrics:end --> 之间的内容
       (以及顶部 <!-- hero:start --> / <!-- hero:end --> 的四数字摘要),
       其余文本一字不动。

用法:
    python scripts/collect_metrics.py                # 采集产物指标
    python scripts/collect_metrics.py --with-tests   # 额外跑 pytest --collect-only -q
    python scripts/collect_metrics.py --no-readme    # 只写 docs/,不改 README

指标口径(写进 docs/metrics.md 的"测量口径"一节):
    - 运行总数 / 成功率:outputs/index.jsonl 的 status 字段;
    - 沙箱耗时:p50 / 均值取 duration_ms(执行结果里的沙箱耗时,非端到端墙钟);
    - token 均值:token_used 跳过 null 后求均值;
    - auto 占比:approvals.jsonl 中 level=="auto" 的决策占比;
    - confirm 占比:approvals.jsonl 中 level=="confirm" 的决策占比;
    - 人工拒绝:decided_by=="human" 且 action=="reject";
    - 会话数:sessions.jsonl 去重 thread_id;已删除:deletions.jsonl 去重 thread_id;
    - eval:按文件名取最新的 outputs/eval/eval_report_*.json。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
DOCS_DIR = PROJECT_ROOT / "docs"
README_PATH = PROJECT_ROOT / "README.md"

METRICS_START = "<!-- metrics:start -->"
METRICS_END = "<!-- metrics:end -->"
HERO_START = "<!-- hero:start -->"
HERO_END = "<!-- hero:end -->"

# 五个真实入口:chat / analyze / ask_stats / ask_db / web
ENTRY_FILES = ("chat.py", "analyze.py", "ask_stats.py", "ask_db.py", "web/run.py")


# ---------- 基础读取与统计 ----------

def read_jsonl(path: Path) -> list[dict]:
    """读 jsonl 全部记录;文件不存在或坏行直接跳过,绝不抛异常。"""
    if not path.is_file():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    except OSError:
        return []
    return records


def _numbers(records: list[dict], key: str) -> list[float]:
    """取某字段的数值序列;None / 非数字 / bool 一律跳过。"""
    values: list[float] = []
    for record in records:
        raw = record.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return values


def mean(values: list[float]) -> float | None:
    """算术均值;空序列返回 None。"""
    return sum(values) / len(values) if values else None


def p50(values: list[float]) -> float | None:
    """中位数(线性插值):偶数个样本取中间两个的平均。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.5
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return (float(ordered[low]) + float(ordered[high])) / 2


def latest_eval_report(outputs_dir: Path) -> dict | None:
    """取最新的 eval/eval_report_*.json;文件名带时间戳,按名字排序即可。

    Args:
        outputs_dir: 项目 outputs 根目录,函数内部拼 eval/ 子目录,
            调用方不改签名(仍传 OUTPUTS_DIR)。
    """
    eval_dir = outputs_dir / "eval"
    reports = sorted(eval_dir.glob("eval_report_*.json"))
    if not reports:
        return None
    for path in reversed(reports):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("total"):
            data["_file"] = path.name
            return data
    return None


def collect_test_count() -> int | None:
    """跑 pytest --collect-only -q,从输出里解析用例数;失败返回 None。

    注意:这里只收集不执行,不触发 Docker / LLM / 网络。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    matches = re.findall(r"(\d+)\s+tests?\s+collected", output, flags=re.IGNORECASE)
    if matches:
        return int(matches[-1])
    return None


def docker_version() -> str:
    """只读 docker client 版本,不连守护进程、不起容器。

    先试 PATH 里的 docker;Windows 上再探测 Docker Desktop 的两个常见安装目录,
    避免"已安装但当前 shell 的 PATH 没带上"被误报成未安装。
    """
    commands = ["docker"]
    local_appdata = os.getenv("LOCALAPPDATA") or ""
    program_files = os.getenv("ProgramFiles") or ""
    for base in (
        os.path.join(program_files, "Docker", "Docker", "resources", "bin") if program_files else "",
        os.path.join(local_appdata, "Programs", "DockerDesktop", "resources", "bin") if local_appdata else "",
    ):
        if base and Path(base, "docker.exe").is_file():
            commands.append(str(Path(base, "docker.exe")))

    for command in commands:
        try:
            result = subprocess.run(
                [command, "--version"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = (result.stdout or "").strip()
        if text and result.returncode == 0:
            return text.replace("Docker version ", "").split(",")[0].strip()
    return "未检测到(Docker 未安装或不在 PATH)"


# ---------- 指标计算 ----------

def compute_metrics(outputs_dir: Path, tests_collected: int | None = None) -> dict:
    """把 outputs/ 与可选测试数汇总成一个可直接序列化的字典。"""
    runs = read_jsonl(outputs_dir / "index.jsonl")
    approvals = read_jsonl(outputs_dir / "approvals.jsonl")
    sessions = read_jsonl(outputs_dir / "sessions.jsonl")
    deletions = read_jsonl(outputs_dir / "deletions.jsonl")

    total_runs = len(runs)
    success_runs = sum(1 for r in runs if str(r.get("status") or "") == "success")

    durations = _numbers(runs, "duration_ms")
    tokens = _numbers(runs, "token_used")

    total_approvals = len(approvals)
    # auto / confirm 都按策略判定结果(level)统计,两者之和等于总决策数
    auto_count = sum(1 for r in approvals if str(r.get("level") or "") == "auto")
    confirm_count = sum(1 for r in approvals if str(r.get("level") or "") == "confirm")
    human_reject = sum(
        1
        for r in approvals
        if str(r.get("decided_by") or "") == "human" and str(r.get("action") or "") == "reject"
    )

    session_ids = {str(r.get("thread_id")) for r in sessions if r.get("thread_id")}
    deleted_ids = {str(r.get("thread_id")) for r in deletions if r.get("thread_id")}

    eval_report = latest_eval_report(outputs_dir)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tests": {"collected": tests_collected},
        "eval": (
            {
                "total": int(eval_report["total"]),
                "passed": int(eval_report.get("passed") or 0),
                "failed": int(eval_report.get("failed") or 0),
                "timestamp": str(eval_report.get("timestamp") or ""),
                "file": str(eval_report.get("_file") or ""),
            }
            if eval_report
            else None
        ),
        "runs": {
            "total": total_runs,
            "success": success_runs,
            "success_rate": (success_runs / total_runs) if total_runs else None,
            "duration_p50_ms": p50(durations),
            "duration_mean_ms": mean(durations),
            "duration_samples": len(durations),
            "token_mean": mean(tokens),
            "token_samples": len(tokens),
        },
        "approvals": {
            "total": total_approvals,
            "auto": auto_count,
            "auto_ratio": (auto_count / total_approvals) if total_approvals else None,
            "confirm": confirm_count,
            "confirm_ratio": (confirm_count / total_approvals) if total_approvals else None,
            "human_reject": human_reject,
        },
        "sessions": {"total": len(session_ids), "deleted": len(deleted_ids)},
        "entries": sum(1 for name in ENTRY_FILES if (PROJECT_ROOT / name).is_file()),
        "params": {
            "machine": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "python": platform.python_version(),
            "docker": docker_version(),
            "model": os.getenv("LLM_MODEL") or "未配置",
            "samples": {
                "runs": total_runs,
                "approvals": total_approvals,
                "sessions": len(session_ids),
                "eval": int(eval_report["total"]) if eval_report else 0,
                "tests": tests_collected or 0,
            },
        },
    }


# ---------- 渲染 ----------

def _fmt_int(value: int | None) -> str:
    return f"{value:,}" if value is not None else "暂无数据"


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "暂无数据"


def _fmt_seconds(ms: float | None) -> str:
    return f"{ms / 1000:.1f} s" if ms is not None else "暂无数据"


def _fmt_number(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "暂无数据"
    if digits == 0:
        return f"{value:,.0f}"
    return f"{value:,.{digits}f}"


def render_hero(metrics: dict) -> str:
    """README 顶部一行四数字摘要,所有值来自同一份 metrics。"""
    tests = metrics["tests"]["collected"]
    tests_text = f"{tests}" if tests else "暂无数据"
    if metrics["eval"]:
        eval_text = f"{metrics['eval']['passed']}/{metrics['eval']['total']} ({_fmt_pct(metrics['eval']['passed'] / max(metrics['eval']['total'], 1))})"
    else:
        eval_text = "暂无数据"
    duration_text = _fmt_seconds(metrics["runs"]["duration_p50_ms"])
    entries_text = f"{metrics['entries']}"
    return (
        f"**{tests_text}** 个离线单元测试 · **{eval_text}** 回归用例通过 · "
        f"单轮沙箱耗时中位 **{duration_text}** · **{entries_text}** 个入口"
    )


def render_metrics_table(metrics: dict) -> list[str]:
    """完整指标表(README 块与 docs/metrics.md 共用同一份渲染逻辑)。"""
    tests = metrics["tests"]["collected"]
    eval_info = metrics["eval"]
    runs = metrics["runs"]
    approvals = metrics["approvals"]
    sessions = metrics["sessions"]

    if eval_info:
        eval_text = (
            f"{eval_info['passed']}/{eval_info['total']} "
            f"({_fmt_pct(eval_info['passed'] / max(eval_info['total'], 1))})"
        )
        eval_note = f"最新 {eval_info['file']}({eval_info['timestamp']})"
    else:
        eval_text = "暂无数据"
        eval_note = "尚无 outputs/eval/eval_report_*.json"

    if approvals["total"]:
        approval_text = (
            f"{approvals['total']} 次:auto {approvals['auto']} ({_fmt_pct(approvals['auto_ratio'])})"
            f" / confirm {approvals['confirm']} ({_fmt_pct(approvals['confirm_ratio'])})"
        )
    else:
        approval_text = "暂无数据"

    duration_text = (
        f"p50 {_fmt_seconds(runs['duration_p50_ms'])} / 均值 {_fmt_seconds(runs['duration_mean_ms'])}"
        if runs["duration_samples"]
        else "暂无数据"
    )

    return [
        "| 指标 | 数值 | 口径 |",
        "|---|---|---|",
        f"| 单元测试 | {_fmt_int(tests)} | `pytest --collect-only -q`(--with-tests) |",
        f"| 回归评估 | {eval_text} | {eval_note} |",
        f"| 运行总数 / 成功率 | {_fmt_int(runs['total'])} / {_fmt_pct(runs['success_rate'])} | `outputs/index.jsonl` 的 status(只记录完成归档的运行) |",
        f"| 单轮沙箱耗时 | {duration_text} | index.jsonl 的 duration_ms(非空样本 {runs['duration_samples']} 条) |",
        f"| token 均值 | {_fmt_number(runs['token_mean'])} | index.jsonl 的 token_used(跳过 null,样本 {runs['token_samples']} 条) |",
        f"| 审批决策 | {approval_text} | `outputs/approvals.jsonl` 的 level(auto / confirm)占比 |",
        f"| 人工拒绝 | {_fmt_int(approvals['human_reject'])} | decided_by=human 且 action=reject |",
        f"| 会话数 / 已删除 | {_fmt_int(sessions['total'])} / {_fmt_int(sessions['deleted'])} | sessions.jsonl / deletions.jsonl 去重 thread_id |",
    ]


def render_params(metrics: dict) -> list[str]:
    """测量口径:机器、依赖版本、模型、样本量。"""
    params = metrics["params"]
    samples = params["samples"]
    return [
        f"- 机器: {params['machine']};Python {params['python']}",
        f"- Docker: {params['docker']}",
        f"- 模型: {params['model']}(通过 OpenAI 兼容接口调用)",
        (
            f"- 样本量: runs {samples['runs']} 条 / approvals {samples['approvals']} 条"
            f" / sessions {samples['sessions']} 条 / eval {samples['eval']} 条"
            f" / tests {samples['tests']} 条"
        ),
        "- 生成方式: `python scripts/collect_metrics.py --with-tests`(不调 LLM、不起容器、不联网)",
    ]


def render_metrics_block(metrics: dict) -> str:
    """README 的 metrics 标记块内容(不含标记本身)。"""
    lines = render_metrics_table(metrics)
    lines.append("")
    lines.append("**测量口径**")
    lines.append("")
    lines.extend(render_params(metrics))
    lines.append("")
    lines.append("完整指标与说明见 [docs/metrics.md](docs/metrics.md)。")
    return "\n".join(lines)


def render_metrics_md(metrics: dict) -> str:
    """docs/metrics.md 全文。"""
    lines = [
        "# 指标快照",
        "",
        "> 本文件由 `scripts/collect_metrics.py` 生成,请勿手改;数字全部来自真实产物。",
        f"> 生成时间: {metrics['generated_at']}",
        "",
        "## 指标",
        "",
        *render_metrics_table(metrics),
        "",
        "## 测量口径",
        "",
        *render_params(metrics),
        "",
        "## 复现命令",
        "",
        "```bash",
        "python -m pytest -q                     # 离线单元测试",
        "python eval/run_eval.py                 # 端到端回归(需 Docker + LLM)",
        "python scripts/smoke.py                 # 冒烟自检(无需 Docker / LLM)",
        "python scripts/collect_metrics.py --with-tests",
        "```",
        "",
    ]
    return "\n".join(lines)


# ---------- README 标记替换 ----------

def replace_marked(text: str, start: str, end: str, content: str) -> tuple[str, bool]:
    """只替换 start 与 end 之间的内容(保留标记本身);标记缺失时原样返回。

    返回 (新文本, 是否替换成功)。这段逻辑是"其余 README 不动"的保证。
    """
    start_at = text.find(start)
    end_at = text.find(end)
    if start_at == -1 or end_at == -1 or end_at <= start_at:
        return text, False
    head = text[: start_at + len(start)]
    tail = text[end_at:]
    return f"{head}\n{content.rstrip()}\n{tail}", True


def update_readme(path: Path, metrics: dict) -> tuple[bool, bool]:
    """更新 README 的 metrics 块与 hero 块;返回 (metrics 是否更新, hero 是否更新)。"""
    if not path.is_file():
        return False, False
    text = path.read_text(encoding="utf-8")
    text, metrics_ok = replace_marked(text, METRICS_START, METRICS_END, render_metrics_block(metrics))
    text, hero_ok = replace_marked(text, HERO_START, HERO_END, render_hero(metrics))
    if metrics_ok or hero_ok:
        path.write_text(text, encoding="utf-8")
    return metrics_ok, hero_ok


def _display_path(path: Path) -> str:
    """优先显示相对项目根的路径;路径在项目外(测试临时目录)时原样显示。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集项目指标并更新文档")
    parser.add_argument(
        "--with-tests",
        action="store_true",
        help="额外跑 pytest --collect-only -q 统计用例数(只收集不执行)",
    )
    parser.add_argument("--no-readme", action="store_true", help="只写 docs/,不改 README")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:  # noqa: BLE001 - 缺 dotenv 时用进程环境变量即可
        pass

    tests_collected = collect_test_count() if args.with_tests else None
    metrics = compute_metrics(OUTPUTS_DIR, tests_collected)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DOCS_DIR / "metrics.json"
    md_path = DOCS_DIR / "metrics.md"
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_metrics_md(metrics), encoding="utf-8")
    print(f"[metrics] 已写入 {_display_path(json_path)} / {_display_path(md_path)}")

    if not args.no_readme:
        metrics_ok, hero_ok = update_readme(README_PATH, metrics)
        if metrics_ok and hero_ok:
            print("[metrics] README 的 hero 与 metrics 标记块已更新")
        elif metrics_ok:
            print("[metrics] README 的 metrics 块已更新;hero 标记缺失,跳过")
        elif hero_ok:
            print("[metrics] README 的 hero 块已更新;metrics 标记缺失,跳过")
        else:
            print("[metrics] README 缺少标记块,未做任何改写")

    runs = metrics["runs"]
    print(
        f"[metrics] runs={runs['total']} 成功率={_fmt_pct(runs['success_rate'])} "
        f"沙箱耗时 p50={_fmt_seconds(runs['duration_p50_ms'])} "
        f"审批 auto 占比={_fmt_pct(metrics['approvals']['auto_ratio'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
