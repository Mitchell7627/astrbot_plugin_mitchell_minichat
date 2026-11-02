"""v0.2.7 守护测试：跨功能状态一致性 + 新增判定的正/负例。

只断言 v0.2.7 明确要保证的行为；不重复现有 4 个测试文件已覆盖的用例。
"""
import time

import pytest

from test_minichat import runner, UMO  # noqa: F401  (runner 是 fixture)
from test_conflicts import at_time
from core import text_rules as rules
from core.runner import Runner


# ---------------------------------------------------------------- 跨功能提示
def test_away_and_event_hint_are_combined(runner):
    """不在身边 + 当前事件同时成立时，两个状态都要出现在提示里。"""
    with at_time(10):
        runner.update_from_message(UMO, "我出门了")
        runner.update_from_message(UMO, "我在路上")
        event = runner.store.get(UMO, "active_event")
        assert event and event.get("name") == "commute"
        hint = runner.event_hint(UMO)
        assert "不在你这边" in hint
        assert "路上" in hint


def test_together_hint_respects_her_event_subject(runner):
    """「你正在洗澡」的主体是她，不能写成「你们现在在洗澡」。"""
    with at_time(10):
        runner._touch_together(UMO, time.time())
        runner.update_from_message(UMO, "你正在洗澡")
        event = runner.store.get(UMO, "active_event")
        assert event and event.get("subject") == "her"
        hint = runner.event_hint(UMO)
        assert "你现在在洗澡" in hint
        assert "你们现在在洗澡" not in hint


def test_event_end_does_not_leave_long_together_tail(runner):
    """长事件结束后，事件关联的「在一起」要收尾，不能挂满 300 分钟。"""
    with at_time(10):
        runner.update_from_message(UMO, "我们一起约会")
        together = runner.store.get(UMO, "together")
        assert together and together.get("active")
        assert together.get("event_linked") is True
        runner.update_from_message(UMO, "约会结束了")
        together = runner.store.get(UMO, "together")
        assert together and together.get("active")
        assert together.get("event_linked") is False
        assert together["until"] <= time.time() + 16 * 60


def test_period_end_closes_matching_event(runner):
    """下班/下课结束工作事件，但人还在外面，不能当成已回到身边。"""
    with at_time(18):
        runner.update_from_message(UMO, "我出门了")
        runner.update_from_message(UMO, "我在写代码")
        event = runner.store.get(UMO, "active_event")
        assert event and event.get("name") == "working"
        runner.update_from_message(UMO, "下班了")
        away = runner.store.get(UMO, "away", {})
        assert away.get("active")
        assert "下班" in (away.get("reason") or "")
        assert runner.store.get(UMO, "active_event") is None
        assert "不在你这边" in runner.event_hint(UMO)
        runner.update_from_message(UMO, "我到家了")
        assert not runner.store.get(UMO, "away", {}).get("active")


def test_sleeping_appointment_is_deferred_to_wake(runner):
    """睡眠中到点的约定改排到起床后，不能静默丢。"""
    with at_time(1):
        runner.update_from_message(UMO, "我睡了")
        now = time.time()
        runner._save_appointments(UMO, [{
            "at": now - 60,
            "label": "到家",
            "kind": "arrive",
            "time_text": "01:00",
            "stage": "pending",
            "lead_sent_at": 0,
            "due_sent_at": 0,
            "follow_up_at": 0,
            "follow_ups": 0,
            "fails": 0,
        }])
        wake_at = float(runner._sleep_state(UMO)["wake_at"])
        assert runner._defer_appointment(UMO, now - 60, wake_at)
        item = runner._appointments(UMO)[0]
        assert item.get("deferred") is True
        assert abs(float(item["at"]) - (wake_at + 60)) < 1
        assert item.get("stage") == "pending"


def test_arrive_appointment_clears_away_and_commute(runner):
    """「到家」约定到点：清 away、结束通勤事件。"""
    with at_time(18):
        runner.update_from_message(UMO, "我出门了")
        runner.update_from_message(UMO, "我在路上")
        assert runner.store.get(UMO, "active_event", {}).get("name") == "commute"
        runner._save_appointments(UMO, [{
            "at": time.time(),
            "label": "到家",
            "kind": "arrive",
            "time_text": "18:00",
            "stage": "pending",
            "fails": 0,
        }])
        runner._advance_appointment(UMO, "due", time.time())
        assert not runner.store.get(UMO, "away", {}).get("active")
        assert runner.store.get(UMO, "active_event") is None


