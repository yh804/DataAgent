"""生成电商领域包的合成演示数据：固定种子、可复现、无任何真实业务数据。

输入文件与编码:
    无外部输入；输出统一 UTF-8 with BOM(utf-8-sig)，与 examples/demo_sales.csv 一致。
关键列假设:
    1. 金额单位为元，保留 2 位小数；
    2. 订单日期是下单自然日；支付时间 / 发货时间为空表示未发生；
    3. 新客按「用户首笔干净支付订单」标记，属于本包 draft 口径；
    4. 用户手机号为不可达的脱敏假值，仅用于演示 PII 识别。
输出路径:
    domains/ecommerce/examples/orders.csv        订单表（约 2000 行）
    domains/ecommerce/examples/order_items.csv   订单明细表
    domains/ecommerce/examples/users.csv         用户表
    domains/ecommerce/examples/products.csv      商品表
    domains/ecommerce/examples/traffic.csv       流量表
复现方式:
    python domains/ecommerce/examples/generate_demo_data.py
"""

from __future__ import annotations

import csv
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

# 固定种子：任何一次运行都生成同一份数据，便于回归与结论复现
SEED = 20260101

# 数据规模：orders 是主表锚点，其余表按业务关系配套生成
N_ORDERS = 2000
N_USERS = 1500
N_PRODUCTS = 120

START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 3, 31)
OUT_DIR = Path(__file__).resolve().parent

CHANNELS = ("线下门店", "线上商城", "小程序", "直播")
REGIONS = ("华北", "华东", "华南", "华中", "西南")
CATEGORIES = ("服饰", "美妆", "食品", "家居", "数码")

# 品类热度：用于订单选品，影响品类结构分析的形态
CATEGORY_WEIGHTS = (0.30, 0.22, 0.20, 0.18, 0.10)
# 品类单价区间（元）：用于生成商品定价，不来自真实价格
PRICE_RANGES = {
    "服饰": (99, 399),
    "美妆": (59, 299),
    "食品": (9, 99),
    "家居": (49, 499),
    "数码": (199, 1999),
}
SUBCATEGORIES = {
    "服饰": ("上衣", "裤装", "外套", "配饰"),
    "美妆": ("护肤", "彩妆", "香水"),
    "食品": ("零食", "饮料", "生鲜"),
    "家居": ("厨具", "收纳", "床品"),
    "数码": ("手机配件", "耳机", "智能设备"),
}
BRANDS = ("合成品牌A", "合成品牌B", "合成品牌C", "合成品牌D", "合成品牌E")

# 订单状态与占比：覆盖支付、发货、取消、退款全链路
STATUS_WEIGHTS = (
    ("已完成", 0.62),
    ("已发货", 0.17),
    ("已支付", 0.06),
    ("待支付", 0.04),
    ("已取消", 0.05),
    ("已退款", 0.04),
    ("退款中", 0.02),
)
PAID_STATUSES = ("已完成", "已发货", "已支付", "已退款", "退款中")
REFUNDED_STATUSES = ("已退款", "退款中")

# 促销日：用于制造趋势尖峰，便于「日销趋势」套路识别异常
PROMO_DATES = (date(2026, 1, 20), date(2026, 2, 14), date(2026, 3, 8))

# 脏数据比例：测试单 / 刷单 / 0 元单 / 缺失值 / 异常金额
TEST_ORDER_RATE = 0.02
FAKE_ORDER_RATE = 0.012
ZERO_AMOUNT_ORDERS = 15
MISSING_DISCOUNT_RATE = 0.02
MISSING_CHANNEL_RATE = 0.01
MISSING_SHIP_TIME_RATE = 0.01
MISSING_AGE_RATE = 0.03
ABNORMAL_AMOUNTS = (8888.88, 9999.99)

# 渠道访客基数（人次/天）：与订单量同量级，避免流量表与订单表差一个数量级
CHANNEL_VISITS = {
    "线下门店": (55, 85),
    "小程序": (95, 140),
    "直播": (60, 110),
    "线上商城": (180, 240),
}
CART_RATES = {"线下门店": 0.10, "线上商城": 0.16, "小程序": 0.14, "直播": 0.20}

