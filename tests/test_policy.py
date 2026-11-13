"""审批风险分级策略(web/approval_policy.decide)的离线测试。

不联网、不调 LLM、不读真实数据目录:dataset_meta 全部手工构造。

覆盖(对应需求验收):
    1. 首次分析该数据集 -> confirm;
    2. 已信任 + 只读问题 -> auto;
    3. 列名命中 PII 关键词 -> confirm(即使已信任,信任不免除内容风险);
    4. 文件 > 50MB -> confirm(即使已信任);
    5. 问题含高风险词(导出等) -> confirm(即使已信任);
    6. 策略内部抛异常 -> 回退 confirm(禁止策略报错时自动放行);
    7. APPROVAL_POLICY=confirm -> 永远 confirm(回归现状);
    8. APPROVAL_POLICY=auto -> 永远 auto(演示模式)。
"""

from __future__ import annotations

import pytest

from web import approval_policy
from web.approval_policy import LARGE_FILE_BYTES, decide


@pytest.fixture(autouse=True)
def _risk_mode(monkeypatch):
    """每个用例默认 risk 模式;个别用例自行 setenv 覆盖。"""
    monkeypatch.delenv("APPROVAL_POLICY", raising=False)


_SMALL_META = {"columns": [{"name": "组"}, {"name": "收入"}], "size_bytes": 1024}
_PII_META = {"columns": [{"name": "手机号"}, {"name": "收入"}], "size_bytes": 1024}
_LARGE_META = {"columns": [{"name": "收入"}], "size_bytes": LARGE_FILE_BYTES + 1}


# ---------- 1. 首次分析 ----------

def test_first_time_dataset_confirms():
    result = decide("统计收入均值", "orders.csv", _SMALL_META, [])
    assert result["level"] == "confirm"
    assert any("首次分析该数据集" in r for r in result["reasons"])


# ---------- 2. 已信任 + 只读 ----------

def test_trusted_readonly_auto():
    result = decide("统计收入均值", "orders.csv", _SMALL_META, ["orders.csv"])
    assert result["level"] == "auto"
    assert result["reasons"] == ["本会话已批准该数据集"]


# ---------- 3. PII 列 ----------

def test_pii_columns_confirm_even_when_trusted():
    result = decide("统计收入均值", "users.csv", _PII_META, ["users.csv"])
    assert result["level"] == "confirm"
    assert any("个人信息" in r for r in result["reasons"])


# ---------- 4. 大文件 ----------

def test_large_file_confirms_even_when_trusted():
    result = decide("统计收入均值", "big.csv", _LARGE_META, ["big.csv"])
    assert result["level"] == "confirm"
    assert any("数据量大" in r for r in result["reasons"])


# ---------- 5. 高风险词 ----------

@pytest.mark.parametrize("word", ["写", "导出", "保存", "删除", "修改", "批量", "全量", "外发"])
def test_high_risk_words_confirm_even_when_trusted(word):
    result = decide(f"把结果{word}一下", "orders.csv", _SMALL_META, ["orders.csv"])
    assert result["level"] == "confirm"
    assert any("数据出口" in r for r in result["reasons"])


# ---------- 6. 异常回退 ----------

def test_policy_error_falls_back_to_confirm(monkeypatch):
    """策略内部任何异常都必须回退 confirm,不能因为报错而自动放行。"""
    def _boom(_meta):
        raise RuntimeError("元数据损坏")

    monkeypatch.setattr(approval_policy, "_pii_columns", _boom)
    result = decide("统计收入均值", "orders.csv", _SMALL_META, ["orders.csv"])
    assert result["level"] == "confirm"
    assert any("策略异常" in r for r in result["reasons"])


# ---------- 7. confirm 模式 ----------

def test_confirm_mode_always_confirms(monkeypatch):
    monkeypatch.setenv("APPROVAL_POLICY", "confirm")
    result = decide("统计收入均值", "orders.csv", _SMALL_META, ["orders.csv"])
    assert result["level"] == "confirm"
    assert any("confirm" in r for r in result["reasons"])


# ---------- 8. auto 模式 ----------

def test_auto_mode_always_auto(monkeypatch):
    monkeypatch.setenv("APPROVAL_POLICY", "auto")
    result = decide("把结果导出成 Excel", "orders.csv", _PII_META, [])
    assert result["level"] == "auto"


def test_invalid_mode_falls_back_to_risk(monkeypatch):
    monkeypatch.setenv("APPROVAL_POLICY", "yolo")
    result = decide("统计收入均值", "orders.csv", _SMALL_META, ["orders.csv"])
    assert result["level"] == "auto"  # 非法值按默认 risk 处理
