"""生成可复现的演示数据(固定随机种子,不含任何真实业务数据)。

产出:
    examples/demo_sales.csv       订单明细,约 1200 行,含少量缺失值与 3 个异常值;
    examples/demo_users_pii.csv   用户表,含"手机号"/"邮箱"列,值全部为假数据。

默认同时复制到 data/processed/,供 Web 端与 CLI 直接选择;
加 --no-copy 只写 examples/,不碰 data/processed/。

用法:
    python scripts/make_demo_data.py
    python scripts/make_demo_data.py --rows 1200 --seed 20261101 --no-copy

说明:
    1. 行数 / 缺失值 / 异常值位置全部由种子决定,同一命令产出字节级一致的文件
       (pandas 浮点格式固定,不依赖平台);
    2. demo_users_pii.csv 只用于演示"疑似包含个人信息字段"的审批理由,
       手机号统一为 1380000xxxx / 邮箱统一为 example.com,均为不可达的假值;
    3. 生成后会打印文件 sha256,便于在不同机器上核对是否为同一份数据。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = PROJECT_ROOT / "examples"
DATA_DIR = PROJECT_ROOT / "data" / "processed"

# 维度取值:刻意保持少量枚举值,方便分组 / 渠道拆解类问题有稳定口径
REGIONS = ("华东", "华北", "华南", "西南", "华中")
CHANNELS = ("线上商城", "线下门店", "小程序", "直播")
REGION_BASE = {"华东": 430.0, "华北": 390.0, "华南": 410.0, "西南": 360.0, "华中": 380.0}
CHANNEL_FACTOR = {"线上商城": 1.05, "线下门店": 0.95, "小程序": 0.88, "直播": 1.18}

# 订单日期覆盖 2026 年 1-9 月,让"按月趋势"有 9 个点
START_DATE = date(2026, 1, 1)
DATE_SPAN_DAYS = 273

# 缺失与异常的比例:足以让数据质量警告与 IQR 异常值有东西可讲,又不淹没正常数据
MISSING_AMOUNT_COUNT = 15
MISSING_CHANNEL_COUNT = 8
ANOMALY_VALUES = (99999.00, 0.01, -520.00)


def build_sales(rows: int, seed: int) -> pd.DataFrame:
    """按固定种子生成订单明细。

    金额 = 地区基准 × 渠道系数 × 月度季节因子 + 正态噪声;
    9 月给 1.15 倍季节因子,让趋势图有一个可解释的抬升。
    """
    rng = np.random.default_rng(seed)

    regions = rng.choice(REGIONS, size=rows)
    channels = rng.choice(CHANNELS, size=rows)
    offsets = rng.integers(0, DATE_SPAN_DAYS, size=rows)
    dates = [START_DATE + timedelta(days=int(offset)) for offset in offsets]

    base = np.array([REGION_BASE[r] for r in regions])
    factor = np.array([CHANNEL_FACTOR[c] for c in channels])
    # 月份季节因子:1-8 月平缓,9 月抬升,便于讲"趋势 + 业务解释"
    month_factor = np.array([1.15 if d.month == 9 else 1.0 + (d.month - 1) * 0.01 for d in dates])
    noise = rng.normal(0, 55, size=rows)
    amount = np.round(np.clip(base * factor * month_factor + noise, 20, 1500), 2)

    is_new = np.where(rng.random(rows) < 0.28, "是", "否")
    # 复购率与新客负相关:新客首单复购概率低,老客高
    repurchase_prob = np.where(is_new == "是", 0.12, 0.55)
    is_repurchase = np.where(rng.random(rows) < repurchase_prob, "是", "否")

    frame = pd.DataFrame(
        {
            "订单日期": dates,
            "地区": regions,
            "渠道": channels,
            "新客": is_new,
            "订单金额": amount,
            "是否复购": is_repurchase,
        }
    )

    # 缺失值:模拟真实 CSV 的空单元格(pandas 写出来就是空字段)
    missing_amount_idx = rng.choice(rows, size=MISSING_AMOUNT_COUNT, replace=False)
    frame.loc[missing_amount_idx, "订单金额"] = np.nan
    remaining = np.setdiff1d(np.arange(rows), missing_amount_idx)
    missing_channel_idx = rng.choice(remaining, size=MISSING_CHANNEL_COUNT, replace=False)
    frame.loc[missing_channel_idx, "渠道"] = np.nan

    # 异常值:3 个明显不合理的金额,用于演示 IQR 异常值统计
    anomaly_idx = rng.choice(remaining, size=len(ANOMALY_VALUES), replace=False)
    for idx, value in zip(anomaly_idx, ANOMALY_VALUES):
        frame.loc[int(idx), "订单金额"] = value

    # 按日期排序让文件可肉眼浏览;索引不落盘
    frame = frame.sort_values("订单日期", kind="stable").reset_index(drop=True)
    return frame


def build_users(seed: int, rows: int = 24) -> pd.DataFrame:
    """生成假用户表:列名含 手机号 / 邮箱,用于演示 PII 审批理由。

    姓名用"演示用户NN"而不是任何真实姓名;手机号 / 邮箱均为不可达的假值。
    """
    rng = np.random.default_rng(seed + 1)
    offsets = rng.integers(0, DATE_SPAN_DAYS, size=rows)
    return pd.DataFrame(
        {
            "用户ID": [f"U{i:04d}" for i in range(1, rows + 1)],
            "姓名": [f"演示用户{i:02d}" for i in range(1, rows + 1)],
            "手机号": [f"1380000{i:04d}" for i in range(1, rows + 1)],
            "邮箱": [f"demo{i:03d}@example.com" for i in range(1, rows + 1)],
            "城市": rng.choice(REGIONS, size=rows),
            "注册日期": [START_DATE + timedelta(days=int(x)) for x in offsets],
        }
    )


def _sha256(path: Path) -> str:
    """分块计算文件哈希,避免大文件一次性进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成演示数据(固定种子,可复现)")
    parser.add_argument("--rows", type=int, default=1200, help="订单表行数,默认 1200")
    parser.add_argument("--seed", type=int, default=20261101, help="随机种子,默认 20261101")
    parser.add_argument(
        "--no-copy", action="store_true", help="只写 examples/,不复制到 data/processed/"
    )
    args = parser.parse_args()

    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    sales = build_sales(args.rows, args.seed)
    users = build_users(args.seed)

    sales_path = EXAMPLES_DIR / "demo_sales.csv"
    users_path = EXAMPLES_DIR / "demo_users_pii.csv"
    sales.to_csv(sales_path, index=False, encoding="utf-8")
    users.to_csv(users_path, index=False, encoding="utf-8")

    print(f"[demo] 订单数据: {sales_path.relative_to(PROJECT_ROOT)}  ({len(sales)} 行)")
    print(f"       缺失值: 订单金额 {MISSING_AMOUNT_COUNT} 个 / 渠道 {MISSING_CHANNEL_COUNT} 个")
    print(f"       异常值: {', '.join(str(v) for v in ANOMALY_VALUES)}")
    print(f"       sha256: {_sha256(sales_path)}")
    print(f"[demo] 假用户表: {users_path.relative_to(PROJECT_ROOT)}  ({len(users)} 行)")
    print(f"       手机号 / 邮箱均为不可达假值,仅用于演示 PII 审批理由")
    print(f"       sha256: {_sha256(users_path)}")

    if not args.no_copy:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sales_path, DATA_DIR / "demo_sales.csv")
        shutil.copy2(users_path, DATA_DIR / "demo_users_pii.csv")
        print(f"[demo] 已复制到 {DATA_DIR.relative_to(PROJECT_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
