"""validate_report 数字溯源的回归测试(批次 B 收尾补丁)。

覆盖两类场景:
1. 回归:字段数 / 行数 / 空值率这类合法来源(或裸个位数)不得误报;
2. 真误报:汇总里出现 stdout / schema 都没有的指标(带小数或单位)必须报警。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from validate import validate_report  # noqa: E402  (sys.path 调整后再导入)


def test_no_false_positive_for_schema_derived_numbers():
    """800 行 / 5 个字段 / 空值率 0.0 都来自 schema,不能报「自造数字」。"""
    schema = {
        "name": "测试数据.csv",
        "row_count": 800,
        "encoding": "utf-8-sig",
        "size_bytes": 12345,
        "sha256": "b075603113f9" * 6,
        "columns": [
            {"name": "用户ID", "dtype": "int64", "null_ratio": 0.0},
            {"name": "分组", "dtype": "object", "null_ratio": 0.0},
            {"name": "收入", "dtype": "float64", "null_ratio": 0.0},
            {"name": "是否购买", "dtype": "bool", "null_ratio": 0.0},
            {"name": "访问时长", "dtype": "int32", "null_ratio": 0.0},
        ],
    }
    summary = {
        "headline": "测试数据.csv 共 800 行,测试组收入均值高于对照组",
        "findings": [
            "测试数据.csv 共 800 行",
            "共 5 个字段",
            "收入列空值率 0.0",
            "测试组收入均值 70.1,对照组 66.5",
            "两组的行数相差 1 行",
        ],
        "recommendations": ["按分组口径复核一次"],
        "caveats": ["仅为描述统计,未做显著性检验", "编码为 utf-8-sig,已自动识别"],
        "method": "按分组列 groupby 求均值,空值不参与计算",
    }
    stdout = "测试组均值: 70.1\n对照组均值: 66.5"

    warnings = validate_report(summary, stdout, schema)
    assert warnings == [], f"不应有误报,实际: {warnings}"


def test_real_fabricated_number_is_reported_once():
    """summary 写了 stdout / schema 都没有的 12.5%,应恰好产生 1 条警告。"""
    schema = {"row_count": 800, "columns": []}
    summary = {
        "headline": "与对照组相比收入提升 12.5%",
        "findings": [],
        "recommendations": [],
        "caveats": ["仅为描述统计"],
        "method": "按分组求均值",
    }
    stdout = "测试组均值: 70.1\n对照组均值: 66.5"

    warnings = validate_report(summary, stdout, schema)
    assert len(warnings) == 1, f"应恰好 1 条警告,实际: {warnings}"
    assert "12.5" in warnings[0]


def test_quantile_labels_and_ranges_are_not_metrics():
    """method 里的分位数标签(P50 / 50% 分位)与区间写法(60-80)不是指标,不能误报。

    背景:真实报告的 method 会写「按 5%/25%/50%/75%/90%/95%/99% 分位数刻画分布」,
    其中的 50 曾被当成「汇总疑似自造数字」。
    """
    schema = {"row_count": 800, "columns": [{"name": "收入", "null_ratio": 0.0}]}
    summary = {
        "headline": "收入分布右偏,均值 70.1",
        "findings": ["均值 70.1"],
        "recommendations": [],
        "caveats": ["收入区间 60-80 覆盖了主要样本"],
        "method": (
            "按 5%/25%/50%/75%/90%/95%/99% 分位数刻画分布,"
            "并用 P50 / P95 标注中位与上分位;分箱区间 60~80、100 至 200 仅作口径说明"
        ),
    }
    stdout = "均值: 70.1"

    warnings = validate_report(summary, stdout, schema)
    assert warnings == [], f"分位标签 / 区间不应报警,实际: {warnings}"
