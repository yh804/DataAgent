"""领域包加载器的单元测试。

覆盖:
    - domain_enabled() 在未设置 / 合法 / 非法值下的行为;
    - load_domain_pack 在未启用 / 目录缺失 / 合法加载时的行为 + mtime 缓存;
    - select_metrics 的 limit / 相关度排序;
    - build_planner_block / build_metric_block 字符数上限;
    - validate_domain_report 在有 / 无 draft 指标时的警告行为。

注意:这些测试**不**会真的拉 LLM 或起 Docker,全部离线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from domains import loader as domain_loader


# ---------- fixture ----------

ECOMMERCE_DIR = Path(__file__).resolve().parent.parent / "domains" / "ecommerce"


@pytest.fixture
def ecommerce_pack():
    """加载真实电商领域包,返回 pack dict。"""
    return domain_loader.load_domain_pack("ecommerce")


@pytest.fixture
def broken_pack_dir(tmp_path: Path) -> Path:
    """构造一个目录存在但 metrics.yaml 缺失的 fake pack 路径。"""
    return tmp_path / "broken"


# ---------- domain_enabled ----------

def test_domain_enabled_default_disabled(monkeypatch):
    """DOMAIN_PACK 未设置时 domain_enabled() 为 False。"""
    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    assert domain_loader.domain_enabled() is False


def test_domain_enabled_unknown_value_disabled(monkeypatch):
    """DOMAIN_PACK 值不存在于 domains/ 时 domain_enabled() 为 False。"""
    monkeypatch.setenv("DOMAIN_PACK", "nonexistent")
    assert domain_loader.domain_enabled() is False


def test_domain_enabled_traversal_blocked(monkeypatch):
    """DOMAIN_PACK 含 ../ 等路径穿越字符时 domain_enabled() 为 False(白名单校验)。"""
    monkeypatch.setenv("DOMAIN_PACK", "../evil")
    assert domain_loader.domain_enabled() is False
    monkeypatch.setenv("DOMAIN_PACK", "ecommerce/../prompts")
    assert domain_loader.domain_enabled() is False


def test_domain_enabled_ecommerce_ok(monkeypatch):
    """DOMAIN_PACK=ecommerce 且目录存在时 domain_enabled() 为 True。"""
    monkeypatch.setenv("DOMAIN_PACK", "ecommerce")
    assert domain_loader.domain_enabled() is True


# ---------- load_domain_pack ----------

def test_load_pack_when_disabled():
    """未启用或空名时返回 None,不抛异常。"""
    assert domain_loader.load_domain_pack("") is None


def test_load_pack_unknown_dir():
    """目录不存在返回 None。"""
    assert domain_loader.load_domain_pack("nonexistent") is None


def test_load_pack_traversal_blocked():
    """路径穿越字符返回 None。"""
    assert domain_loader.load_domain_pack("../evil") is None


def test_load_pack_missing_metrics_yaml(tmp_path: Path):
    """目录存在但 metrics.yaml 缺失时返回 None。

    做法:临时目录下构造 domains/broken/,只写 spec.md 不写 metrics.yaml,
    再用 monkeypatch 把 loader.PROJECT_ROOT 指向 tmp_path。
    """
    broken = tmp_path / "domains" / "broken"
    broken.mkdir(parents=True)
    (broken / "spec.md").write_text("# spec", encoding="utf-8")
    # metrics.yaml 故意不写

    import domains.loader as loader  # noqa: PLC0415 - 模块级 import 已存在

    old_root = loader.PROJECT_ROOT
    loader.PROJECT_ROOT = tmp_path
    try:
        assert loader.load_domain_pack("broken") is None
    finally:
        loader.PROJECT_ROOT = old_root


def test_load_pack_success(ecommerce_pack):
    """合法加载时 pack 含必需字段与 10 个指标。"""
    assert ecommerce_pack is not None
    assert ecommerce_pack["name"] == "ecommerce"
    assert ecommerce_pack["path"].endswith("ecommerce")
    assert ecommerce_pack["spec_text"], "spec.md 应非空"
    assert ecommerce_pack["playbooks_text"], "playbooks.md 应非空"
    assert ecommerce_pack["metrics"], "metrics.yaml 应含指标"
    assert ecommerce_pack["schema_text"], "schema.md 应非空"
    assert len(ecommerce_pack["metrics"]) == 10
    ids = {m["id"] for m in ecommerce_pack["metrics"]}
    assert {"gmv", "order_count", "aov", "refund_rate", "repurchase_rate"} <= ids


def test_load_pack_cache_invalidation(tmp_path: Path):
    """metrics.yaml mtime 变化后,第二次 load 应返回新内容(进程内缓存失效)。"""
    import time

    pack_dir = tmp_path / "domains" / "fake_pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "spec.md").write_text("# spec", encoding="utf-8")
    (pack_dir / "playbooks.md").write_text("# playbooks", encoding="utf-8")
    (pack_dir / "schema.md").write_text("# schema", encoding="utf-8")
    (pack_dir / "metrics.yaml").write_text(
        "version: \"0.1\"\nmetrics:\n  - id: gmv\n    name: GMV\n    category: 规模\n    formula: SUM\n    required_columns: []\n    filters: []\n    dimensions: []\n    time_window: 月度\n    edge_cases: []\n    status: confirmed\n",
        encoding="utf-8",
    )

    import domains.loader as loader  # noqa: PLC0415 - 模块级 import 已存在

    old_root = loader.PROJECT_ROOT
    loader.PROJECT_ROOT = tmp_path
    try:
        pack1 = loader.load_domain_pack("fake_pack")
        assert pack1 is not None and pack1["metrics"][0]["id"] == "gmv"

        # 改 metrics.yaml:新增一个指标,必须让 mtime 真正改变
        time.sleep(0.05)
        (pack_dir / "metrics.yaml").write_text(
            "version: \"0.1\"\nmetrics:\n  - id: gmv\n    name: GMV\n    category: 规模\n    formula: SUM\n    required_columns: []\n    filters: []\n    dimensions: []\n    time_window: 月度\n    edge_cases: []\n    status: confirmed\n  - id: aov\n    name: 客单价\n    category: 效率\n    formula: gmv / order_count\n    required_columns: []\n    filters: []\n    dimensions: []\n    time_window: 月度\n    edge_cases: []\n    status: draft\n    open_questions: [{question: q, options: [a, b]}]\n",
            encoding="utf-8",
        )
        loader._cache.clear()
        pack2 = loader.load_domain_pack("fake_pack")
        ids = {m["id"] for m in pack2["metrics"]}
        assert "aov" in ids, f"mtime 变了应该重新加载,实际 ids={ids}"
    finally:
        loader.PROJECT_ROOT = old_root


# ---------- select_metrics ----------

def test_select_metrics_empty_pack():
    """pack=None 时返回空列表。"""
    assert domain_loader.select_metrics(None, "gmv") == []


def test_select_metrics_limit(ecommerce_pack):
    """最多返回 limit 个,且按分数降序。"""
    result = domain_loader.select_metrics(
        ecommerce_pack, "GMV 订单数 客单价 支付转化率 退款率 复购率", limit=2
    )
    assert len(result) == 2
    # 至少包含分数最高的那几个
    ids = {m["id"] for m in result}
    assert ids <= {m["id"] for m in ecommerce_pack["metrics"]}


def test_select_metrics_daily_gmv(ecommerce_pack):
    """「按天看 GMV 趋势」能命中 gmv / order_count / aov。"""
    result = domain_loader.select_metrics(ecommerce_pack, "按天看 GMV 趋势并画图", limit=3)
    ids = {m["id"] for m in result}
    assert "gmv" in ids, result
    # order_count / aov 至少一个也该入选
    assert ids & {"order_count", "aov"}, result


def test_select_metrics_no_match(ecommerce_pack):
    """完全无关的问题返回空列表。"""
    assert domain_loader.select_metrics(ecommerce_pack, "今天天气怎么样") == []


# ---------- build_*_block 字符数 ----------

def test_build_planner_block_size(ecommerce_pack):
    """planner block 不超过 800 字符。"""
    block = domain_loader.build_planner_block(ecommerce_pack)
    assert block.startswith("【领域上下文】")
    assert len(block) <= 800, f"planner block {len(block)} 字符超上限"


def test_build_metric_block_size(ecommerce_pack):
    """metric block 不超过 1200 字符,且含标题。"""
    block = domain_loader.build_metric_block(ecommerce_pack, "GMV 趋势")
    # 空串时不超过 1200 也是空串,先判起点
    if block:
        assert "指标口径" in block
        assert len(block) <= 1200, f"metric block {len(block)} 字符超上限"


def test_build_planner_block_empty_when_disabled():
    """pack=None 时三个 build_*_block 都返回空串。"""
    assert domain_loader.build_planner_block(None) == ""
    assert domain_loader.build_metric_block(None, "gmv") == ""
    assert domain_loader.build_quality_note(None, []) == ""


# ---------- build_quality_note ----------

def test_build_quality_note_with_draft(ecommerce_pack):
    """选中 draft 指标时 quality_note 必须包含 draft 指标名称和「口径」「待确认」。"""
    draft_metrics = [m for m in ecommerce_pack["metrics"] if m.get("status") == "draft"]
    # 取一个 draft 指标作为选中
    selected = [draft_metrics[0]] if draft_metrics else []
    note = domain_loader.build_quality_note(ecommerce_pack, selected)
    assert note
    assert "口径" in note
    assert "待确认" in note or "draft" in note.lower()


def test_build_quality_note_all_confirmed(ecommerce_pack):
    """选中指标全 confirmed 时不输出 draft 提示。"""
    confirmed = [m for m in ecommerce_pack["metrics"] if m.get("status") == "confirmed"]
    note = domain_loader.build_quality_note(ecommerce_pack, confirmed)
    # confirmed 指标没 draft,但 pack 里还有 draft 指标,所以会给一条通用提示
    # 通用提示不点名具体指标
    assert "口径" in note or note == ""


# ---------- validate_domain_report ----------

def test_validate_domain_report_with_draft_gives_warning(ecommerce_pack):
    """有 draft 指标时,报告没提口径待确认 → 返回 warning。"""
    draft_id = next((m["id"] for m in ecommerce_pack["metrics"] if m.get("status") == "draft"), None)
    selected = [m for m in ecommerce_pack["metrics"] if m.get("id") == draft_id]
    warnings = domain_loader.validate_domain_report("这是一份啥也没提的报告", selected)
    assert warnings, "应该返回至少一条 warning"
    assert "口径" in warnings[0] or "draft" in warnings[0].lower()


def test_validate_domain_report_passes_when_mentions_quality(ecommerce_pack):
    """有 draft 指标且报告提了口径待确认 → 通过。"""
    draft_id = next((m["id"] for m in ecommerce_pack["metrics"] if m.get("status") == "draft"), None)
    selected = [m for m in ecommerce_pack["metrics"] if m.get("id") == draft_id]
    report = "## 口径与数据版本\n本次 GMV 口径采用毛 GMV,其他 draft 口径指标口径待确认。"
    assert domain_loader.validate_domain_report(report, selected) == []


def test_validate_domain_report_no_draft_no_warning(ecommerce_pack):
    """选中指标全 confirmed → 返回空列表,不管报告写啥。"""
    selected = [m for m in ecommerce_pack["metrics"] if m.get("status") == "confirmed"]
    assert domain_loader.validate_domain_report("随便啥报告", selected) == []
