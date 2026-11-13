"""电商领域包自检:文档可解析、引用一致、数据列名一致、参考值可复现。

检查项:
    1. metrics.yaml 能被 PyYAML 解析,10 个指标字段齐全,draft 指标都有 open_questions;
    2. eval/cases.json 能被 json 解析,case 格式与根目录 eval/cases.json 一致;
    3. playbooks.md 与 eval/cases.json 引用的指标 id 都存在于 metrics.yaml;
    4. required_columns 引用的表与列都存在于 schema.md 定义的字段契约;
    5. 五张 CSV 能被 pandas 读入,列名与 schema.md 完全一致;
    6. eval/cases.json 的 tolerance 参考值能从 CSV 重新计算并落在容差内。

用法:
    python domains/ecommerce/check_package.py

返回码:
    0 = 全部通过;1 = 存在失败项。
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

BASE_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = BASE_DIR / "examples"
METRICS_PATH = BASE_DIR / "metrics.yaml"
CASES_PATH = BASE_DIR / "eval" / "cases.json"
PLAYBOOKS_PATH = BASE_DIR / "playbooks.md"
SCHEMA_PATH = BASE_DIR / "schema.md"

# 与 schema.md 保持一致的字段契约;改字段时必须同步改这里
EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "orders": (
        "订单号", "用户ID", "订单日期", "支付时间", "发货时间", "订单状态", "渠道", "地区",
        "商品金额", "运费", "优惠金额", "订单金额", "支付金额", "退款金额", "是否退款",
        "新客", "是否测试单", "是否刷单", "活动ID",
    ),
    "order_items": (
        "订单号", "明细行号", "商品ID", "数量", "单价", "明细金额", "优惠分摊", "是否赠品",
    ),
    "users": (
        "用户ID", "注册日期", "注册渠道", "地区", "会员等级", "性别", "年龄", "手机号",
        "是否内部账号", "是否测试账号",
    ),
    "products": (
        "商品ID", "商品名称", "品类", "子品类", "品牌", "单价", "成本价", "上架日期", "是否在售",
    ),
    "traffic": (
        "日期", "渠道", "实验组", "曝光人数", "访问人数", "加购人数", "下单人数", "支付人数",
    ),
}

# 根目录 eval/run_eval.py 支持的 checks 键;领域用例不得引入新的 checks 键
SUPPORTED_CHECKS = {
    "final_contains", "final_contains_any", "final_not_contains", "final_not_empty",
    "error_contains", "charts_min", "messages_contains", "max_duration_ms",
    "report_sections", "summary_contains", "summary_not_contains",
}

REQUIRED_METRIC_FIELDS = (
    "id", "name", "category", "formula", "required_columns", "filters",
    "dimensions", "time_window", "edge_cases", "status",
)

PAID_STATUSES = ("已完成", "已发货", "已支付", "已退款", "退款中")
DIRTY_USER_COLUMNS = ("是否内部账号", "是否测试账号")
AB_CHANNEL = "线上商城"
AB_GROUPS = ("对照组", "实验组")


def load_metrics() -> tuple[dict, list[str]]:
    """读取 metrics.yaml 并校验字段完整性。

    Returns:
        (指标文档, 失败原因列表)。
    """
    failures: list[str] = []
    document = yaml.safe_load(METRICS_PATH.read_text(encoding="utf-8"))
    metrics = document.get("metrics") if isinstance(document, dict) else None
    if not isinstance(metrics, list) or not metrics:
        return document, ["metrics.yaml 缺少非空的 metrics 列表"]

    ids: list[str] = []
    for index, metric in enumerate(metrics, 1):
        for field in REQUIRED_METRIC_FIELDS:
            if field not in metric:
                failures.append(f"第 {index} 个指标缺少字段 {field}")
        metric_id = metric.get("id")
        if not metric_id:
            failures.append(f"第 {index} 个指标缺少 id")
            continue
        if metric_id in ids:
            failures.append(f"指标 id 重复: {metric_id}")
        ids.append(metric_id)
        if metric.get("status") not in ("draft", "confirmed"):
            failures.append(f"{metric_id} 的 status 只能是 draft / confirmed")
        if metric.get("status") == "draft":
            questions = metric.get("open_questions") or []
            if len(questions) < 1:
                failures.append(f"{metric_id} 为 draft 但没有 open_questions")
            for question in questions:
                if len(question.get("options") or []) < 2:
                    failures.append(f"{metric_id} 的 open_questions 选项少于 2 个")
    return document, failures


def load_cases(metric_ids: set[str]) -> tuple[list[dict], list[str]]:
    """读取 eval/cases.json,校验 case 格式与指标引用。

    Returns:
        (用例列表, 失败原因列表)。
    """
    failures: list[str] = []
    document = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(cases, list) or not cases:
        return [], ["eval/cases.json 缺少非空的 cases 列表"]

    for case in cases:
        case_id = case.get("id", "(无 id)")
        for field in ("question", "checks", "metrics"):
            if field not in case:
                failures.append(f"{case_id} 缺少字段 {field}")
        unknown = [key for key in (case.get("checks") or {}) if key not in SUPPORTED_CHECKS]
        if unknown:
            failures.append(f"{case_id} 使用不支持的 checks 键: {unknown}")
        for metric_id in case.get("metrics", []):
            if metric_id not in metric_ids:
                failures.append(f"{case_id} 引用了不存在的指标 id: {metric_id}")
    return cases, failures


def check_playbook_refs(metric_ids: set[str]) -> list[str]:
    """检查 playbooks.md 中反引号包裹的纯英文标识符都是合法指标 id。

    为什么限定纯英文:反引号里的表名 / 列名带点号或中文,会被自动跳过,
    避免把字段名误判为指标 id。
    """
    failures: list[str] = []
    text = PLAYBOOKS_PATH.read_text(encoding="utf-8")
    tokens = re.findall(r"`([A-Za-z_]+)`", text)
    referenced = {token for token in tokens if token}
    for token in sorted(referenced):
        if token not in metric_ids:
            failures.append(f"playbooks.md 引用了不存在的指标 id: {token}")
    missing = metric_ids - referenced
    if missing:
        failures.append(f"playbooks.md 未引用这些指标 id: {sorted(missing)}")
    return failures


def check_required_columns(metrics: list[dict]) -> list[str]:
    """检查 required_columns 里的「表.字段」都存在于字段契约中。"""
    failures: list[str] = []
    for metric in metrics:
        for item in metric.get("required_columns", []):
            if "." not in item:
                failures.append(f"{metric.get('id')} 的 required_columns 格式错误: {item}")
                continue
            table, column = item.split(".", 1)
            if table not in EXPECTED_COLUMNS:
                failures.append(f"{metric.get('id')} 引用了不存在的表: {table}")
            elif column not in EXPECTED_COLUMNS[table]:
                failures.append(f"{metric.get('id')} 引用了不存在的列: {item}")
    return failures


def load_frames() -> tuple[dict[str, pd.DataFrame], list[str]]:
    """读取五张 CSV,校验列名与行数。

    Returns:
        (表名到 DataFrame 的映射, 失败原因列表)。
    """
    failures: list[str] = []
    frames: dict[str, pd.DataFrame] = {}
    for table, expected in EXPECTED_COLUMNS.items():
        path = EXAMPLES_DIR / f"{table}.csv"
        if not path.is_file():
            failures.append(f"缺少数据文件: {path.name}")
            continue
        frame = pd.read_csv(path, encoding="utf-8-sig")
        frames[table] = frame
        actual = tuple(frame.columns.tolist())
        if actual != expected:
            failures.append(f"{path.name} 列名与 schema.md 不一致: 实际 {actual}")
        if frame.empty:
            failures.append(f"{path.name} 没有数据行")
    return frames, failures


def compute_reference_metrics(frames: dict[str, pd.DataFrame]) -> dict[str, float]:
    """按 metrics.yaml 的干净口径重算关键指标,用于校验用例容差。"""
    orders = frames["orders"].copy()
    users = frames["users"]
    items = frames["order_items"]
    traffic = frames["traffic"]

    dirty = (
        users.set_index("用户ID")["是否内部账号"].eq("是")
        | users.set_index("用户ID")["是否测试账号"].eq("是")
    )
    paid = orders["订单状态"].isin(PAID_STATUSES)
    clean = (
        orders["是否测试单"].eq("否")
        & orders["是否刷单"].eq("否")
        & ~orders["用户ID"].map(dirty).fillna(False)
        & paid
    )
    clean_orders = orders.loc[clean]

    gmv = float(clean_orders["支付金额"].sum())
    order_count = int(len(clean_orders))
    aov = gmv / order_count if order_count else float("nan")
    refund_rate = float(clean_orders["是否退款"].eq("是").mean())
    new_customer_ratio = float(clean_orders["新客"].eq("是").mean())

    per_user = clean_orders.groupby("用户ID")["订单号"].nunique()
    repurchase_rate = float((per_user >= 2).sum() / max((per_user >= 1).sum(), 1))

    clean_ids = set(clean_orders["订单号"])
    attach_items = items[items["订单号"].isin(clean_ids) & items["是否赠品"].eq("否")]
    attach_rate = float(attach_items["数量"].sum() / order_count)

    pay_conversion_rate = float(traffic["支付人数"].sum() / traffic["访问人数"].sum())
    cart_add_rate = float(traffic["加购人数"].sum() / traffic["访问人数"].sum())

    ab = traffic[traffic["渠道"].eq(AB_CHANNEL)].groupby("实验组")[["访问人数", "支付人数"]].sum()
    n1, x1 = ab.loc[AB_GROUPS[0], "访问人数"], ab.loc[AB_GROUPS[0], "支付人数"]
    n2, x2 = ab.loc[AB_GROUPS[1], "访问人数"], ab.loc[AB_GROUPS[1], "支付人数"]
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    z_value = (p2 - p1) / math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    ab_p_value = math.erfc(abs(z_value) / math.sqrt(2))
    ab_relative_lift = (p2 - p1) / p1

    return {
        "gmv": gmv,
        "order_count": float(order_count),
        "aov": aov,
        "refund_rate": refund_rate,
        "repurchase_rate": repurchase_rate,
        "new_customer_ratio": new_customer_ratio,
        "attach_rate": attach_rate,
        "pay_conversion_rate": pay_conversion_rate,
        "cart_add_rate": cart_add_rate,
        "ab_relative_lift": float(ab_relative_lift),
        "ab_p_value": float(ab_p_value),
    }


def check_tolerances(cases: list[dict], computed: dict[str, float]) -> list[str]:
    """校验用例的 tolerance 参考值与从 CSV 重算的结果在相对容差内。"""
    failures: list[str] = []
    for case in cases:
        tolerance = case.get("tolerance")
        if not tolerance:
            continue
        metric = tolerance.get("metric")
        expected = tolerance.get("value")
        rel_tol = tolerance.get("rel_tol")
        if metric not in computed:
            failures.append(f"{case.get('id')} 的 tolerance 指标无法计算: {metric}")
            continue
        if not isinstance(expected, (int, float)) or not isinstance(rel_tol, (int, float)):
            failures.append(f"{case.get('id')} 的 tolerance 缺少 value / rel_tol")
            continue
        actual = computed[metric]
        if expected == 0:
            if abs(actual) > rel_tol:
                failures.append(f"{case.get('id')} 参考值 {metric} 偏差过大: {actual}")
        elif abs(actual - expected) / abs(expected) > rel_tol:
            failures.append(
                f"{case.get('id')} 参考值 {metric} 偏差过大: "
                f"期望 {expected}, 实际 {actual:.6f}, 容差 {rel_tol}"
            )
    return failures


def main() -> int:
    """执行全部自检并打印结果,返回退出码。"""
    failures: list[str] = []

    metrics_document, metric_failures = load_metrics()
    failures.extend(metric_failures)
    metrics = metrics_document.get("metrics", []) if isinstance(metrics_document, dict) else []
    metric_ids = {metric.get("id") for metric in metrics}

    cases, case_failures = load_cases(metric_ids)
    failures.extend(case_failures)
    failures.extend(check_playbook_refs(metric_ids))
    failures.extend(check_required_columns(metrics))

    frames, frame_failures = load_frames()
    failures.extend(frame_failures)

    computed: dict[str, float] = {}
    if not frame_failures:
        computed = compute_reference_metrics(frames)
        failures.extend(check_tolerances(cases, computed))

    print("[自检] metrics.yaml / eval/cases.json 可解析")
    print(f"[自检] 指标数 {len(metrics)},用例数 {len(cases)}")
    if computed:
        print(
            "[自检] 参考值: "
            f"GMV={computed['gmv']:.2f}, 订单数={int(computed['order_count'])}, "
            f"客单价={computed['aov']:.2f}, 退款率={computed['refund_rate']:.4f}, "
            f"复购率={computed['repurchase_rate']:.4f}, "
            f"新客占比={computed['new_customer_ratio']:.4f}, "
            f"连带率={computed['attach_rate']:.4f}, "
            f"支付转化率={computed['pay_conversion_rate']:.6f}, "
            f"AB 相对提升={computed['ab_relative_lift']:.4f}, "
            f"AB p 值={computed['ab_p_value']:.6f}"
        )

    if failures:
        print("\n[自检] 失败项:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\n[自检] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
