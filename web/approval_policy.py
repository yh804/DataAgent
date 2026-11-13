"""审批风险分级策略(确定性规则,不调 LLM)。

目标:保留人工审批这条安全线,同时让大多数「只读聚合」轮次自动通过。

规则按顺序命中,返回 {"level": "auto"|"confirm", "reasons": [str]}:

    1. 数据集已被本会话信任 且 问题不含高风险词 且 无 PII 列 且 文件 <= 50MB
       -> auto,理由「本会话已批准该数据集」
    2. 首次分析该数据集 -> confirm,理由「首次分析该数据集」
    3. 问题含高风险词(写|导出|保存|删除|修改|批量|全量|外发)
       -> confirm,理由「可能产生数据出口或副作用」
    4. 列名命中 PII 关键词 -> confirm,理由「疑似包含个人信息字段」
    5. 文件 > 50MB -> confirm,理由「数据量大,耗时与成本较高」
    6. 其余 -> auto,理由「只读聚合,风险低」

与需求字面顺序的一处刻意偏差(修正语义):
    若按字面顺序,「首次分析」规则会永远先于 PII / 大文件规则命中,
    使后两者成为死代码。因此规则 1 的 auto 额外要求「无 PII 列且文件不大」
    —— 信任不免除内容风险,符合「保留安全线」的目标。

环境变量 APPROVAL_POLICY:
    risk    默认,按上述规则判定;
    confirm 每轮都问(回归现状);
    auto    全部自动通过(仅演示用)。

任何异常一律回退 confirm:禁止因为策略报错而自动放行。
"""

from __future__ import annotations

import os
from typing import Any

# 高风险词:问题中出现即意味着可能有数据出口或副作用
HIGH_RISK_WORDS: tuple[str, ...] = ("写", "导出", "保存", "删除", "修改", "批量", "全量", "外发")

# PII 关键词:列名包含即提示「疑似包含个人信息字段」
PII_WORDS: tuple[str, ...] = ("手机号", "手机", "身份证", "邮箱", "姓名", "地址", "银行卡", "护照")

# 大文件阈值:超过即要求人工确认(耗时与成本较高)
LARGE_FILE_BYTES = 50 * 1024 * 1024

_VALID_MODES = {"risk", "confirm", "auto"}


def _policy_mode() -> str:
    """读取 APPROVAL_POLICY;非法值按默认 risk 处理。每次调用读环境变量,便于测试 monkeypatch。"""
    mode = (os.getenv("APPROVAL_POLICY") or "risk").strip().lower()
    return mode if mode in _VALID_MODES else "risk"


def _has_high_risk_word(question: str) -> bool:
    return any(word in (question or "") for word in HIGH_RISK_WORDS)


def _pii_columns(dataset_meta: dict[str, Any]) -> list[str]:
    """从 describe_dataset 的返回里找列名命中 PII 关键词的列。meta 结构缺失时按无 PII 处理。"""
    hits: list[str] = []
    for col in dataset_meta.get("columns") or []:
        name = str(col.get("name") or "") if isinstance(col, dict) else str(col)
        if any(word in name for word in PII_WORDS):
            hits.append(name)
    return hits


def _file_size_bytes(dataset_meta: dict[str, Any]) -> int:
    """describe_dataset 返回的 size_bytes;取不到按 0(不触发大文件规则)。"""
    try:
        return int(dataset_meta.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return 0


def decide(
    question: str,
    dataset_name: str,
    dataset_meta: dict[str, Any],
    trusted_datasets: list[str],
) -> dict[str, Any]:
    """对本轮分析计划做风险分级。确定性判断,不调 LLM;任何异常回退 confirm。"""
    try:
        mode = _policy_mode()
        if mode == "auto":
            return {"level": "auto", "reasons": ["APPROVAL_POLICY=auto,演示模式全部放行"]}
        if mode == "confirm":
            return {"level": "confirm", "reasons": ["APPROVAL_POLICY=confirm,每轮都需人工确认"]}

        question = question or ""
        dataset_name = dataset_name or ""
        trusted = dataset_name in (trusted_datasets or [])
        high_risk = _has_high_risk_word(question)
        pii_cols = _pii_columns(dataset_meta or {})
        too_large = _file_size_bytes(dataset_meta or {}) > LARGE_FILE_BYTES

        # 规则 1:已信任 + 无任何内容风险 -> auto(信任不免除 PII / 大文件 / 高风险词)
        if trusted and not high_risk and not pii_cols and not too_large:
            return {"level": "auto", "reasons": ["本会话已批准该数据集"]}
        # 规则 2:首次分析该数据集
        if not trusted:
            return {"level": "confirm", "reasons": ["首次分析该数据集"]}
        # 规则 3:高风险词(数据出口或副作用)
        if high_risk:
            return {"level": "confirm", "reasons": ["可能产生数据出口或副作用"]}
        # 规则 4:PII 列
        if pii_cols:
            return {
                "level": "confirm",
                "reasons": [f"疑似包含个人信息字段: {', '.join(pii_cols[:5])}"],
            }
        # 规则 5:大文件
        if too_large:
            return {"level": "confirm", "reasons": ["数据量大,耗时与成本较高"]}
        # 规则 6:只读聚合
        return {"level": "auto", "reasons": ["只读聚合,风险低"]}
    except Exception:  # noqa: BLE001 - 策略报错绝不自动放行
        return {"level": "confirm", "reasons": ["策略异常,回退人工确认"]}
