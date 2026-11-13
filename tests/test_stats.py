"""stats 7 个确定性统计方法的合成数据测试 + 异常分支(离线)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from stats import (  # noqa: E402
    ab_sample_size,
    anova,
    chi_square,
    correlation,
    describe_stats,
    linear_regression,
    ttest,
)


def _p_in_range(value: float) -> bool:
    return 0.0 <= value <= 1.0


def test_describe_stats_returns_n_and_moments():
    df = pd.DataFrame({"收入": [float(v) for v in range(1, 21)]})
    result = describe_stats(df, "收入")

    assert result["n"] == 20
    assert result["missing"] == 0
    assert abs(result["mean"] - 10.5) < 1e-6
    assert result["ci95_low"] is not None and result["ci95_high"] is not None


def test_ttest_returns_p_and_group_sizes():
    # 两组明显不同,期望 p 很小
    df = pd.DataFrame(
        {
            "分组": ["A"] * 10 + ["B"] * 10,
            "收入": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
            + [20, 21, 22, 23, 24, 25, 26, 27, 28, 29],
        }
    )
    result = ttest(df, "分组", "收入", "A", "B")

    assert _p_in_range(result["p"])
    assert result["p"] < 0.05
    assert result["n_a"] == 10 and result["n_b"] == 10
    assert result["mean_diff"] < 0


def test_chi_square_returns_n_and_cramers_v():
    df = pd.DataFrame(
        {
            "性别": ["男"] * 40 + ["女"] * 40,
            "购买": ["是"] * 30 + ["否"] * 10 + ["是"] * 10 + ["否"] * 30,
        }
    )
    result = chi_square(df, "性别", "购买")

    assert _p_in_range(result["p"])
    assert result["n"] == 80
    assert result["dof"] == 1
    assert result["cramers_v"] is not None


def test_anova_returns_groups_and_eta():
    df = pd.DataFrame(
        {
            "分组": ["A"] * 8 + ["B"] * 8 + ["C"] * 8,
            "得分": list(range(1, 9)) + list(range(11, 19)) + list(range(21, 29)),
        }
    )
    result = anova(df, "分组", "得分")

    assert _p_in_range(result["p"])
    assert len(result["groups"]) == 3
    assert result["eta_sq"] is not None and 0.0 <= result["eta_sq"] <= 1.0


def test_correlation_returns_r_and_n():
    df = pd.DataFrame({"x": [1, 2, 3, 4, 5], "y": [2, 4, 6, 8, 10]})
    result = correlation(df, "x", "y")

    assert _p_in_range(result["p"])
    assert result["n"] == 5
    assert abs(result["r"] - 1.0) < 1e-9


def test_linear_regression_returns_fit_and_coefficients():
    df = pd.DataFrame({"y": [1, 2, 3, 4, 5, 6], "x": [1, 2, 3, 4, 5, 6]})
    result = linear_regression(df, "y", ["x"])

    assert result["n"] == 6
    assert _p_in_range(result["p"])
    assert 0.0 <= result["r2"] <= 1.0
    names = [item["name"] for item in result["coefficients"]]
    assert names == ["const", "x"]


def test_ab_sample_size_returns_per_group_and_total():
    result = ab_sample_size(baseline=0.10, mde=0.02)

    assert result["n_per_group"] > 0
    assert result["total"] == result["n_per_group"] * 2
    # 提升幅度越小,所需样本越大(方向性 sanity check)
    larger = ab_sample_size(baseline=0.10, mde=0.01)
    assert larger["n_per_group"] > result["n_per_group"]


def test_missing_column_raises():
    df = pd.DataFrame({"收入": [1, 2, 3]})
    with pytest.raises(ValueError, match="列不存在"):
        describe_stats(df, "不存在的列")


def test_missing_group_raises():
    df = pd.DataFrame({"分组": ["A", "B"], "收入": [1, 2]})
    with pytest.raises(ValueError, match="组不存在"):
        ttest(df, "分组", "收入", "A", "C")


def test_insufficient_samples_raise():
    df = pd.DataFrame({"x": [1, 2], "y": [2, 4]})
    with pytest.raises(ValueError, match="至少需要 3"):
        correlation(df, "x", "y")


def test_constant_data_raises_for_describe():
    """整列为 NaN 时没有有效数值,必须报错而不是返回空结论。"""
    df = pd.DataFrame({"收入": [float("nan"), float("nan")]})
    with pytest.raises(ValueError, match="没有有效数值"):
        describe_stats(df, "收入")
