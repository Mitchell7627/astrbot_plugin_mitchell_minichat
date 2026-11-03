"""Date ownership and quantity/clock interpretation, including prior audit contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain
from astrbot_plugin_mitchell_minichat.core.appointment import find_clock


def msg(text, umo):
    return SimpleNamespace(unified_msg_origin=umo, get_messages=lambda: [Plain(text)], message_str=text)


async def send(plugin, umo, text):
    await plugin.on_private_message(msg(text, umo))
    if plugin._bg_tasks:
        await asyncio.gather(*list(plugin._bg_tasks))


@pytest.mark.parametrize('text', ['一点办法', '一点兴趣', '一点精力', '一点水', '一点饭', '一点空', '一点建议', '一点想法'])
def test_quantity_nouns(text):
    assert find_clock(text) is None


@pytest.mark.parametrize('text', ['我今天决定明天下午三点开会', '我今天确认后天下午三点开会', '今天刚定好明天下午三点开会'])
def test_explicit_later_day_with_today_in_same_clause(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('text', ['理发预约下午三点取消了', '牙医预约下午三点取消了', '体检预约下午三点取消了'])
def test_unknown_named_item_before_clock(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, '我下午三点开会')
    before = plugin.runner._appointments(umo)
    plugin.runner.update_from_message(umo, text)
    assert plugin.runner._appointments(umo) == before


@pytest.mark.parametrize('text', ['不打算取消下午三点的会议', '没打算取消下午三点的会议', '没有要取消下午三点的会议', '不是说要取消下午三点的会议', '下午三点的会议取消失败了', '下午三点的会议取消不了'])
def test_cancellation_polarity_more_forms(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, '我下午三点开会')
    before = plugin.runner._appointments(umo)
    plugin.runner.update_from_message(umo, text)
    assert plugin.runner._appointments(umo) == before


@pytest.mark.parametrize('text', ['下午三点的这个约定取消了', '下午三点的那个安排取消了', '下午三点的所有约定取消了'])
def test_generic_cancel_determiners(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, '我下午三点开会')
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('prefix', ['现在十点了', '两点天气不错', '你两点开会吗'])
def test_appointment_disabled_must_not_make_future_event_current(plugin, umo, clock, prefix):
    plugin.engine_cfg['enable_appointment'] = False
    plugin.runner.update_from_message(umo, prefix + '，我下午三点开会')
    assert plugin.store.get(umo, 'active_event') is None
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('prefix', ['现在十点了', '两点天气不错'])
def test_pending_future_protection_with_quota_full(plugin, umo, clock, prefix):
    plugin.engine_cfg['appointment_max_per_day'] = 1
    plugin.runner.update_from_message(umo, '我十一点下课')
    plugin.runner.update_from_message(umo, prefix + '，我下午三点开会')
    assert len(plugin.runner._appointments(umo)) == 1
    assert plugin.store.get(umo, 'active_event') is None


@pytest.mark.parametrize('text', ['我不去冲个澡', '他说我去冲个澡', '如果我去冲个澡', '明天我去冲个澡', '我去冲个澡吗'])
async def test_noncurrent_fallback_controls(plugin, umo, clock, monkeypatch, text):
    classifier = AsyncMock(return_value={'name': 'shower', 'done': False})
    monkeypatch.setattr(plugin.runner, 'classify_text', classifier)
    await send(plugin, umo, text)
    classifier.assert_not_awaited()
    assert plugin.store.get(umo, 'active_event') is None


@pytest.mark.parametrize('text', ['现在十点了，我下午三点开会', '你两点开会吗，我下午三点开会', '快一点，我下午三点开会'])
def test_fixed_clock_controls(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert [a['time_text'] for a in plugin.runner._appointments(umo)] == ['15:00']


@pytest.mark.parametrize('text', ['我去冲个澡，稍微快一点', '我还有一点时间去冲个澡'])
async def test_fixed_quantity_activity_controls(plugin, umo, clock, monkeypatch, text):
    monkeypatch.setattr(plugin.runner, 'classify_text', AsyncMock(return_value={'name': 'shower', 'done': False}))
    await send(plugin, umo, text)
    assert plugin.store.get(umo, 'active_event')['name'] == 'shower'


@pytest.mark.parametrize('text', ['我正在洗澡', '我正在吃饭', '我正在开会'])
def test_current_activity_controls(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert plugin.store.get(umo, 'active_event') is not None

@pytest.mark.parametrize("text", [
    "一点动力", "一点勇气", "一点耐心", "一点余地", "一点糖", "一点咖啡", "一点知识", "一点快乐",
    "喝一点水", "吃一点饭", "给我一点开会的时间", "我还有一点运动的精力",
])
def test_more_quantity_structures(text):
    assert find_clock(text) is None


@pytest.mark.parametrize("text,h,m", [
    ("一点开会", 1, 0), ("一点半下课", 1, 30), ("下午一点到家", 1, 0),
    ("一点要开会", 1, 0), ("一点我去开会", 1, 0), ("一点洗澡", 1, 0),
    ("1:30下课", 1, 30), ("一点一刻下课", 1, 15),
    ("加一点糖，我下午三点开会", 3, 0),
])
def test_clock_structure_controls(text, h, m):
    clock = find_clock(text)
    assert clock is not None and (clock["h"], clock["m"]) == (h, m)


@pytest.mark.parametrize("text", [
    "我今天决定明天下午三点开会", "明天，我下午三点开会",
    "我今天确认后天下午三点开会", "我今早确定明晚三点开会",
    "我今天决定下周下午三点开会", "我昨天决定明天下午三点开会",
    "我今天下午三点开会改到明天",
])
def test_unsupported_day_has_truthful_reject_record(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo)
    records = [x for x in plugin.runner.decisions.items(umo) if x["rule"] == "F1"]
    assert records and all(x["action"] == "dropped" and not x["touched"] and "未登记" in x["reason"] for x in records)


@pytest.mark.parametrize("text", [
    "我今天下午三点开会", "明天的开会改到今天下午三点",
    "我明天下午三点开会改到今天",
])
def test_today_and_explicit_day_correction_controls(plugin, umo, clock, text):
    plugin.runner.update_from_message(umo, text)
    assert [a["time_text"] for a in plugin.runner._appointments(umo)] == ["15:00"]


@pytest.mark.parametrize("first", [True, False])
async def test_rejected_date_does_not_starve_current_activity(plugin, umo, clock, monkeypatch, first):
    classifier = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classifier)
    parts = ["我今天决定明天下午三点开会", "我现在去冲个澡"]
    await send(plugin, umo, "，".join(parts if first else list(reversed(parts))))
    assert not plugin.runner._appointments(umo)
    assert plugin.store.get(umo, "active_event")["name"] == "shower"
    assert classifier.await_args.args[1] == "我现在去冲个澡"


@pytest.mark.parametrize("quantity", ["精力", "勇气", "动力", "时间"])
async def test_quantity_does_not_starve_current_activity(plugin, umo, clock, monkeypatch, quantity):
    classifier = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classifier)
    await send(plugin, umo, f"我还有一点{quantity}去冲个澡")
    classifier.assert_awaited_once()
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


@pytest.mark.parametrize("text", [
    "我不去冲个澡，还有一点精力", "他说“我还有一点精力去冲个澡”",
    "我妈还有一点精力去冲个澡", "如果我还有一点精力去冲个澡",
    "我明天还有一点精力去冲个澡", "我还有一点精力去冲个澡吗",
])
async def test_quantity_does_not_bypass_activity_guards(plugin, umo, clock, monkeypatch, text):
    classifier = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classifier)
    await send(plugin, umo, text)
    classifier.assert_not_awaited()
    assert plugin.store.get(umo, "active_event") is None


@pytest.mark.parametrize("text", [
    "我今天决定明天还有十分钟下课", "明天，还有十分钟下课",
])
async def test_future_relative_time_does_not_reach_gray_provider(plugin, umo, clock, text):
    provider = AsyncMock(return_value=SimpleNamespace(completion_text="YES"))
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=provider)
    await send(plugin, umo, text)
    provider.assert_not_awaited()
    assert not plugin.runner._appointments(umo)


async def test_today_relative_time_control(plugin, umo, clock):
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="YES")))
    await send(plugin, umo, "我今天还有十分钟下课")
    assert [a["time_text"] for a in plugin.runner._appointments(umo)] == ["10:10"]