ORDER_FIELDS = (
    "订单号",
    "用户ID",
    "订单日期",
    "支付时间",
    "发货时间",
    "订单状态",
    "渠道",
    "地区",
    "商品金额",
    "运费",
    "优惠金额",
    "订单金额",
    "支付金额",
    "退款金额",
    "是否退款",
    "新客",
    "是否测试单",
    "是否刷单",
    "活动ID",
)
ITEM_FIELDS = (
    "订单号",
    "明细行号",
    "商品ID",
    "数量",
    "单价",
    "明细金额",
    "优惠分摊",
    "是否赠品",
)
USER_FIELDS = (
    "用户ID",
    "注册日期",
    "注册渠道",
    "地区",
    "会员等级",
    "性别",
    "年龄",
    "手机号",
    "是否内部账号",
    "是否测试账号",
)
PRODUCT_FIELDS = (
    "商品ID",
    "商品名称",
    "品类",
    "子品类",
    "品牌",
    "单价",
    "成本价",
    "上架日期",
    "是否在售",
)
TRAFFIC_FIELDS = (
    "日期",
    "渠道",
    "实验组",
    "曝光人数",
    "访问人数",
    "加购人数",
    "下单人数",
    "支付人数",
)


def _money(value: float) -> str:
    """把金额格式化为两位小数字符串，统一金额精度。"""
    return f"{value:.2f}"


def _date_text(value: date) -> str:
    """把日期格式化为 YYYY-MM-DD，与 schema.md 的日期类型一致。"""
    return value.strftime("%Y-%m-%d")


def _datetime_text(value: datetime | None) -> str:
    """把时间格式化为 YYYY-MM-DD HH:MM:SS；None 输出空串表示未发生。"""
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def _all_dates() -> list[date]:
    """返回分析区间内的全部自然日（含首尾）。"""
    days: list[date] = []
    current = START_DATE
    while current <= END_DATE:
        days.append(current)
        current += timedelta(days=1)
    return days


def _date_weights(days: list[date]) -> list[float]:
    """给每个日期生成抽样权重：周末与促销日更高，并带轻微上升趋势。"""
    weights: list[float] = []
    for index, day in enumerate(days):
        weight = 1.0
        if day.weekday() >= 5:
            weight *= 1.25
        if day in PROMO_DATES:
            weight *= 1.6
        # 90 天里从 1.0 缓慢升到 1.15，模拟自然增长，不引入真实业务假设
        weight *= 1.0 + 0.15 * index / max(len(days) - 1, 1)
        weights.append(weight)
    return weights


def generate_users(rng: random.Random) -> list[dict]:
    """生成用户表：注册日期、渠道、地区、会员等级与内部 / 测试账号标记。

    Args:
        rng: 已固定种子的随机数生成器。

    Returns:
        用户字典列表，字段与 USER_FIELDS 一致。
    """
    users: list[dict] = []
    for index in range(N_USERS):
        user_id = f"U{index + 1:06d}"
        # 前 8 个为内部账号、接着 6 个为测试账号，其余为正常用户
        is_internal = "是" if index < 8 else "否"
        is_test = "是" if 8 <= index < 14 else "否"
        # 注册日期不晚于 2026-03-15，保证仍有下单窗口
        reg_offset = rng.randint(0, (date(2026, 3, 15) - date(2025, 6, 1)).days)
        reg_date = date(2025, 6, 1) + timedelta(days=reg_offset)
        age: int | str = "" if rng.random() < MISSING_AGE_RATE else rng.randint(18, 65)
        # 手机号是脱敏假值，后 4 位来自序号，确保不指向真实号码
        phone = f"138****{index + 1:04d}"
        users.append(
            {
                "用户ID": user_id,
                "注册日期": _date_text(reg_date),
                "注册渠道": rng.choice(CHANNELS),
                "地区": rng.choice(REGIONS),
                "会员等级": rng.choices(
                    ("普通", "银卡", "金卡", "黑卡"), weights=(0.68, 0.20, 0.09, 0.03)
                )[0],
                "性别": rng.choices(("男", "女", "未知"), weights=(0.48, 0.47, 0.05))[0],
                "年龄": age,
                "手机号": phone,
                "是否内部账号": is_internal,
                "是否测试账号": is_test,
            }
        )
    return users


