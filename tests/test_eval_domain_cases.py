"""eval/run_eval.py 支持 --cases 传入领域包 JSON 且扩展字段不报错的单元测试。

跑的是 load_cases + check_case 这一段的离线能力,不真的调 run_agent。
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.run_eval import check_case, load_cases

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = PROJECT_ROOT / "eval" / "cases.json"
DOMAIN_CASES = PROJECT_ROOT / "domains" / "ecommerce" / "eval" / "cases.json"


def test_load_cases_default_path():
    """不传 --cases 时,默认读 eval/cases.json,包含原有 9 条用例。"""
    cases = load_cases()
    assert len(cases) == 9
    ids = {c["id"] for c in cases}
    assert "case_discover" in ids
    assert "case_ab_significance" in ids


def test_load_cases_default_path_explicit():
    """显式传默认路径时结果一样。"""
    cases = load_cases(DEFAULT_CASES)
    assert len(cases) == 9


def test_load_cases_domain_path_no_keyerror():
    """传入领域包 cases.json,包含扩展字段(metrics / datasets / tolerance)也不报错。"""
    cases = load_cases(DOMAIN_CASES)
    assert len(cases) == 13
    # 扩展字段不会让 load_cases KeyError
    for case in cases:
        assert "id" in case
        assert "question" in case
        assert "checks" in case

    # 抽查一条用例的扩展字段确实被保留了(后续如需做更细的断言可用)
    first = cases[0]
    assert "metrics" in first
    assert "datasets" in first


def test_load_cases_domain_only_filter():
    """--only 过滤在领域包 cases 里也正常工作。"""
    cases = load_cases(DOMAIN_CASES, only_id="ec_repurchase_rate")
    assert len(cases) == 1
    assert cases[0]["id"] == "ec_repurchase_rate"


def test_load_cases_missing_path_raises(tmp_path: Path):
    """路径不存在时抛 FileNotFoundError,不默默返回空。"""
    import pytest

    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "nonexistent.json")


def test_check_case_reports_section_passes():
    """用 check_case 跑一条最简单的 case:final_not_empty + 不跑 run_agent 只测 checks。"""
    case = {
        "id": "test_simple",
        "question": "dummy",
        "checks": {
            "final_not_empty": True,
            "final_contains": ["GMV"],
        },
    }
    # 用例 checks 不涉及 report 文件 → state 传 dict 就能过
    state = {"final_answer": "GMV=1234", "error_analysis": "", "messages": [], "exec_result": {}}
    failures = check_case(case, state, duration_ms=10)
    assert failures == []


def test_check_case_final_contains_missing():
    """final_contains 缺失时返回失败原因。"""
    case = {
        "id": "test_fail",
        "question": "dummy",
        "checks": {"final_contains": ["必须出现这段"]},
    }
    state = {"final_answer": "完全无关的内容", "error_analysis": "", "messages": [], "exec_result": {}}
    failures = check_case(case, state, duration_ms=10)
    assert failures
    assert "必须出现这段" in failures[0]


def test_domain_cases_json_valid():
    """领域包 cases.json 是合法 JSON,顶层 domain / version / note 字段存在。"""
    doc = json.loads(DOMAIN_CASES.read_text(encoding="utf-8"))
    assert doc["domain"] == "ecommerce"
    assert "version" in doc
    assert "note" in doc
    assert isinstance(doc["cases"], list)


def test_stage_case_datasets_copies_once(tmp_path: Path, monkeypatch):
    """领域包用例的数据集只复制一次,已存在时不覆盖。"""
    from eval import run_eval

    pack = tmp_path / "domains" / "demo"
    (pack / "examples").mkdir(parents=True)
    source = pack / "examples" / "orders.csv"
    source.write_text("a\n1\n", encoding="utf-8")

    # 把项目根指到临时目录,避免真的写项目 data/processed
    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path / "proj")

    case = {"datasets": ["orders.csv"]}
    staged = run_eval._stage_case_datasets(case, pack)
    target = tmp_path / "proj" / "data" / "processed" / "orders.csv"
    assert target.read_text(encoding="utf-8") == "a\n1\n"
    assert staged == [target]

    # 源文件被改后再次调用:目标已存在,保持原内容,且不再计入 staged
    source.write_text("b\n2\n", encoding="utf-8")
    staged2 = run_eval._stage_case_datasets(case, pack)
    assert target.read_text(encoding="utf-8") == "a\n1\n"
    assert staged2 == []


def test_stage_and_cleanup_only_removes_staged(tmp_path: Path, monkeypatch):
    """清理时只删本次复制的文件;用户原有的同名文件绝不动。"""
    from eval import run_eval

    pack = tmp_path / "domains" / "demo"
    (pack / "examples").mkdir(parents=True)
    (pack / "examples" / "orders.csv").write_text("a\n1\n", encoding="utf-8")
    (pack / "examples" / "users.csv").write_text("b\n2\n", encoding="utf-8")

    monkeypatch.setattr(run_eval, "PROJECT_ROOT", tmp_path / "proj")
    processed = tmp_path / "proj" / "data" / "processed"
    processed.mkdir(parents=True)
    # 用户手工放了一个同名文件 → 已存在,不属于"本次复制"
    (processed / "users.csv").write_text("user owned\n", encoding="utf-8")

    case = {"datasets": ["orders.csv", "users.csv"]}
    staged = run_eval._stage_case_datasets(case, pack)
    assert staged == [processed / "orders.csv"]

    run_eval._cleanup_staged_datasets(staged)
    assert not (processed / "orders.csv").exists()
    assert (processed / "users.csv").read_text(encoding="utf-8") == "user owned\n"


# ---------- metric_tolerance 数字裁判 ----------


def _state_with_stdout(stdout: str) -> dict:
    """构造只带 stdout 的最小 state,供 metric_tolerance 检查使用。"""
    return {
        "final_answer": "",
        "error_analysis": "",
        "messages": [],
        "exec_result": {"stdout": stdout},
    }


def test_metric_tolerance_pass_within_rel_tol():
    """METRIC 行解析成功且相对误差在容差内 → 通过。"""
    case = {
        "id": "t",
        "question": "q",
        "checks": {},
        "tolerance": {"metric": "gmv", "value": 1193284.77, "rel_tol": 0.01},
    }
    state = _state_with_stdout("若干输出\nMETRIC gmv=1193284.770000\n")
    assert check_case(case, state, duration_ms=10) == []


def test_metric_tolerance_missing_line_fails():
    """stdout 里没有对应 METRIC 行 → FAIL,原因写「未输出 METRIC <id>」。"""
    case = {
        "id": "t",
        "question": "q",
        "checks": {},
        "tolerance": {"metric": "gmv", "value": 1193284.77, "rel_tol": 0.01},
    }
    state = _state_with_stdout("只打印了表格,没有 METRIC 行")
    failures = check_case(case, state, duration_ms=10)
    assert failures
    assert "未输出 METRIC gmv" in failures[0]


def test_metric_tolerance_wrong_formula_fails_with_expected_actual():
    """故意把 GMV 算成订单金额求和:相对误差超容差 → FAIL 且带期望值/实际值。"""
    case = {
        "id": "ec_daily_gmv_trend",
        "question": "q",
        "checks": {},
        "tolerance": {"metric": "gmv", "value": 1193284.77, "rel_tol": 0.01},
    }
    # 模拟「订单金额求和」的错误口径:数值明显偏离干净口径 GMV
    state = _state_with_stdout("METRIC gmv=1256789.120000\n")
    failures = check_case(case, state, duration_ms=10)
    assert failures
    assert "1193284.77" in failures[0]
    assert "1256789.12" in failures[0]
    assert "相对误差" in failures[0]


def test_metric_tolerance_checks_key_takes_precedence():
    """checks.metric_tolerance 优先于用例顶层 tolerance 字段。"""
    case = {
        "id": "t",
        "question": "q",
        "checks": {
            "metric_tolerance": {"metric_id": "aov", "value": 706.09, "rel_tol": 0.01}
        },
        # 顶层 tolerance 提供的是另一个指标,不应被采用
        "tolerance": {"metric": "gmv", "value": 1193284.77, "rel_tol": 0.01},
    }
    state = _state_with_stdout("METRIC gmv=1193284.770000\nMETRIC aov=706.090000\n")
    assert check_case(case, state, duration_ms=10) == []


def test_parse_metric_lines_skips_bad_rows():
    """解析只认「METRIC <id>=<数值>」行;格式坏 / 数值坏 / 其他行都跳过。"""
    from eval.run_eval import _parse_metric_lines

    stdout = (
        "METRIC gmv=1193284.770000\n"
        "GMV 总计 1193284.77\n"
        "METRIC bad_line\n"
        "METRIC broken=abc\n"
        "METRIC =1.0\n"
        "METRIC refund_rate=0.050959\n"
    )
    parsed = _parse_metric_lines(stdout)
    assert parsed == {"gmv": 1193284.77, "refund_rate": 0.050959}
