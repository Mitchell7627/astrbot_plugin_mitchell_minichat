"""Controls around cancellation, stale work and partial delivery."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import Plain


def message(text, umo):
    return SimpleNamespace(unified_msg_origin=umo, get_messages=lambda: [Plain(text)], message_str=text)


@pytest.mark.parametrize("text", [
    "不要取消下午三点的会议", "下午三点的会议没有取消", "下午三点的会议取消了吗",
    "我妈取消下午三点的会议", "我妈现在取消下午三点的会议",
    "他说“下午三点的会议取消了”", "如果下雨，下午三点的会议就取消",
    "我梦见下午三点的会议取消了", "我不去洗澡了", "我不去买菜了",
])
def test_unrelated_or_nonasserted_cancellation_preserves_appointment(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    original = r._appointments(umo)
    r.update_from_message(umo, text)
    assert r._appointments(umo) == original


def test_cancellation_matches_clock_and_does_not_remove_other_meeting(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    r.update_from_message(umo, "我下午四点开会")
    remaining_id = r._appointments(umo)[1]["id"]
    r.update_from_message(umo, "我下午三点的会议取消了")
    assert [item["id"] for item in r._appointments(umo)] == [remaining_id]


def test_ambiguous_cancellation_does_not_remove_multiple_meetings(plugin, umo, clock):
    r = plugin.runner
    for text in ("我下午三点开会", "我下午四点开会"):
        r.update_from_message(umo, text)
    original = r._appointments(umo)
    r.update_from_message(umo, "会议取消了")
    assert r._appointments(umo) == original
    r.update_from_message(umo, "全部约定取消了")
    assert not r._appointments(umo)


def test_cancel_and_add_new_appointment_in_same_message(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    r.update_from_message(umo, "下午三点会议取消了，我下午四点吃饭")
    assert len(r._appointments(umo)) == 1
    assert r._appointments(umo)[0]["label"] == "吃饭"


@pytest.mark.parametrize("mode", ["normal", "gray", "appointment_gray"])
async def test_remove_then_readd_still_discards_original_result(plugin, umo, clock, monkeypatch, mode):
    r = plugin.runner
    r.started = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        entered.set()
        await release.wait()
        return {"name": "shower", "done": False}

    async def verify(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text="YES")

    monkeypatch.setattr(r, "classify_text", classify)
    monkeypatch.setattr(r, "_recent_dialogue", AsyncMock(return_value=""))
    plugin.context.get_using_provider = lambda **kwargs: SimpleNamespace(text_chat=verify)
    text = {"normal": "我准备去整点东西", "gray": "今天洗澡", "appointment_gray": "我大概十二点下课"}[mode]
    await plugin.on_private_message(message(text, umo))
    await asyncio.wait_for(entered.wait(), 1)
    plugin.panel_service.task_action({"action": "remove", "umo": umo})
    plugin.panel_service.task_action({"action": "add", "umo": umo})
    new_timer = r.timers[umo]
    release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    assert r.timers[umo] is new_timer
    assert plugin.store.get(umo, "active_event") is None
    assert not r._appointments(umo)


@pytest.mark.parametrize("new_text", ["我睡了", "我现在正在吃饭"])
async def test_user_turn_between_fire_segments_stops_tail_and_preserves_counter(plugin, umo, clock, monkeypatch, new_text):
    r = plugin.runner
    r.started = True
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value="第一段。第二段。"))
    monkeypatch.setattr(r, "_append_assistant_history", AsyncMock())

    async def send(*args):
        await plugin.on_private_message(message(new_text, umo))
        return True

    plugin.context.send_message.side_effect = send
    result = await plugin.panel_service.fire({"umo": umo})
    assert result["delivery_status"] == "partial"
    assert result["delivered_segments"] == 1
    assert plugin.context.send_message.await_count == 1
    assert plugin.store.get(umo, "unanswered") == 0
    r._append_assistant_history.assert_awaited_once_with(umo, "第一段。")


@pytest.mark.parametrize("text", ["我到家了晚安我睡了", "我下午三点开会，我先睡了"])
def test_sleep_keeps_independent_presence_and_future_information(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我去公司上班了")
    r.update_from_message(umo, text)
    assert r.is_sleeping(umo)
    if "到家" in text:
        assert not (plugin.store.get(umo, "away") or {}).get("active")
    else:
        assert len(r._appointments(umo)) == 1
