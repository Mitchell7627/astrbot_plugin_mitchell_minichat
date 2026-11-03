"""Local-only date/period fixes: state, timestamps, rejection and cancellation contracts."""
import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain
from astrbot_plugin_mitchell_minichat.core.appointment import find_clock, resolve_at


async def send(plugin, umo, text):
    await plugin.on_private_message(SimpleNamespace(
        unified_msg_origin=umo, message_str=text, get_messages=lambda: [Plain(text)]))
    if plugin._bg_tasks:
        await asyncio.gather(*list(plugin._bg_tasks))


@pytest.mark.parametrize("text,now,expected", [
    ("凌晨十二点到家", datetime(2026, 11, 2, 22), datetime(2026, 11, 3)),
    ("凌晨零点到家", datetime(2026, 11, 2, 22), datetime(2026, 11, 3)),
    ("凌晨一点半到家", datetime(2026, 11, 2, 22), datetime(2026, 11, 3, 1, 30)),
    ("上午十二点到家", datetime(2026, 11, 2, 10), datetime(2026, 11, 2, 12)),
    ("中午十二点到家", datetime(2026, 11, 2, 10), datetime(2026, 11, 2, 12)),
    ("晚上十二点到家", datetime(2026, 11, 2, 22), datetime(2026, 11, 3)),
    ("凌晨十二点到家", datetime(2026, 12, 31, 22), datetime(2027, 1, 1)),
])
def test_period_final_timestamp(text, now, expected):
    c = find_clock(text)
    at = resolve_at(c["h"], c["m"], c["period"], now, 48)
    assert datetime.fromtimestamp(at) == expected


@pytest.mark.parametrize("text", [
    "今晚预计会在一点到家", "今晚，我预计会在一点到家",
    "晚上我应该会在一点到家", "凌晨我预计会在一点到家",
])
def test_distant_or_adjacent_pure_period(plugin, umo, clock, text):
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    plugin.runner.update_from_message(umo, text)
    items = plugin.runner._appointments(umo)
    assert len(items) == 1
    assert datetime.fromtimestamp(items[0]["at"]) == datetime(2026, 11, 3, 1)


def test_distant_night_does_not_become_afternoon_when_out_of_horizon(plugin, umo, clock):
    plugin.runner.update_from_message(umo, "今晚预计会在一点到家")
    assert not plugin.runner._appointments(umo)


def test_period_clauses_do_not_borrow_other_activities():
    assert find_clock("今晚有事，我一点到家")["period"] is None
    assert find_clock("今晚有事，下午一点到家")["period"] == "pm"
    assert find_clock("今晚，我一点到家")["period"] == "night"
    assert find_clock("我今晚预计会在一点到家")["period"] == "night"


def test_nearest_period_per_clock(plugin, umo, clock):
    plugin.runner.update_from_message(umo, "下午一点到家，晚上十点下课")
    items = plugin.runner._appointments(umo)
    assert [datetime.fromtimestamp(x["at"]) for x in items] == [
        datetime(2026, 11, 2, 13), datetime(2026, 11, 2, 22)]


@pytest.mark.parametrize("date", [
    "11月2日", "11月2号", "2026年11月2日", "二零二六年十一月二日", "十一月二日",
    "周一", "星期一", "礼拜一", "这周一", "本星期一", "本周星期一", "周1",
])
def test_today_calendar_and_weekday_registers(plugin, umo, clock, date):
    plugin.runner.update_from_message(umo, f"我{date}下午三点开会")
    items = plugin.runner._appointments(umo)
    assert len(items) == 1
    assert datetime.fromtimestamp(items[0]["at"]) == datetime(2026, 11, 2, 15)


@pytest.mark.parametrize("date", [
    "11月4日", "2027年11月2日", "2025年11月2日", "2月30日", "13月2日", "0月2日",
    "2026年2月29日", "二零二七年十一月二日", "十一月四日",
    "周三", "星期三", "礼拜三", "周末", "下周一", "上周一", "下星期一",
    "下周星期一", "下个星期一", "下一个星期一", "下一周一", "这周三", "本周三",
])
def test_other_or_invalid_day_rejects_without_model(plugin, umo, clock, date, same_day_only):
    p = plugin.runner
    p.update_from_message(umo, f"我{date}下午三点开会")
    assert not p._appointments(umo)
    assert plugin.store.get(umo, "active_event") is None
    assert p.take_llm_candidate(umo) is None
    rows = [x for x in p.decisions.items(umo) if x["rule"] == "F1"]
    assert rows and all(x["action"] == "dropped" and not x["touched"] for x in rows)


@pytest.mark.parametrize("text", [
    "我11月4日周一下午三点开会", "我周一（11月4日）下午三点开会",
])
def test_conflicting_calendar_and_weekday_does_not_become_today(plugin, umo, clock, text):
    p = plugin.runner
    p.update_from_message(umo, text)
    assert not p._appointments(umo)
    assert p.take_llm_candidate(umo) is None
    rows = [x for x in p.decisions.items(umo) if x["rule"] == "F1"]
    assert rows and all(x["action"] == "dropped" and not x["touched"] for x in rows)