def generate_products(rng: random.Random) -> list[dict]:
    """生成商品表：合成名称、品类、品牌、标准单价与成本。

    Args:
        rng: 已固定种子的随机数生成器。

    Returns:
        商品字典列表，字段与 PRODUCT_FIELDS 一致。
    """
    products: list[dict] = []
    for index in range(N_PRODUCTS):
        category = CATEGORIES[index % len(CATEGORIES)]
        low, high = PRICE_RANGES[category]
        price = round(rng.uniform(low, high), 2)
        cost = round(price * rng.uniform(0.30, 0.60), 2)
        # 上架日期只用于判断「在售时长」，不参与口径计算
        listed = date(2025, 1, 1) + timedelta(days=rng.randint(0, 424))
        products.append(
            {
                "商品ID": f"P{index + 1:03d}",
                "商品名称": f"合成商品-{index + 1:03d}",
                "品类": category,
                "子品类": rng.choice(SUBCATEGORIES[category]),
                "品牌": rng.choice(BRANDS),
                "单价": _money(price),
                "成本价": _money(cost),
                "上架日期": _date_text(listed),
                "是否在售": "是" if rng.random() < 0.92 else "否",
            }
        )
    return products


def _pick_user(rng: random.Random, users: list[dict]) -> dict:
    """按 96% 正常 / 2% 内部 / 2% 测试的比例选下单用户。"""
    value = rng.random()
    if value < 0.02:
        return users[rng.randrange(0, 8)]
    if value < 0.04:
        return users[rng.randrange(8, 14)]
    return users[rng.randrange(14, len(users))]


def _pick_product(rng: random.Random, products: list[dict]) -> dict:
    """按品类热度加权选品，再在该品类内随机选一个商品。"""
    category = rng.choices(CATEGORIES, weights=CATEGORY_WEIGHTS)[0]
    candidates = [product for product in products if product["品类"] == category]
    return rng.choice(candidates)


