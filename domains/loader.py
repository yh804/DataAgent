"""领域包加载器：按名称读 domains/<name>/ 下的文档，并提供指标选择与 prompt 块拼接。

为什么做成独立 loader：
    - 把「读磁盘 + 拼字符串 + 启发式选指标」的逻辑集中在一个模块,
      节点里只剩 if-enabled: 调用;不把 yaml 解析和领域词塞进 prompt 模块。
    - 任何节点都能复用同一个 loader,不会在 planner / code_analyst / summarize
      各写一份重复读 metrics.yaml 的代码。

红线：
    - 不读 spec.md 全文进 prompt；最多 800 字符 planner 块 / 1200 字符 metrics 块。
    - metrics.yaml 必须存在,缺失时返回 None 并打 warning,不抛异常。
    - 进程内缓存按文件 mtime 失效;改了 yaml/schema/playbooks 后下一次加载自动刷新。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("domain_loader")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 指标关键词到 category 的映射:用户问题里出现这类词时,promote 对应 category 的指标
CATEGORY_KEYWORDS = {
    "规模": ("规模",),
    "增长": ("规模", "留存"),
    "GMV": ("规模",),
    "销售额": ("规模",),
    "订单": ("规模",),
    "渠道": ("转化",),
    "转化": ("转化",),
    "支付": ("转化",),
    "漏斗": ("转化",),
    "复购": ("留存",),
    "留存": ("留存",),
    "新客": ("结构",),
    "老客": ("结构",),
    "退款": ("质量",),
    "质量": ("质量",),
    "价格": ("效率",),
    "客单价": ("效率",),
}

METRIC_KEYWORDS = {
    "gmv": ("gmv", "GMV", "销售额", "销售额趋势"),
    "order_count": ("订单数", "订单量", "订单个数"),
    "aov": ("客单价", "AOV", "平均订单金额"),
    "pay_conversion_rate": ("支付转化率", "支付转化", "支付率"),
    "refund_rate": ("退款率", "退款", "退货"),
    "repurchase_rate": ("复购率", "复购比例"),
    "repurchase_interval": ("复购间隔", "复购周期", "多久复购"),
    "new_customer_ratio": ("新客占比", "新客比例", "新客占", "新老客"),
    "attach_rate": ("连带率", "连带", "件单比"),
    "cart_add_rate": ("加购率", "加购转化率"),
}

PLANNER_SIZE_BUDGET = 800
METRIC_SIZE_BUDGET = 1200
DOMAIN_KEYWORD = {
    "GMV", "gmv", "客单价", "AOV", "复购", "退款", "新客", "连带率",
    "转化率", "漏斗", "价格带", "RFM", "cohort", "Cohort", "AB 实验",
    "AB实验", "AB",
}


def _safe_import_yaml():
    """延迟导入 yaml,避免模块装不上时在 import 阶段就炸。"""
    try:
        import yaml  # 已在 PyYAML 里,项目依赖已有
    except ImportError as exc:  # 这路径不会命中,但防御性兜一下
        raise RuntimeError("PyYAML 未安装,无法读取 metrics.yaml") from exc
    return yaml


# ---------- 开关与白名单 ----------

def domain_enabled() -> bool:
    """读 DOMAIN_PACK 环境变量,做白名单校验,返回是否启用。

    规则:
        - 空字符串 / 未设置 / 未知值 → False,不启用任何领域包;
        - 必须是 domains/<name>/ 真实目录,且包含 metrics.yaml;
        - 非法字符(../ 等)直接判 False,不做父目录遍历。
    """
    raw = os.getenv("DOMAIN_PACK", "").strip()
    if not raw:
        return False
    if "/" in raw or "\\" in raw or ".." in raw:
        logger.warning("DOMAIN_PACK 值非法: %s", raw)
        return False
    pack_dir = PROJECT_ROOT / "domains" / raw
    if not pack_dir.is_dir():
        logger.warning("DOMAIN_PACK 目录不存在: %s", pack_dir)
        return False
    if not (pack_dir / "metrics.yaml").is_file():
        logger.warning("DOMAIN_PACK 目录缺少 metrics.yaml: %s", pack_dir)
        return False
    return True


def current_pack_name() -> str:
    """当前启用的包名;未启用时返回空串。"""
    return os.getenv("DOMAIN_PACK", "").strip() if domain_enabled() else ""


# ---------- 读盘 + 缓存 ----------

_cache: dict[str, tuple[float, dict]] = {}


def _load_markdown_text(path: Path) -> str:
    """安全读取 md 文件,不存在返回空串。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_domain_pack(name: str = "") -> dict | None:
    """读 domains/<name>/ 下的 spec.md / metrics.yaml / playbooks.md / schema.md。

    Args:
        name: 包名;空串时用 DOMAIN_PACK 环境变量;非法/不存在返回 None。

    Returns:
        {"name", "path", "spec_text", "metrics": [...], "playbooks_text", "schema_text"}
        或 None。metrics 必须存在且能被 yaml 解析,否则返回 None 并打 warning。
    """
    if not name:
        name = os.getenv("DOMAIN_PACK", "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or ".." in name:
        logger.warning("load_domain_pack: 非法包名 %r", name)
        return None

    pack_dir = PROJECT_ROOT / "domains" / name
    if not pack_dir.is_dir():
        logger.warning("load_domain_pack: 目录不存在 %s", pack_dir)
        return None

    metrics_path = pack_dir / "metrics.yaml"
    if not metrics_path.is_file():
        logger.warning("load_domain_pack: metrics.yaml 缺失 %s", metrics_path)
        return None

    # 进程内缓存:按目录 + metrics.yaml + schema.md 的 mtime 失效
    key = str(pack_dir)
    mtime_parts = [pack_dir.stat().st_mtime, metrics_path.stat().st_mtime]
    schema_path = pack_dir / "schema.md"
    if schema_path.is_file():
        mtime_parts.append(schema_path.stat().st_mtime)
    playbooks_path = pack_dir / "playbooks.md"
    if playbooks_path.is_file():
        mtime_parts.append(playbooks_path.stat().st_mtime)
    cache_key = hash(tuple(mtime_parts))
    cached = _cache.get(key)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    yaml = _safe_import_yaml()
    try:
        doc = yaml.safe_load(metrics_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都判不可用
        logger.warning("load_domain_pack: metrics.yaml 解析失败 %s: %s", metrics_path, exc)
        return None

    metrics_list = doc.get("metrics", []) if isinstance(doc, dict) else []
    if not metrics_list:
        logger.warning("load_domain_pack: metrics.yaml 没有 metrics 列表 %s", metrics_path)
        return None

    pack = {
        "name": name,
        "path": str(pack_dir),
        "spec_text": _load_markdown_text(pack_dir / "spec.md"),
        "metrics": metrics_list,
        "playbooks_text": _load_markdown_text(pack_dir / "playbooks.md"),
        "schema_text": _load_markdown_text(pack_dir / "schema.md"),
    }
    _cache[key] = (cache_key, pack)
    return pack


# ---------- 指标选择 ----------

def _tokenize(text: str) -> set[str]:
    """简化分词:小写 + 去标点,便于 in 判断。"""
    lowered = text.lower()
    # 中英文字符 + 数字都保留,其它替换为空格
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", lowered, flags=re.UNICODE)
    return {token for token in cleaned.split() if token}


def _metric_match_score(metric: dict, question: str, plan: list[str] | None) -> int:
    """给单个指标打分:字面命中 id/name 得高分,category 关键词得中分。"""
    score = 0
    text = f"{question} {' '.join(plan or [])}"
    q_lower = text.lower()

    metric_id = metric.get("id", "")
    metric_name = metric.get("name", "")

    # 直接命中指标名(中文或英文 id):+10
    if metric_name and metric_name in text:
        score += 10
    if metric_id and metric_id.lower() in q_lower:
        score += 10

    # 通过 METRIC_KEYWORDS 里的同义词命中
    for alias in METRIC_KEYWORDS.get(metric_id, ()):
        if alias in text or alias.lower() in q_lower:
            score += 8

    # 通过 category 关键词兜底
    category = metric.get("category", "")
    for keyword, cats in CATEGORY_KEYWORDS.items():
        if category in cats and keyword in text:
            score += 4

    return score


def select_metrics(
    pack: dict | None, question: str, plan: list[str] | None = None, limit: int = 3
) -> list[dict]:
    """从领域包里挑出与问题/计划最相关的指标。

    打分规则:
        - 指标名或英文 id 直接命中 +10;
        - 同义词命中 +8;
        - category 关键词兜底 +4;
        - 按分数降序取前 limit 个;分数相同时按 id 字母序稳定排序。
    分数全 0 时返回空列表(说明问题和领域包完全无关,不在 prompt 里硬塞)。
    """
    if not pack:
        return []
    metrics = pack.get("metrics") or []
    if not metrics:
        return []

    scored = [
        (_metric_match_score(metric, question, plan), metric.get("id", ""), metric)
        for metric in metrics
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    top = [metric for score, _, metric in scored if score > 0][:limit]
    return top


# ---------- prompt 块构造 ----------

def _truncate(text: str, limit: int) -> str:
    """截断字符串到 limit 字符,末尾加 …;空串直接返回。"""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def build_planner_block(pack: dict | None) -> str:
    """生成 planner 节点插入 prompt 的领域上下文块,≤800 字符。

    内容:
        - 一行北极星候选摘要(仅 3 个候选名);
        - Q1–Q3 每问题一行(只写问题标签 + 1 个代表指标 id);
        - 一行 draft 指标 id 列表。
    """
    if not pack:
        return ""
    name = pack.get("name", "") or "unknown"
    metrics = pack.get("metrics") or []

    north_lines: list[str] = [f"启用领域包:{name}"]
    # 北极星候选只写名称,不展开定义
    north_lines.append("北极星候选:净 GMV / 支付用户数(去重) / 复购 GMV 占比(三选一,默认都列)")

    # Q1–Q3:从 spec.md 里抠出三行标题即可,不在 loader 里重复手写 spec 原文
    spec_text = pack.get("spec_text", "")
    q_pattern = re.compile(r"### (Q[123]).*?\n(- .*?\n){1,3}", re.DOTALL)
    q_lines: list[str] = []
    for match in q_pattern.finditer(spec_text):
        header = match.group(1)
        q_lines.append(f"  {header}")
    if q_lines:
        north_lines.append("决策问题:")
        north_lines.extend(q_lines)

    draft_ids = [m.get("id", "") for m in metrics if m.get("status") == "draft" and m.get("id")]
    if draft_ids:
        north_lines.append("以下指标口径待业务确认(draft):" + "、".join(draft_ids))

    block = "【领域上下文】\n" + "\n".join(north_lines)
    return _truncate(block, PLANNER_SIZE_BUDGET)


def build_metric_block(
    pack: dict | None, question: str, plan: list[str] | None = None
) -> str:
    """生成 code_analyst 插入 prompt 的指标口径块,≤1200 字符。

    每个指标一行:「id/名称(status):公式;必排除项;关键 edge_case」。
    不传全量,只传 select_metrics 选中的最多 3 个。
    """
    if not pack:
        return ""
    selected = select_metrics(pack, question, plan, limit=3)
    if not selected:
        return ""

    lines: list[str] = []
    for metric in selected:
        mid = metric.get("id", "")
        mname = metric.get("name", "")
        mstatus = metric.get("status", "draft")
        formula = (metric.get("formula") or "").strip().rstrip("。")
        filters = metric.get("filters") or []
        excludes = "; ".join(f.replace("排除 ", "") for f in filters[:2]) if filters else ""
        edge_cases = metric.get("edge_cases") or []
        edge = (edge_cases[0].strip() if edge_cases else "")
        # 精简字段,保证每行不会太长
        line = f"- {mid}/{mname}({mstatus}):{formula}"
        if excludes:
            line += f"; 必排除:{excludes}"
        if edge:
            edge_clean = edge.split(":", 1)[-1].strip() if ":" in edge else edge
            edge_clean = edge_clean.rstrip("。")
            line += f"; 注意:{edge_clean}"
        lines.append(line)

    # 输出契约放在标题后、指标行前:即使整体被 1200 字符预算截断,
    # 契约也一定保留,指标行才可能被截掉
    contract = (
        "输出契约:必须先执行上述 filters 再套公式(先过滤再计算);"
        "脚本末尾为每个指标 print 一行 METRIC <id>=<数值>(保留 6 位小数);"
        "required_columns 含 users.* 时必须 join users 并按 filters 排除内部/测试账号;"
        "所需表不存在时停止并说明缺什么,禁止自行换口径。"
    )
    block = (
        "【指标口径(来自领域包,必须按此计算)】\n"
        + contract
        + "\n"
        + "\n".join(lines)
    )
    return _truncate(block, METRIC_SIZE_BUDGET)


def build_quality_note(pack: dict | None, selected_metrics: list[dict] | None) -> str:
    """给 summarize 节点的口径待确认提示句;无 draft 指标返回空串。"""
    if not pack:
        return ""
    selected = selected_metrics or []
    draft_ids = [
        m.get("id", "") for m in selected if m.get("status") == "draft" and m.get("id")
    ]
    # 如果选中的全是 confirmed,但包里有 draft 指标,仍然给一条通用提示
    if not draft_ids:
        all_draft = [
            m.get("id", "")
            for m in pack.get("metrics", [])
            if m.get("status") == "draft" and m.get("id")
        ]
        if not all_draft:
            return ""
        return (
            "【领域包提示】本领域存在 draft 口径指标,本次分析未涉及但需留意未来版本变更。"
            "请在报告「局限与数据质量」一节注明。"
        )
    names = []
    for mid in draft_ids:
        mname = next((m.get("name", "") for m in pack.get("metrics", []) if m.get("id") == mid), "")
        names.append(f"{mid}({mname})" if mname else mid)
    return (
        f"【领域包提示】以下指标口径为 draft(待业务确认):{'、'.join(names)}。"
        f"请在报告「口径与数据版本」一节逐条写明本次采用的默认口径。"
    )


def validate_domain_report(report_text: str, selected_metrics: list[dict] | None) -> list[str]:
    """检查报告是否提到 draft 口径待确认;缺失只返回 warning,不阻断。

    Args:
        report_text: summarize 生成的报告文本或 summary.dict() 里拼出来的文本。
        selected_metrics: 本次选中的指标列表;若不含 draft 指标返回空列表。

    Returns:
        警告字符串列表,可能为空。
    """
    selected = selected_metrics or []
    draft_ids = [m.get("id", "") for m in selected if m.get("status") == "draft" and m.get("id")]
    if not draft_ids:
        return []
    text = str(report_text or "")
    warnings: list[str] = []
    # 轻量检查：draft 指标名字 + 「口径」+ 「待确认」至少出现一次
    keywords_found = all(kw in text for kw in ("口径", "待确认")) or "draft" in text.lower()
    if not keywords_found:
        warnings.append(
            "领域包提示:本次分析涉及 draft 口径指标,但报告未提及口径待确认。"
            "建议在「口径与数据版本」一节补充说明。"
        )
    return warnings