@pytest.mark.parametrize("text", [
    "11月4日，我下午三点开会", "2027年11月2日，我下午三点开会", "周三，我下午三点开会",
    "今天决定11月4日下午三点开会", "我今天决定下周一下午三点开会",
    "我下午三点开会改到11月4日", "我下午三点开会改到下周一",
])
def test_explicit_date_scope_rejects(plugin, umo, clock, text, same_day_only):
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize("text", [
    "我明天下午三点开会改到11月2日", "我明天下午三点开会改到周一",
    "我11月4日下午三点开会改到今天", "我11月4日下午三点开会改到11月2日",
    "我今天下午三点开会",
])
def test_explicit_today_correction(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert [x["time_text"] for x in plugin.runner._appointments(umo)] == ["15:00"]


@pytest.mark.parametrize("text", ["我周一一点到家", "我星期一一点半到家", "我11月2日一点到家"])
def test_weekday_does_not_consume_hour_digit(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    items = plugin.runner._appointments(umo)
    assert len(items) == 1
    assert items[0]["time_text"] in ("13:00", "13:30")


@pytest.mark.parametrize("date", ["11月2日", "2026年11月2日", "周一"])
async def test_today_remaining_minutes_control(plugin, umo, clock, date):
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="YES")))
    await send(plugin, umo, f"我{date}还有十分钟下课")
    assert [x["time_text"] for x in plugin.runner._appointments(umo)] == ["10:10"]


@pytest.mark.parametrize("date", ["11月4日", "2月30日", "周三", "下周一"])
async def test_other_day_relative_minutes_never_calls_provider(plugin, umo, clock, date):
    chat = AsyncMock(return_value=SimpleNamespace(completion_text="YES"))
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=chat)
    await send(plugin, umo, f"我{date}还有十分钟下课")
    chat.assert_not_awaited()
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize("date", ["11月4日", "2月30日", "周三"])
async def test_rejected_date_leaves_current_activity(plugin, umo, clock, monkeypatch, date, same_day_only):
    classifier = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classifier)
    await send(plugin, umo, f"我{date}下午三点开会，我现在去冲个澡")
    assert not plugin.runner._appointments(umo)
    assert plugin.store.get(umo, "active_event")["name"] == "shower"
    assert classifier.await_args.args[1] == "我现在去冲个澡"


@pytest.mark.parametrize("date", ["11月2日", "2026年11月2日", "周一", "星期一"])
@pytest.mark.parametrize("stale", [False, True])
def test_same_day_cancel_snapshot_undo(plugin, umo, clock, date, stale):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    before = r._appointments(umo)
    r.update_from_message(umo, f"我{date}下午三点的会议取消掉")
    assert not r._appointments(umo)
    rec = next(x for x in r.decisions.items(umo) if x["rule"] == "CANCEL")
    assert rec["action"] == "applied" and rec["touched"]
    if stale:
        clock.now += 1
        r.update_from_message(umo, "我下午四点下课")
        newer = copy.deepcopy(r._appointments(umo))
        with pytest.raises(ValueError):
            r.undo_decision(rec["id"])
        assert r._appointments(umo) == newer
    else:
        r.undo_decision(rec["id"])
        assert r._appointments(umo) == before


@pytest.mark.parametrize("text", [
    "我11月4日下午三点的会议取消了", "我周三下午三点的会议取消了",
    "我2月30日下午三点的会议取消了", "我下周一下午三点的会议取消了",
    "他说11月2日下午三点的会议取消了", "不要取消11月2日下午三点的会议",
    "如果11月2日下午三点的会议取消了", "11月2日下午三点的会议取消了吗",
    "11月2日下午三点的牙医预约取消了",
])
def test_cancel_date_and_semantic_guards(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我下午三点开会")
    before = r._appointments(umo)
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before


def test_dawn_cancel_and_undo(plugin, umo, clock):
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    r = plugin.runner
    r.update_from_message(umo, "凌晨十二点到家")
    before = r._appointments(umo)
    assert datetime.fromtimestamp(before[0]["at"]) == datetime(2026, 11, 3)
    r.update_from_message(umo, "凌晨十二点到家的提醒不用了")
    assert not r._appointments(umo)
    rec = next(x for x in r.decisions.items(umo) if x["rule"] == "CANCEL")
    r.undo_decision(rec["id"])
    assert r._appointments(umo) == before


def test_explicit_today_does_not_roll_past_clock_into_tomorrow(plugin, umo, clock):
    plugin.engine_cfg["appointment_max_hours"] = 48
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    plugin.runner.update_from_message(umo, "我11月2日上午十二点到家")
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("date", ["11月4日", "周三"])
@pytest.mark.parametrize("prefix", ["", "现在十点了，"])
async def test_other_day_current_clock_cannot_become_current_activity(plugin, umo, clock, enabled, date, prefix, same_day_only):
    plugin.engine_cfg["enable_appointment"] = enabled
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="YES")))
    await send(plugin, umo, prefix + f"我{date}十点去开会")
    assert not plugin.runner._appointments(umo)
    assert plugin.store.get(umo, "active_event") is None


@pytest.mark.parametrize("period", ["今晚", "今天晚上", "11月2日晚上", "周一晚上"])
def test_adjacent_pure_day_and_period(plugin, umo, clock, period):
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    plugin.runner.update_from_message(umo, f"{period}，我预计会在一点到家")
    items = plugin.runner._appointments(umo)
    assert len(items) == 1
    assert datetime.fromtimestamp(items[0]["at"]) == datetime(2026, 11, 3, 1)


@pytest.mark.parametrize("period", ["明天晚上", "11月4日晚上", "周三晚上"])
def test_adjacent_non_today_period_rejects(plugin, umo, clock, period, same_day_only):
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    plugin.runner.update_from_message(umo, f"{period}，我一点到家")
    assert not plugin.runner._appointments(umo)


def test_adjacent_today_dawn_does_not_roll_to_tomorrow(plugin, umo, clock):
    clock.now = datetime(2026, 11, 2, 22).timestamp()
    plugin.runner.update_from_message(umo, "11月2日，凌晨十二点到家")
    assert not plugin.runner._appointments(umo)