def generate_orders(
    rng: random.Random, users: list[dict], products: list[dict]
) -> tuple[list[dict], list[dict]]:
    """生成订单与订单明细，并回填新客标记。

    Args:
        rng: 已固定种子的随机数生成器。
        users: 用户表记录。
        products: 商品表记录。

    Returns:
        (订单列表, 订单明细列表)，字段与 ORDER_FIELDS / ITEM_FIELDS 一致。
    """
    days = _all_dates()
    weights = _date_weights(days)
    orders: list[dict] = []
    items: list[dict] = []

    for index in range(N_ORDERS):
        user = _pick_user(rng, users)
        # 下单日不得早于注册日：保证用户生命周期自洽
        reg_date = datetime.strptime(user["注册日期"], "%Y-%m-%d").date()
        allowed = [(day, weight) for day, weight in zip(days, weights) if day >= reg_date]
        order_date, _ = rng.choices(allowed, weights=[w for _, w in allowed])[0]

        channel = user["注册渠道"] if rng.random() < 0.55 else rng.choice(CHANNELS)
        if rng.random() < MISSING_CHANNEL_RATE:
            channel = ""

        order_id = f"SO{order_date:%Y%m%d}{index:06d}"
        item_count = rng.choices((1, 2, 3, 4), weights=(0.45, 0.30, 0.17, 0.08))[0]

        goods_amount = 0.0
        order_items: list[dict] = []
        for line_no in range(1, item_count + 1):
            product = _pick_product(rng, products)
            is_gift = rng.random() < 0.03
            quantity = 1 if is_gift else rng.choices((1, 2, 3), weights=(0.72, 0.20, 0.08))[0]
            unit_price = 0.0 if is_gift else float(product["单价"])
            line_amount = round(unit_price * quantity, 2)
            goods_amount += line_amount
            order_items.append(
                {
                    "订单号": order_id,
                    "明细行号": line_no,
                    "商品ID": product["商品ID"],
                    "数量": quantity,
                    "单价": _money(unit_price),
                    "明细金额": _money(line_amount),
                    "优惠分摊": _money(0.0),
                    "是否赠品": "是" if is_gift else "否",
                }
            )

        shipping = 0.0 if channel == "线下门店" or goods_amount >= 99 else 10.0
        discount_missing = rng.random() < MISSING_DISCOUNT_RATE
        discount = 0.0
        if not discount_missing and goods_amount > 0:
            discount = float(rng.choice((0, 0, 5, 10, 20, 30)))
            discount = min(discount, goods_amount + shipping)
        payable = round(goods_amount + shipping - discount, 2)

        # 0 元单：用整单优惠把应付金额压到 0，属于需要单独口径的边界数据
        zero_amount = index < ZERO_AMOUNT_ORDERS
        if zero_amount:
            discount = goods_amount + shipping
            payable = 0.0

        status = rng.choices([s for s, _ in STATUS_WEIGHTS], weights=[w for _, w in STATUS_WEIGHTS])[0]
        paid = status in PAID_STATUSES
        paid_amount = payable if paid else 0.0

        pay_time: datetime | None = None
        if paid:
            pay_time = datetime.combine(
                order_date, time(rng.randint(8, 22), rng.randint(0, 59), rng.randint(0, 59))
            )
        ship_time: datetime | None = None
        if status in ("已完成", "已发货") and pay_time is not None:
            if rng.random() >= MISSING_SHIP_TIME_RATE:
                ship_time = pay_time + timedelta(
                    days=rng.randint(1, 3),
                    hours=rng.randint(9, 18),
                )

        refund_amount = paid_amount if status == "已退款" else 0.0
        is_refund = "是" if status in REFUNDED_STATUSES else "否"

        is_test = "是" if user["是否测试账号"] == "是" or user["是否内部账号"] == "是" else "否"
        if is_test == "否" and rng.random() < TEST_ORDER_RATE:
            is_test = "是"
        is_fake = "否"
        if is_test == "否" and rng.random() < FAKE_ORDER_RATE:
            is_fake = "是"

        # 异常金额：只保留、不删除，用于演示「异常值只报告」的处理原则
        if index in (N_ORDERS - 2, N_ORDERS - 1):
            amount = ABNORMAL_AMOUNTS[index % len(ABNORMAL_AMOUNTS)]
            goods_amount = amount
            shipping = 0.0
            discount = 0.0
            payable = amount
            paid_amount = amount
            status = "已完成"
            paid = True

        orders.append(
            {
                "订单号": order_id,
                "用户ID": user["用户ID"],
                "订单日期": _date_text(order_date),
                "支付时间": _datetime_text(pay_time),
                "发货时间": _datetime_text(ship_time),
                "订单状态": status,
                "渠道": channel,
                "地区": user["地区"],
                "商品金额": _money(goods_amount),
                "运费": _money(shipping),
                "优惠金额": "" if discount_missing else _money(discount),
                "订单金额": _money(payable),
                "支付金额": _money(paid_amount),
                "退款金额": _money(refund_amount),
                "是否退款": is_refund,
                "新客": "否",
                "是否测试单": is_test,
                "是否刷单": is_fake,
                "活动ID": rng.choice(("新客券", "满减券", "直播专享", "会员日"))
                if rng.random() < 0.30
                else "",
            }
        )
        items.extend(order_items)

    _mark_new_customers(orders, users)
    return orders, items


