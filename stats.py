"""确定性统计计算层：只做数值计算,不依赖 LLM。

铁律：
    统计量全部由主进程的 scipy / statsmodels 计算,LLM 只负责「选方法 + 填参数 + 解读」,
    绝不让 LLM 生成统计代码,也不把这些计算放进沙箱。

约定：
    - 所有函数返回可直接 JSON 序列化的 dict(numpy 标量统一转原生类型,保留 6 位小数);
    - 缺失值显式 dropna,并返回实际参与计算的 n;
    - 列不存在 / 组不存在抛 ValueError,信息里带上可用项,便于 LLM 或用户修正;
    - 函数不含 I/O、不读环境变量,便于单测与复现。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats as sps

# 输出保留的小数位数：统计量无需更高精度,统一舍入保证报告稳定可读
DECIMALS = 6

# 正态性检验的样本量阈值：scipy 建议 shapiro 用于小样本,超过后用 normaltest
SHAPIRO_MAX_N = 5000


def _num(value: Any) -> Any:
    """把 numpy / NaN 标量转成可 JSON 序列化的原生值,并统一舍入。"""
    if value is None:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        rounded = round(value, DECIMALS)
        # 极小值(如 p=1e-9)直接舍入会变成 0,报告会误导为「p 恰好为 0」;
        # 这里改用 4 位有效数字保留量级
        if rounded == 0 and value != 0:
            return float(f"{value:.4g}")
        return rounded
    return value


def _round_dict(data: dict[str, Any]) -> dict[str, Any]:
    """对 dict 的标量值统一做序列化与舍入;嵌套结构原样保留。"""
    return {key: _num(value) for key, value in data.items()}


def _check_columns(df: pd.DataFrame, columns: Sequence[str]) -> None:
    """校验列存在,缺失时抛出带可用列名的 ValueError。"""
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise ValueError(f"列不存在: {missing};可用列: {list(df.columns)}")


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    """取数值列：无法转成数值的单元格置 NaN,交给调用方统一 dropna。

    为什么用 to_numeric：
        真实 CSV 里数值列常被读成 object(含空串、千分位等),
        直接 astype(float) 会整列失败,逐值转换更稳健且失败可见(NaN)。
    """
    return pd.to_numeric(df[column], errors="coerce")


def _normality(values: np.ndarray) -> tuple[float | None, str]:
    """正态性检验：n<=5000 用 shapiro,否则用 normaltest(D'Agostino)。

    返回：
        (p 值, 使用的检验名)。样本不足(<3)时 p 为 None。
    """
    n = len(values)
    if n < 3:
        return None, "样本不足(<3),未做正态性检验"
    if n > SHAPIRO_MAX_N:
        # 大样本下 shapiro 对微小偏离也显著,改用 normaltest 更合适
        return float(sps.normaltest(values).pvalue), "normaltest"
    return float(sps.shapiro(values).pvalue), "shapiro"


def describe_stats(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """描述性统计：集中趋势、离散程度、分布形态与均值的 95% 置信区间。

    参数：
        df: 数据表。
        col: 数值列名。

    返回：
        {n, mean, std, median, q1, q3, skew, kurtosis, ci95_low, ci95_high, missing}

    异常：
        ValueError: 列不存在,或剔除缺失值后没有有效数值。
    """
    _check_columns(df, [col])
    missing = int(df[col].isna().sum())
    values = _numeric(df, col).dropna().to_numpy(dtype=float)
    n = len(values)
    if n == 0:
        raise ValueError(f"列 {col} 没有有效数值(原始缺失 {missing} 个)")

    mean = float(values.mean())
    std = float(values.std(ddof=1)) if n > 1 else None
    # 均值的 95% CI 用 t 分布：小样本下比正态近似更保守
    if n > 1 and std is not None:
        half = float(sps.t.ppf(0.975, n - 1)) * std / math.sqrt(n)
        ci_low, ci_high = mean - half, mean + half
    else:
        ci_low = ci_high = None

    return _round_dict(
        {
            "n": n,
            "mean": mean,
            "std": std,
            "median": float(np.median(values)),
            "q1": float(np.percentile(values, 25)),
            "q3": float(np.percentile(values, 75)),
            # skew/kurtosis 用 scipy 默认(总体矩,bias=True),与常规软件口径一致
            "skew": float(sps.skew(values)) if n > 2 else None,
            "kurtosis": float(sps.kurtosis(values)) if n > 3 else None,
            "ci95_low": ci_low,
            "ci95_high": ci_high,
            "missing": missing,
        }
    )


def _group_values(
    df: pd.DataFrame,
    group_col: str,
    value_col: str,
    group_a: Any,
    group_b: Any,
) -> tuple[pd.Series, pd.Series]:
    """取出两组的数值序列并校验组存在。

    异常：
        ValueError: 列不存在、组不存在,或某组剔除缺失后为空。
    """
    _check_columns(df, [group_col, value_col])
    frame = pd.DataFrame(
        {
            "group": df[group_col],
            "value": _numeric(df, value_col),
        }
    ).dropna()

    available = sorted({str(item) for item in frame["group"].unique()})
    for label in (group_a, group_b):
        # 用字符串比对：CSV 里组名可能是数字/字符串混合,LLM 传入的也多为字符串
        if label not in available and not (
            frame["group"].astype(str) == str(label)
        ).any():
            raise ValueError(f"组不存在: {label!r};可用组: {available}")

    mask_a = frame["group"].astype(str) == str(group_a)
    mask_b = frame["group"].astype(str) == str(group_b)
    series_a = frame.loc[mask_a, "value"]
    series_b = frame.loc[mask_b, "value"]
    if series_a.empty or series_b.empty:
        raise ValueError(f"组 {group_a!r} 或 {group_b!r} 没有有效数值")
    return series_a, series_b


def ttest(
    df: pd.DataFrame,
    group_col: str,
    value_col: str,
    group_a: Any,
    group_b: Any,
    paired: bool = False,
    welch: bool = True,
) -> dict[str, Any]:
    """两组均值差异的 t 检验(独立样本或配对样本)。

    参数：
        df: 数据表。
        group_col: 分组列名。
        value_col: 数值列名。
        group_a / group_b: 两组的取值。
        paired: True 用配对 t 检验(要求两组样本量一致,按行顺序配对)。
        welch: 独立样本时是否用 Welch 校正(不假设方差齐性)。

    返回：
        {t, p, dof, mean_a, mean_b, mean_diff, ci95_low, ci95_high,
         normality_p_a, normality_p_b, levene_p, n_a, n_b, normality_test}

    异常：
        ValueError: 列 / 组不存在、有效样本不足、配对样本量不一致。
    """
    series_a, series_b = _group_values(df, group_col, value_col, group_a, group_b)

    if paired:
        # 配对要求两组按行一一对应：样本量不一致说明数据未对齐,直接报错而不是硬算
        if len(series_a) != len(series_b):
            raise ValueError(
                f"配对检验要求两组样本量一致,实际 {len(series_a)} vs {len(series_b)}"
            )
        if len(series_a) < 2:
            raise ValueError("配对检验至少需要 2 对样本")
        diff = series_a.to_numpy(dtype=float) - series_b.to_numpy(dtype=float)
        if float(diff.std(ddof=1)) == 0:
            # 差值全部相同：t 统计量为无穷(scipy 返回 inf),无法给出可解释的 CI
            raise ValueError("配对差值方差为 0(所有配对差值相同),无法做配对 t 检验")
        result = sps.ttest_rel(series_a.to_numpy(), series_b.to_numpy())
        dof = len(diff) - 1
        half = float(sps.t.ppf(0.975, dof)) * float(diff.std(ddof=1)) / math.sqrt(len(diff))
        mean_diff = float(diff.mean())
        ci_low, ci_high = mean_diff - half, mean_diff + half
        levene_p = None  # 配对设计不做方差齐性检验
    else:
        if len(series_a) < 2 or len(series_b) < 2:
            raise ValueError(
                f"独立样本 t 检验每组至少需要 2 个有效值,实际 {len(series_a)} vs {len(series_b)}"
            )
        a = series_a.to_numpy(dtype=float)
        b = series_b.to_numpy(dtype=float)
        result = sps.ttest_ind(a, b, equal_var=not welch)
        var_a, var_b = a.var(ddof=1), b.var(ddof=1)
        mean_diff = float(a.mean() - b.mean())
        if welch:
            # Welch–Satterthwaite 自由度;scipy 的返回值带 .df,缺失时按公式手算
            dof = float(getattr(result, "df", None) or _welch_dof(len(a), var_a, len(b), var_b))
            se = math.sqrt(var_a / len(a) + var_b / len(b))
        else:
            dof = float(len(a) + len(b) - 2)
            pooled = ((len(a) - 1) * var_a + (len(b) - 1) * var_b) / dof
            se = math.sqrt(pooled * (1 / len(a) + 1 / len(b)))
        half = float(sps.t.ppf(0.975, dof)) * se
        ci_low, ci_high = mean_diff - half, mean_diff + half
        levene_p = float(sps.levene(a, b).pvalue)

    p_a, test_name = _normality(series_a.to_numpy(dtype=float))
    p_b, _ = _normality(series_b.to_numpy(dtype=float))

    return _round_dict(
        {
            "t": float(result.statistic),
            "p": float(result.pvalue),
            "dof": dof,
            "mean_a": float(series_a.mean()),
            "mean_b": float(series_b.mean()),
            "mean_diff": mean_diff,
            "ci95_low": ci_low,
            "ci95_high": ci_high,
            "normality_p_a": p_a,
            "normality_p_b": p_b,
            "levene_p": levene_p,
            "n_a": int(len(series_a)),
            "n_b": int(len(series_b)),
            "normality_test": test_name,
        }
    )


def _welch_dof(n_a: int, var_a: float, n_b: int, var_b: float) -> float:
    """Welch–Satterthwaite 自由度公式(scipy 未返回 df 时的兜底)。"""
    numerator = (var_a / n_a + var_b / n_b) ** 2
    denominator = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    return numerator / denominator if denominator else float(n_a + n_b - 2)


def chi_square(df: pd.DataFrame, row_col: str, col_col: str) -> dict[str, Any]:
    """列联表卡方独立性检验 + Cramér's V 效应量。

    参数：
        df: 数据表。
        row_col / col_col: 两个分类列名。

    返回：
        {chi2, p, dof, cramers_v, min_expected, n}

    异常：
        ValueError: 列不存在或有效样本不足(列联表退化)。
    """
    _check_columns(df, [row_col, col_col])
    frame = df[[row_col, col_col]].dropna()
    if frame.empty:
        raise ValueError(f"{row_col} / {col_col} 剔除缺失后没有有效数据")

    table = pd.crosstab(frame[row_col], frame[col_col])
    if table.shape[0] < 2 or table.shape[1] < 2:
        raise ValueError(
            f"列联表维度不足({table.shape[0]} 行 × {table.shape[1]} 列),无法做卡方检验"
        )

    # correction=False：使用标准 Pearson 卡方(2×2 的 Yates 校正会改变 p 值口径)
    result = sps.chi2_contingency(table, correction=False)
    chi2, p, dof, expected = result[0], result[1], result[2], result[3]
    n = int(table.to_numpy().sum())
    # Cramér's V：把卡方标准化到 [0, 1],便于跨表比较关联强度
    cramers_v = math.sqrt(chi2 / (n * (min(table.shape) - 1))) if n else None

    return _round_dict(
        {
            "chi2": float(chi2),
            "p": float(p),
            "dof": int(dof),
            "cramers_v": float(cramers_v) if cramers_v is not None else None,
            "min_expected": float(expected.min()),
            "n": n,
        }
    )


def anova(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, Any]:
    """单因素方差分析(one-way ANOVA)+ eta² 效应量。

    参数：
        df: 数据表。
        group_col: 分组列名(至少 2 个组)。
        value_col: 数值列名。

    返回：
        {F, p, eta_sq, groups, normality_p, levene_p, normality_by_group}

    异常：
        ValueError: 列不存在、有效组不足 2 个,或某组样本不足。
    """
    _check_columns(df, [group_col, value_col])
    frame = pd.DataFrame(
        {"group": df[group_col], "value": _numeric(df, value_col)}
    ).dropna()
    if frame.empty:
        raise ValueError(f"{group_col} / {value_col} 剔除缺失后没有有效数据")

    grouped = [(str(name), group["value"].to_numpy(dtype=float)) for name, group in frame.groupby("group")]
    if len(grouped) < 2:
        raise ValueError(f"组数不足({len(grouped)} 个),ANOVA 至少需要 2 组")
    if any(len(values) < 2 for _, values in grouped):
        raise ValueError("每个组至少需要 2 个有效值")

    result = sps.f_oneway(*[values for _, values in grouped])
    all_values = frame["value"].to_numpy(dtype=float)
    grand_mean = float(all_values.mean())
    # eta² = 组间平方和 / 总平方和：手工计算,避免额外依赖
    ss_between = sum(len(values) * (float(values.mean()) - grand_mean) ** 2 for _, values in grouped)
    ss_total = float(((all_values - grand_mean) ** 2).sum())
    eta_sq = ss_between / ss_total if ss_total else None

    normality_by_group = {}
    for name, values in grouped:
        p_value, _ = _normality(values)
        normality_by_group[name] = _num(p_value)
    valid_p = [value for value in normality_by_group.values() if value is not None]
    levene_p = float(sps.levene(*[values for _, values in grouped]).pvalue)

    return {
        "F": _num(float(result.statistic)),
        "p": _num(float(result.pvalue)),
        "eta_sq": _num(eta_sq),
        "groups": [
            {"name": name, "n": int(len(values)), "mean": _num(float(values.mean()))}
            for name, values in grouped
        ],
        "normality_p": _num(min(valid_p)) if valid_p else None,
        "levene_p": _num(levene_p),
        "normality_by_group": normality_by_group,
    }


def correlation(
    df: pd.DataFrame, x: str, y: str, method: str = "pearson"
) -> dict[str, Any]:
    """两变量相关分析。

    参数：
        df: 数据表。
        x / y: 两个数值列名。
        method: "pearson"(线性相关)或 "spearman"(单调关系,抗异常值)。

    返回：
        {r, p, n, method}

    异常：
        ValueError: 列不存在、method 非法,或有效样本不足 3。
    """
    if method not in ("pearson", "spearman"):
        raise ValueError(f"method 只支持 pearson / spearman,收到: {method}")
    _check_columns(df, [x, y])
    frame = pd.DataFrame({"x": _numeric(df, x), "y": _numeric(df, y)}).dropna()
    n = len(frame)
    if n < 3:
        raise ValueError(f"相关分析至少需要 3 个有效样本,实际 {n}")

    if method == "spearman":
        result = sps.spearmanr(frame["x"], frame["y"])
    else:
        result = sps.pearsonr(frame["x"], frame["y"])

    return _round_dict(
        {"r": float(result.statistic), "p": float(result.pvalue), "n": n, "method": method}
    )


def linear_regression(df: pd.DataFrame, y: str, xs: Sequence[str]) -> dict[str, Any]:
    """多元线性回归(OLS),返回整体拟合优度与各系数显著性。

    参数：
        df: 数据表。
        y: 因变量列名。
        xs: 自变量列名列表(可多个)。

    返回：
        {r2, adj_r2, F, p, n, coefficients: [{name, coef, std_err, t, p}]}

    异常：
        ValueError: 列缺失、自变量为空、样本不足,或模型无法估计(完全共线性)。
    """
    xs = list(xs)
    if not xs:
        raise ValueError("linear_regression 至少需要 1 个自变量(xs)")
    _check_columns(df, [y, *xs])

    frame = pd.DataFrame({"__y": _numeric(df, y)})
    for column in xs:
        frame[column] = _numeric(df, column)
    frame = frame.dropna()
    if len(frame) <= len(xs) + 1:
        raise ValueError(
            f"样本不足：{len(frame)} 行无法估计 {len(xs)} 个自变量(至少需 {len(xs) + 2} 行)"
        )

    # add_constant 显式加截距：statsmodels 默认不加,漏掉会改变 R² 口径
    design = sm.add_constant(frame[xs], has_constant="add")
    try:
        model = sm.OLS(frame["__y"], design).fit()
    except Exception as exc:
        raise ValueError(f"回归模型无法估计(可能存在完全共线性): {exc}") from exc

    coefficients = [
        {
            "name": name,
            "coef": _num(float(model.params[name])),
            "std_err": _num(float(model.bse[name])),
            "t": _num(float(model.tvalues[name])),
            "p": _num(float(model.pvalues[name])),
        }
        for name in ["const", *xs]
    ]

    return {
        "r2": _num(float(model.rsquared)),
        "adj_r2": _num(float(model.rsquared_adj)),
        "F": _num(float(model.fvalue)),
        "p": _num(float(model.f_pvalue)),
        "n": int(model.nobs),
        "coefficients": coefficients,
    }


def ab_sample_size(
    baseline: float, mde: float, alpha: float = 0.05, power: float = 0.8
) -> dict[str, Any]:
    """A/B 实验两组所需样本量(两比例 z 检验的正态近似)。

    参数：
        baseline: 对照组基线转化率(0~1)。
        mde: 绝对提升幅度(如 baseline=0.10、mde=0.02 表示目标 12%)。
        alpha: 显著性水平(双侧)。
        power: 统计功效(1 - β)。

    返回：
        {n_per_group, total, assumptions}

    异常：
        ValueError: 参数越界(比率不在 0~1、mde 非正、提升后超过 1 等)。
    """
    for name, value in (("baseline", baseline), ("alpha", alpha), ("power", power)):
        if not 0 < float(value) < 1:
            raise ValueError(f"{name} 必须在 (0, 1) 之间,收到: {value}")
    if float(mde) <= 0:
        raise ValueError(f"mde(绝对提升幅度)必须为正数,收到: {mde}")

    p1 = float(baseline)
    p2 = p1 + float(mde)
    if p2 >= 1:
        raise ValueError(f"baseline + mde = {p2:.4f} 超出 (0, 1),请检查提升幅度")

    pooled = (p1 + p2) / 2
    z_alpha = float(sps.norm.ppf(1 - float(alpha) / 2))
    z_power = float(sps.norm.ppf(float(power)))
    numerator = z_alpha * math.sqrt(2 * pooled * (1 - pooled)) + z_power * math.sqrt(
        p1 * (1 - p1) + p2 * (1 - p2)
    )
    n_per_group = math.ceil((numerator / float(mde)) ** 2)

    return {
        "n_per_group": int(n_per_group),
        "total": int(n_per_group * 2),
        "assumptions": {
            "baseline": _num(p1),
            "mde": _num(float(mde)),
            "mde_mode": "absolute(绝对提升,目标值 = baseline + mde)",
            "alpha": _num(float(alpha)),
            "power": _num(float(power)),
            "alternative": "two-sided",
            "method": "两比例 z 检验 + 正态近似",
            "note": "未做连续性校正;极端低基线(如 <1%)建议用精确检验或增加样本量",
        },
    }