def test_away_with_her_event_does_not_claim_company(runner):
    """人不在身边时，「她正在洗澡」不能写成「他就在旁边陪着你」。"""
    with at_time(10):
        runner.update_from_message(UMO, "我出门了")
        runner.update_from_message(UMO, "你正在洗澡")
        hint = runner.event_hint(UMO)
        assert "不在你这边" in hint
        assert "旁边陪着" not in hint


# ---------------------------------------------------------------- 关键词层负例
def test_wake_negative_controls_keep_sleeping(runner):
    for text in (
        "我还没起床", "明天起床再说", "我妈醒了", "你醒了吗",
        "他说我醒了", "我起床气很大", "如果我醒了就找你",
    ):
        with at_time(1):
            runner.update_from_message(UMO, "我睡了")
            assert runner.is_sleeping(UMO)
            runner.update_from_message(UMO, text)
            assert runner.is_sleeping(UMO), text


def test_conditional_true_positive_and_incidental_mention():
    assert rules.is_conditional_clause("如果下雨，我就去洗澡", 7)
    assert rules.is_conditional_clause("如果你在，我就去洗澡", 7)
    assert rules.is_conditional_clause("如果下雨的话，我就不去", 8)
    assert not rules.is_conditional_clause("刚才我们讨论了如果下雨怎么办，现在我在洗澡", 16)


def test_possessive_de_hua_is_not_conditional():
    assert not rules.is_conditional_clause("我正在开会先别打断我的话", 4)
    assert not rules.is_conditional_clause("我说的话他都记得", 6)
    assert rules.is_conditional_clause("下雨的话，我就不去了", 0)


def test_negation_false_friends_are_not_negation():
    assert not rules.negated_by_clause("不过我在洗澡", 4, rules.SELF_NEGATIONS)
    assert not rules.negated_by_clause("不但洗澡还洗头", 2, rules.SELF_NEGATIONS)
    assert not rules.negated_by_clause("不小心把水洒了，我在洗澡", 8, rules.SELF_NEGATIONS)
    assert rules.negated_by_clause("我没有在坐地铁", 6, rules.SELF_NEGATIONS)
    assert rules.negated_by_clause("不是我在看电影，是朋友", 5, rules.SELF_NEGATIONS)


def test_dream_guard_does_not_block_real_action_after_waking(runner):
    runner.update_from_message(UMO, "我做了个梦，现在在洗澡")
    event = runner.store.get(UMO, "active_event")
    assert event and event.get("name") == "shower"


def test_third_person_clause_only_for_stop():
    assert rules.is_third_person_clause("我妈约会结束了", 5)
    assert not rules.is_third_person_clause("我和我妈约会结束了", 7)
    # 事件路径仍用严格形态：「我妈在做饭」算第三人称，「我妈做饭」不算谓语形态
    assert rules.is_third_person_subject("我妈在做饭", 3)


# ---------------------------------------------------------------- together 边界
def test_hehao_is_not_a_companion(runner):
    runner.update_from_message(UMO, "我和好了，现在我们一起吃饭")
    assert runner.store.get(UMO, "together", {}).get("active")


def test_second_person_away_is_not_user_away(runner):
    for text in ("你去上班吧", "你快去上班", "您去开会吧"):
        with at_time(10):
            runner.update_from_message(UMO, text)
            assert not runner.store.get(UMO, "away", {}).get("active"), text


def test_appointment_before_current_action_is_consumed(runner):
    """约定在前、当前动作在后：约定被消费，后半句的洗澡仍要识别。"""
    with at_time(10):
        runner.update_from_message(UMO, "下午三点开会，我现在在洗澡")
        assert len(runner._appointments(UMO)) == 1
        event = runner.store.get(UMO, "active_event")
        assert event and event.get("name") == "shower"


# ---------------------------------------------------------------- 工具协议
def test_tool_placeholder_is_inserted_inside_its_group(runner):
    history = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "answer"},
    ]
    out = Runner._repair_tool_calls(history)
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "assistant"]
    assert out[1]["tool_call_id"] == "c1"
    assert out[2]["tool_call_id"] == "c2"
