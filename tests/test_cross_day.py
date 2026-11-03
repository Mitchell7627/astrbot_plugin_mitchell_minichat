"""v0.2.18 default cross-day behavior, independent from explicit same-day compatibility tests."""
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
from astrbot.core.message.components import Plain
from astrbot_plugin_mitchell_minichat.core.appointment import stage_of, build_hint
from astrbot_plugin_mitchell_minichat.core.store import Store


@pytest.mark.parametrize("text,expected", [
    ("我明天下午三点开会", datetime(2026,11,3,15)),
    ("我今天决定明天下午三点开会", datetime(2026,11,3,15)),
    ("我后天下午三点开会", datetime(2026,11,4,15)),
    ("我大后天下午三点开会", datetime(2026,11,5,15)),
    ("我下周一下午三点开会", datetime(2026,11,9,15)),
    ("我下周日下午三点开会", datetime(2026,11,15,15)),
    ("我周三下午三点开会", datetime(2026,11,4,15)),
    ("我11月4日下午三点开会", datetime(2026,11,4,15)),
    ("我2026年11月4日下午三点开会", datetime(2026,11,4,15)),
    ("我明早八点到家", datetime(2026,11,3,8)),
    ("我明晚八点到家", datetime(2026,11,3,20)),
    ("我明晚一点到家", datetime(2026,11,4,1)),
    ("明天，我下午三点开会", datetime(2026,11,3,15)),
    ("11月4日晚上，我一点到家", datetime(2026,11,5,1)),
    ("我下午三点开会改到明天", datetime(2026,11,3,15)),
])
def test_default_cross_day_exact_date(plugin, umo, clock, text, expected):
    assert plugin.runner._appointment_cfg()["future_days"] == 14
    plugin.runner.update_from_message(umo,text)
    items=plugin.runner._appointments(umo)
    assert len(items)==1
    assert datetime.fromtimestamp(items[0]["at"])==expected
    assert items[0]["date_text"]==expected.strftime("%Y-%m-%d")
    assert plugin.store.get(umo,"active_event") is None


@pytest.mark.parametrize("text", [
    "我昨天下午三点开会","我上周一下午三点开会","我11月1日下午三点开会",
    "我2027年11月2日下午三点开会","我2月30日下午三点开会",
    "我11月17日下午三点开会","我改天下午三点开会",
    "我11月4日周一下午三点开会",
    "他说我明天下午三点开会","如果我明天下午三点开会","你明天下午三点开会吗",
])
def test_past_invalid_out_of_range_and_semantic_guards(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo)
    assert plugin.store.get(umo,"active_event") is None


def test_last_day_inclusive_and_first_day_outside(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,"我11月16日下午三点开会")
    assert len(r._appointments(umo))==1
    r.update_from_message(umo,"我11月17日下午三点开会")
    assert len(r._appointments(umo))==1


def test_explicit_year_crosses_new_year(plugin,umo,clock):
    clock.now=datetime(2026,12,31,10).timestamp()
    plugin.runner.update_from_message(umo,"我2027年1月1日下午三点开会")
    assert datetime.fromtimestamp(plugin.runner._appointments(umo)[0]["at"])==datetime(2027,1,1,15)


def test_bare_weekday_nearest_but_this_week_not_rolled(plugin,umo,clock):
    clock.now=datetime(2026,11,5,10).timestamp() # Thursday
    r=plugin.runner
    r.update_from_message(umo,"我本周三下午三点开会")
    assert not r._appointments(umo)
    r.update_from_message(umo,"我周三下午三点开会")
    assert datetime.fromtimestamp(r._appointments(umo)[0]["at"])==datetime(2026,11,11,15)


def test_future_end_does_not_extend_current_event(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,"我正在上课")
    before=copy.deepcopy(plugin.store.get(umo,"active_event"))
    r.update_from_message(umo,"我明天下午三点下课")
    assert plugin.store.get(umo,"active_event")==before
    assert r.appointment_shot_item(umo) is None


def test_due_after_date_arrives_and_survives_reload(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,"我明天下午三点开会")
    ap=r._appointments(umo)[0]
    restored=Store(plugin.data_dir)
    assert restored.get(umo,"appointments")==r._appointments(umo)
    cfg=r._appointment_cfg()
    assert stage_of(ap,clock.now,cfg) is None
    clock.now=ap["at"]-60
    assert stage_of(ap,clock.now,cfg)=="due"
    assert "2026-11-03" in build_hint(ap,"lead",datetime.fromtimestamp(clock.now))


@pytest.mark.parametrize("cancel,remaining",[
    ("明天下午三点的会议取消了",["2026-11-04"]),
    ("后天下午三点的会议取消了",["2026-11-03"]),
    ("下午三点的会议取消了",["2026-11-03","2026-11-04"]),
    ("所有约定取消了",[]),
])
def test_cancellation_compares_dates_and_ambiguity(plugin,umo,clock,cancel,remaining):
    r=plugin.runner
    r.update_from_message(umo,"我明天下午三点开会")
    r.update_from_message(umo,"我后天下午三点开会")
    r.update_from_message(umo,cancel)
    assert [x["date_text"] for x in r._appointments(umo)]==remaining


def test_future_cancel_and_undo(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,"我明天下午三点开会")
    before=r._appointments(umo)
    r.update_from_message(umo,"明天下午三点的那个取消掉")
    assert not r._appointments(umo)
    rec=next(x for x in r.decisions.items(umo) if x["rule"]=="CANCEL")
    r.undo_decision(rec["id"])
    assert r._appointments(umo)==before


def test_cross_day_quota_and_queue_bound(plugin,umo,clock):
    r=plugin.runner
    plugin.engine_cfg["appointment_max_per_day"]=1
    r.update_from_message(umo,"我明天下午三点开会")
    r.update_from_message(umo,"我后天下午三点开会")
    assert len(r._appointments(umo))==1


async def test_future_appointment_and_current_unknown_activity(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    classify=AsyncMock(return_value={"name":"shower","done":False})
    monkeypatch.setattr(r,"classify_text",classify)
    text="我明天下午三点开会，我现在去冲个澡"
    await plugin.on_private_message(SimpleNamespace(unified_msg_origin=umo,message_str=text,get_messages=lambda:[Plain(text)]))
    await asyncio.gather(*list(plugin._bg_tasks))
    assert len(r._appointments(umo))==1
    assert plugin.store.get(umo,"active_event")["name"]=="shower"

async def test_gray_reference_date_is_not_shifted_by_midnight(plugin,umo,clock):
    clock.now=datetime(2026,11,2,23,59,59).timestamp()
    started,release=asyncio.Event(),asyncio.Event()
    async def answer(**kw):
        started.set()
        await release.wait()
        return SimpleNamespace(completion_text="YES")
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=answer)
    text="我明天下午三点左右开会"
    await plugin.on_private_message(SimpleNamespace(unified_msg_origin=umo,message_str=text,get_messages=lambda:[Plain(text)]))
    await asyncio.wait_for(started.wait(),2)
    clock.now+=2
    release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    assert datetime.fromtimestamp(plugin.runner._appointments(umo)[0]["at"])==datetime(2026,11,3,15)

