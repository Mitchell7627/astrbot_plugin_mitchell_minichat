"""Night timestamp, repeated quantities and scoped colloquial cancellation."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from astrbot.core.message.components import Plain
from astrbot_plugin_mitchell_minichat.core.appointment import find_clock, resolve_at


@pytest.mark.parametrize("now_hour", [0, 10, 18, 22, 23])
@pytest.mark.parametrize("word", ["今晚", "晚上", "夜里", "晚间"])
def test_night_one_resolves_to_next_morning(now_hour, word):
    now = datetime(2026, 11, 2, now_hour)
    c = find_clock(word + "一点到家")
    at = resolve_at(c["h"], c["m"], c["period"], now, 48)
    assert datetime.fromtimestamp(at) == datetime(2026, 11, 3, 1)


@pytest.mark.parametrize("text,expected", [
    ("晚上六点下课", datetime(2026, 11, 2, 18)),
    ("晚上十二点到家", datetime(2026, 11, 3)),
    ("晚上五点到家", datetime(2026, 11, 3, 5)),
    ("今晚0:30到家", datetime(2026, 11, 3, 0, 30)),
    ("晚上23:30到家", datetime(2026, 11, 2, 23, 30)),
    ("下午一点到家", datetime(2026, 11, 2, 13)),
    ("下午十二点到家", datetime(2026, 11, 2, 12)),
    ("中午十二点到家", datetime(2026, 11, 2, 12)),
    ("中午一点到家", datetime(2026, 11, 2, 13)),
])
def test_day_evening_midnight_clock_controls(text, expected):
    c = find_clock(text)
    at = resolve_at(c["h"], c["m"], c["period"], datetime(2026, 11, 2, 10), 48)
    assert datetime.fromtimestamp(at) == expected


@pytest.mark.parametrize("hour", [10, 18, 22])
def test_night_meeting_never_becomes_afternoon(plugin, umo, clock, hour):
    clock.now = datetime(2026, 11, 2, hour).timestamp()
    plugin.runner.update_from_message(umo, "今晚一点开会")
    assert not plugin.runner._appointments(umo)  # 原深夜 start 类提醒保护保留


def test_conflicting_night_hour_is_not_afternoon():
    c = find_clock("今晚十三点到家")
    assert resolve_at(c["h"], c["m"], c["period"], datetime(2026, 11, 2, 10), 48) is None


@pytest.mark.parametrize("hour,count", [(10, 0), (18, 1), (22, 1)])
def test_night_arrival_keeps_configured_horizon(plugin, umo, clock, hour, count):
    clock.now = datetime(2026, 11, 2, hour).timestamp()
    plugin.engine_cfg["appointment_max_hours"] = 14
    plugin.runner.update_from_message(umo, "今晚一点到家")
    assert len(plugin.runner._appointments(umo)) == count


def test_night_arrival_register_cancel_undo(plugin, umo, clock):
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    r = plugin.runner
    r.update_from_message(umo, "今晚一点到家")
    before = r._appointments(umo)
    assert len(before) == 1
    assert datetime.fromtimestamp(before[0]["at"]) == datetime(2026, 11, 3, 1)
    r.update_from_message(umo, "今晚一点到家的提醒不用了")
    assert not r._appointments(umo)
    rec = next(x for x in r.decisions.items(umo) if x["rule"] == "CANCEL")
    r.undo_decision(rec["id"])
    assert r._appointments(umo) == before


@pytest.mark.parametrize("text", [
    "一点一点来", "一点一点一点来", "一 点 一 点来", "一点一点收拾", "一点点来", "一点儿一点儿来",
])
def test_repeated_quantity_never_becomes_clock(text):
    assert find_clock(text) is None


@pytest.mark.parametrize("text", [
    "一点一点来，下午三点开会", "一点，一点半下课", "一点一点来，14:30开会",
])
def test_real_clock_after_repetition_is_still_found(text):
    c = find_clock(text)
    assert c is not None
    assert (c["h"], c["m"]) in [(3, 0), (1, 30), (14, 30)]


def test_repetition_does_not_mask_keyword_activity(plugin, umo, clock):
    plugin.runner.update_from_message(umo, "我一点一点收拾好去冲个澡")
    assert plugin.store.get(umo, "active_event")["name"] == "errand"


async def test_repetition_does_not_starve_semantic_activity(plugin, umo, clock, monkeypatch):
    classify = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classify)
    text = "我一点一点去冲个澡"
    event = SimpleNamespace(unified_msg_origin=umo, message_str=text, get_messages=lambda: [Plain(text)])
    await plugin.on_private_message(event)
    if plugin._bg_tasks:
        await asyncio.gather(*list(plugin._bg_tasks))
    classify.assert_awaited_once()
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


@pytest.mark.parametrize("text", [
    "六点的那个取消掉", "六点的那个取消掉了", "取消掉六点的下课提醒",
    "六点下课不用了", "六点的下课提醒不要了", "六点的提醒算了吧",
    "那个提醒不用了", "所有提醒不用了",
])
def test_colloquial_cancel_explicit_target(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我晚上六点下课")
    r.update_from_message(umo, text)
    assert not r._appointments(umo)
    rec = next(x for x in r.decisions.items(umo) if x["rule"] == "CANCEL")
    assert rec["action"] == "applied" and rec["undo_state"] == "available"


@pytest.mark.parametrize("text", [
    "不用取消六点的下课提醒", "不要取消六点的下课提醒",
    "六点的提醒取消不掉", "六点的提醒取消掉不了",
    "六点的提醒算了我还是要", "算了我还是六点下课",
    "六点的那个不用了但别取消", "不用了", "不要了", "算了",
    "他说六点的提醒不用了", "我妈六点的提醒不用了", "如果六点的提醒不用了",
    "六点的提醒不用了吗", "六点的体检提醒不用了",
])
def test_colloquial_cancel_guards(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我晚上六点下课")
    before = r._appointments(umo)
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before
    assert not any(x["rule"] == "CANCEL" for x in r.decisions.items(umo))


@pytest.mark.parametrize("text,expected", [
    ("六点的那个不用了", ["17:00"]),
    ("所有提醒不用了", []),
    ("那个提醒不用了", ["17:00", "18:00"]),
])
def test_colloquial_scope_and_ambiguity(plugin, umo, clock, text, expected):
    r = plugin.runner
    r.update_from_message(umo, "我下午五点开会")
    r.update_from_message(umo, "我晚上六点下课")
    r.update_from_message(umo, text)
    assert [x["time_text"] for x in r._appointments(umo)] == expected


def test_colloquial_stale_undo_preserves_newer_state(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我晚上六点下课")
    r.update_from_message(umo, "六点的那个取消掉")
    rec = next(x for x in r.decisions.items(umo) if x["rule"] == "CANCEL")
    clock.now += 10
    r.update_from_message(umo, "我下午五点开会")
    newer = r._appointments(umo)
    with pytest.raises(ValueError):
        r.undo_decision(rec["id"])
    assert r._appointments(umo) == newer
