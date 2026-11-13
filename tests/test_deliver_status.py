"""交付(deliver)轮次的状态映射与置信水平数字白名单回归。"""

from validate import validate_report
from web.app import _answer_payload


def test_answer_payload_maps_deliver_status():
    """复用归档的轮次没有新 run_id,但必须给 deliver 状态,不能被当成未完成。"""
    payload = _answer_payload(
        "thread-1",
        {
            "next_action": "deliver",
            "deliver_run_id": "20260101_000000_abcdef",
            "final_answer": "已基于上一轮运行生成",
            "run_id": "",
        },
    )

    assert payload["status"] == "deliver"
    assert payload["deliver_run_id"] == "20260101_000000_abcdef"
    assert payload["run_id"] == ""


def test_confidence_level_is_not_reported_as_fabricated_number():
    """「95% 置信区间」里的 95 是口径常数,不应触发自造数字告警。"""
    warnings = validate_report(
        {"headline": "95% 置信区间为 0.02 至 0.49"},
        "p_value 0.03, ci 0.02 0.49",
    )

    assert not any("95" in warning for warning in warnings)