def _mark_new_customers(orders: list[dict], users: list[dict]) -> None:
    """按「用户首笔干净支付订单」回填新客标记，直接修改 orders。

    Args:
        orders: 订单列表（原地修改）。
        users: 用户表，用于识别内部 / 测试账号。

    Returns:
        None。正常用户的首笔干净支付订单标记为「是」，其余为「否」。
    """
    dirty_users = {
        user["用户ID"]
        for user in users
        if user["是否内部账号"] == "是" or user["是否测试账号"] == "是"
    }
    first_order: dict[str, tuple[str, str]] = {}
    for order in orders:
        if order["是否测试单"] == "是" or order["是否刷单"] == "是":
            continue
        if order["用户ID"] in dirty_users or order["订单状态"] not in PAID_STATUSES:
            continue
        key = (order["订单日期"], order["订单号"])
        current = first_order.get(order["用户ID"])
        if current is None or key < current:
            first_order[order["用户ID"]] = key
    for order in orders:
        if first_order.get(order["用户ID"]) == (order["订单日期"], order["订单号"]):
            order["新客"] = "是"


def _funnel_row(
    rng: random.Random, day: date, channel: str, group: str, visits: int, pay_uplift: float
) -> dict:
    """生成一条流量记录：曝光 → 访问 → 加购 → 下单 → 支付保持单调递减。"""
    exposure = int(visits * rng.uniform(1.5, 2.2))
    cart = int(visits * CART_RATES[channel] * rng.uniform(0.85, 1.15))
    order = int(cart * rng.uniform(0.45, 0.60))
    pay = int(order * rng.uniform(0.58, 0.72) * pay_uplift)
    pay = min(pay, order)
    return {
        "日期": _date_text(day),
        "渠道": channel,
        "实验组": group,
        "曝光人数": max(exposure, visits),
        "访问人数": visits,
        "加购人数": max(cart, 0),
        "下单人数": max(order, pay),
        "支付人数": max(pay, 0),
    }


def generate_traffic(rng: random.Random) -> list[dict]:
    """生成流量表：线下 / 小程序 / 直播为未分流，线上商城拆实验组与对照组。

    Args:
        rng: 已固定种子的随机数生成器。

    Returns:
        流量字典列表，字段与 TRAFFIC_FIELDS 一致。
    """
    rows: list[dict] = []
    for day in _all_dates():
        promo_uplift = 1.6 if day in PROMO_DATES else 1.0
        for channel in ("线下门店", "小程序", "直播"):
            low, high = CHANNEL_VISITS[channel]
            visits = int(rng.randint(low, high) * promo_uplift)
            rows.append(_funnel_row(rng, day, channel, "未分流", visits, 1.0))

        low, high = CHANNEL_VISITS["线上商城"]
        total = int(rng.randint(low, high) * promo_uplift)
        control = total // 2
        treat = total - control
        # 实验组支付转化提升 15%，用于演示两比例 z 检验与置信区间
        rows.append(_funnel_row(rng, day, "线上商城", "对照组", control, 1.0))
        rows.append(_funnel_row(rng, day, "线上商城", "实验组", treat, 1.15))
    return rows


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    """按固定字段顺序写 CSV；编码 utf-8-sig，缺失键输出空串。"""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), restval="")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """生成五张表并打印行数，便于人工核对数据规模。"""
    rng = random.Random(SEED)
    users = generate_users(rng)
    products = generate_products(rng)
    orders, items = generate_orders(rng, users, products)
    traffic = generate_traffic(rng)

    _write_csv(OUT_DIR / "users.csv", USER_FIELDS, users)
    _write_csv(OUT_DIR / "products.csv", PRODUCT_FIELDS, products)
    _write_csv(OUT_DIR / "orders.csv", ORDER_FIELDS, orders)
    _write_csv(OUT_DIR / "order_items.csv", ITEM_FIELDS, items)
    _write_csv(OUT_DIR / "traffic.csv", TRAFFIC_FIELDS, traffic)

    print(f"users.csv       {len(users)} 行")
    print(f"products.csv    {len(products)} 行")
    print(f"orders.csv      {len(orders)} 行")
    print(f"order_items.csv {len(items)} 行")
    print(f"traffic.csv     {len(traffic)} 行")


if __name__ == "__main__":
    main()
