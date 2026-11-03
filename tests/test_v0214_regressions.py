"""v0.2.14: appointment scope, cancellation, arbitration and polarity regressions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import Plain
from astrbot_plugin_mitchell_minichat.core.appointment import find_clock


def msg(text, umo):
    return SimpleNamespace(unified_msg_origin=umo, get_messages=lambda: [Plain(text)], message_str=text)


async def drain(plugin):
    if plugin._bg_tasks:
        await asyncio.gather(*list(plugin._bg_tasks))


@pytest.mark.parametrize("prefix", [
    "我妈已经下课了", "他已经到家了", "昨天我散会了", "你下班了",
])
@pytest.mark.parametrize("first", [True, False])
def test_other_clause_completion_cannot_reject_real_appointment(plugin, umo, clock, prefix, first):
    parts = [prefix, "我下午三点开会"]
    if not first:
        parts.reverse()
    plugin.runner.update_from_message(umo, "，".join(parts))
    assert [v["time_text"] for v in plugin.runner._appointments(umo)] == ["15:00"]


@pytest.mark.parametrize("prefix", ["今天心情不错", "我今天正在吃饭", "今晚想看星星"])
@pytest.mark.parametrize("day", ["明天", "后天"])
def test_today_in_other_clause_cannot_turn_future_day_into_today(plugin, umo, clock, prefix, day, same_day_only):
    plugin.runner.update_from_message(umo, f"{prefix}，我{day}下午三点开会")
    assert not plugin.runner._appointments(umo), plugin.runner._appointments(umo)


@pytest.mark.parametrize("prefix", ["现在十点了", "两点天气不错", "你两点开会吗", "我两点没有安排"])
def test_rejected_first_clock_does_not_hide_later_real_clock(plugin, umo, clock, prefix):
    plugin.runner.update_from_message(umo, prefix + "，我下午三点开会")
    assert [v["time_text"] for v in plugin.runner._appointments(umo)] == ["15:00"]
    assert plugin.store.get(umo, "active_event") is None


@pytest.mark.parametrize("text", [
    "给我一点时间", "还有一点时间", "花一点时间", "一点小事", "一点要求",
])
def test_quantity_point_is_not_a_clock(text):
    assert find_clock(text) is None


@pytest.mark.parametrize("text,hour", [("一点开会", 1), ("十三点开会", 13), ("一点半下课", 1)])
def test_real_clock_positive_controls(text, hour):
    assert find_clock(text)["h"] == hour


@pytest.mark.parametrize("appointment", ["理发预约", "牙医预约", "体检预约", "洗车预约"])
def test_named_unrelated_cancellation_does_not_delete_meeting(plugin, umo, clock, appointment):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    original = r._appointments(umo)
    r.update_from_message(umo, f"下午三点的{appointment}取消了")
    assert r._appointments(umo) == original


@pytest.mark.parametrize("prefix", ["不是取消", "并非取消", "不是要取消", "并不是取消"])
def test_negative_cancellation_does_not_delete_meeting(plugin, umo, clock, prefix):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    original = r._appointments(umo)
    r.update_from_message(umo, prefix + "下午三点的会议")
    assert r._appointments(umo) == original


@pytest.mark.parametrize("label", ["约定", "安排"])
def test_generic_clock_cancellation_targets_unique_item(plugin, umo, clock, label):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    r.update_from_message(umo, f"下午三点的{label}取消了")
    assert not r._appointments(umo)


@pytest.mark.parametrize("time_text", ["中午一点", "晚上十二点"])
def test_cancellation_uses_same_period_normalization_as_registration(plugin, umo, clock, time_text):
    r = plugin.runner
    r.update_from_message(umo, f"我{time_text}到家")
    assert len(r._appointments(umo)) == 1
    r.update_from_message(umo, f"我{time_text}到家的约定取消了")
    assert not r._appointments(umo)


@pytest.mark.parametrize("verdict", ["YES", "NO", "quota_exhausted", "disabled"])
async def test_unrelated_gray_clause_cannot_starve_explicit_current_fallback(plugin, umo, clock, monkeypatch, verdict):
    r = plugin.runner
    classify = AsyncMock(return_value={"name": "working", "done": False})
    monkeypatch.setattr(r, "classify_text", classify)
    monkeypatch.setattr(r, "_recent_dialogue", AsyncMock(return_value=""))
    verify = AsyncMock(return_value=SimpleNamespace(completion_text=verdict))
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=verify)
    if verdict == "quota_exhausted":
        plugin.engine_cfg["gray_review_per_hour"] = 0
    if verdict == "disabled":
        plugin.engine_cfg["enable_gray_review"] = False
    await plugin.on_private_message(msg("今天洗澡，我现在准备埋头码字了", umo))
    await drain(plugin)
    classify.assert_awaited_once()
    assert plugin.store.get(umo, "active_event")["name"] == "working"


async def test_explicit_unknown_current_action_without_gray_control(plugin, umo, clock, monkeypatch):
    monkeypatch.setattr(plugin.runner, "classify_text", AsyncMock(return_value={"name": "working", "done": False}))
    await plugin.on_private_message(msg("我现在准备埋头码字了", umo))
    await drain(plugin)
    assert plugin.store.get(umo, "active_event")["name"] == "working"


@pytest.mark.parametrize("gray_first", [False, True])
async def test_appointment_daily_limit_applies_to_keyword_and_gray_together(plugin, umo, clock, gray_first):
    r = plugin.runner
    plugin.engine_cfg["appointment_max_per_day"] = 1
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="YES"))
    )
    clauses = ["我十一点下课", "我大概十二点下课"]
    if gray_first:
        clauses.reverse()
    for text in clauses:
        await plugin.on_private_message(msg(text, umo))
        await drain(plugin)
    assert len(r._appointments(umo)) == 1, r._appointments(umo)


@pytest.mark.parametrize("text", [
    "我不是没在冲个澡", "我并不是没有在洗香香", "我现在不是没在沐浴",
])
async def test_positive_double_negation_can_reach_semantic_classifier(plugin, umo, clock, monkeypatch, text):
    classifier = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classifier)
    await plugin.on_private_message(msg(text, umo))
    await drain(plugin)
    classifier.assert_awaited_once()
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


@pytest.mark.parametrize("text", [
    "我没有在冲个澡", "明天我准备埋头码字", "他说“我去洗香香了”",
])
async def test_negative_future_quoted_fallback_controls(plugin, umo, clock, monkeypatch, text):
    classifier = AsyncMock()
    monkeypatch.setattr(plugin.runner, "classify_text", classifier)
    await plugin.on_private_message(msg(text, umo))
    await drain(plugin)
    classifier.assert_not_awaited()
    assert plugin.store.get(umo, "active_event") is None


@pytest.mark.parametrize("text,expected", [
    ("我正在洗澡", "shower"), ("我正在吃饭", "eating"), ("我正在开会", "meeting"),
])
def test_deterministic_activity_controls(plugin, umo, clock, text, expected):
    plugin.runner.update_from_message(umo, text)
    assert plugin.store.get(umo, "active_event")["name"] == expected

@pytest.mark.parametrize("text", [
    "取消下午三点的理发预约", "下午三点理发取消了",
    "不要取消下午三点的会议", "别取消下午三点的会议",
    "他说下午三点的会议取消了", "我妈下午三点的会议取消了",
    "如果下午三点的会议取消了", "下午三点的会议取消了吗",
])
def test_cancel_safety_controls(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    before = r._appointments(umo)
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before


@pytest.mark.parametrize("text", ["下午三点的会议取消了", "取消下午三点的会议", "下午三点取消了"])
def test_cancel_positive_controls(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    r.update_from_message(umo, text)
    assert not r._appointments(umo)


@pytest.mark.parametrize("text", [
    "我不是没在冲个澡，但是我没有在洗香香",
    "他说我不是没在冲个澡", "明天我不是没在冲个澡",
])
async def test_double_negative_protection(plugin, umo, clock, monkeypatch, text):
    classify = AsyncMock(return_value={"name": "none", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classify)
    await plugin.on_private_message(msg(text, umo))
    await drain(plugin)
    assert plugin.store.get(umo, "active_event") is None

    if text.startswith(("他说", "明天")):
        classify.assert_not_awaited()
    else:
        assert "没有" not in classify.await_args.args[1]


async def test_gray_fallback_retained_after_none(plugin, umo, clock, monkeypatch):
    classify = AsyncMock(return_value={"name": "none", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classify)
    monkeypatch.setattr(plugin.runner, "_recent_dialogue", AsyncMock(return_value=""))
    verify = AsyncMock(return_value=SimpleNamespace(completion_text="YES"))
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=verify)
    await plugin.on_private_message(msg("今天洗澡，我知道了", umo))
    await drain(plugin)
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


def test_two_real_clocks_and_invalid_clock_control(plugin, umo, clock):
    plugin.engine_cfg["appointment_max_per_day"] = 6
    plugin.runner.update_from_message(umo, "25:99 是错的，我下午三点开会，我下午四点下课")
    assert sorted(x["time_text"] for x in plugin.runner._appointments(umo)) == ["15:00", "16:00"]


async def test_gray_cannot_add_after_daily_limit_changes(plugin, umo, clock):
    r = plugin.runner
    plugin.engine_cfg["appointment_max_per_day"] = 1
    started, release = asyncio.Event(), asyncio.Event()
    async def answer(**kw):
        started.set()
        await release.wait()
        return SimpleNamespace(completion_text="YES")
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=answer)
    await plugin.on_private_message(msg("我大概十二点下课", umo))
    await asyncio.wait_for(started.wait(), 2)
    plugin.store.set(umo, "appointment_counter", {"date": "2026-11-02", "n": 1})
    release.set()
    await drain(plugin)
    assert not r._appointments(umo)

