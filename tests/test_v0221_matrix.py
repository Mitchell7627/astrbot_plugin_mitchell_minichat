import asyncio
import copy
from datetime import datetime
from unittest.mock import AsyncMock

import pytest


def queue(r, umo):
    return [(datetime.fromtimestamp(x['at']).strftime('%Y-%m-%d %H:%M'), x['kind']) for x in r._appointments(umo)]


@pytest.mark.parametrize('text', [
    '那个会改到明天下午四点吗', '如果那个会改到明天下午四点',
    '我妈那个会改到明天下午四点', '他说那个会改到明天下午四点',
    '不要把那个会改到明天下午四点', '别改到明天下午四点开会',
    '那个会改到明天下午四点失败了', '考虑把那个会改到明天下午四点',
    '那个会改到昨天下午四点', '那个会改到15天后下午四点',
    '那个会改到2月30日下午四点', '那个会改到明年下午四点',
    '音乐会改到明天下午四点', '快递改到明天下午四点',
    '那个会改到明天下午四点或者五点', '那个会改到明天下午四点左右',
    '那个会改到明天下午四点洗澡',
    '那个会改到凌晨十二点', '我没把那个会改到明天下午四点',
    '别把明天下午三点开会改到明天下午四点',
    '不要把明天下午三点开会改到明天下午四点',
    '考虑把明天下午三点开会改到明天下午四点',
    '明天下午三点开会改到明天下午四点失败了',
    '那个会改到明天下午四点，或者后天下午五点开会',
])
def test_reschedule_protection(plugin, umo, clock, text):
    clock.now = datetime(2026, 11, 3, 10).timestamp()
    r = plugin.runner
    r.update_from_message(umo, '明天下午三点开会')
    before = copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before, {'text': text, 'actual': queue(r, umo)}


@pytest.mark.parametrize('target,expected', [
    ('明天下午四点', '2026-11-04 16:00'), ('后天下午四点', '2026-11-05 16:00'),
    ('11月6日下午四点', '2026-11-06 16:00'), ('周五下午四点', '2026-11-06 16:00'),
    ('下午四点', '2026-11-04 16:00'), ('上午十点', '2026-11-04 10:00'),
])
def test_unique_named_reschedule_preserves_other_kind(plugin, umo, clock, target, expected):
    clock.now = datetime(2026, 11, 3, 10).timestamp()
    r = plugin.runner
    r.update_from_message(umo, '明早八点叫醒我')
    r.update_from_message(umo, '明天下午三点开会')
    r.update_from_message(umo, '那个会改到' + target)
    assert sorted(queue(r, umo)) == sorted([('2026-11-04 08:00', 'wake'), (expected, 'start')])


@pytest.mark.parametrize('text', ['改到下午四点', '那个会改到下午四点'])
def test_ambiguous_reschedule_preserves_both(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, '明天下午三点开会')
    r.update_from_message(umo, '后天下午三点开会')
    before = copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before


@pytest.mark.parametrize('text', [
    '明早八点不用叫醒我了', '不用明早八点叫醒我了', '明早八点不要叫醒我了',
    '明早八点别叫醒我', '明早八点不必喊醒我', '明早八点别叫我起床',
    '你明早八点不用叫醒我了',
])
def test_wake_cancel_variants(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, '明早八点叫醒我')
    r.update_from_message(umo, text)
    assert not r._appointments(umo), {'text': text, 'actual': queue(r, umo)}


@pytest.mark.parametrize('text', [
    '不要取消明早八点叫醒我', '不用取消明早八点叫醒我',
    '如果明早八点不用叫醒我', '明早八点不用叫醒我了吗',
    '我妈说不用明早八点叫醒我了', '明早八点不要叫醒我妈了',
    '明早八点别叫我妈起床', '明早八点叫醒取消失败了',
])
def test_wake_cancel_negative(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, '明早八点叫醒我')
    before = copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo, text)
    assert r._appointments(umo) == before, {'text': text, 'actual': queue(r, umo)}


@pytest.mark.parametrize('text', [
    '明天下午三点开会，后天下午四点开会，二选一',
    '明天下午三点开会，或者后天下午四点开会，或者大后天下午五点开会',
    '明天下午三点开会，或者后天再决定',
])
def test_alternative_scope(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, text)
    assert not r._appointments(umo), queue(r, umo)


async def test_two_failed_wakes_preserve_sleep_and_stop_retry(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    plugin.engine_cfg['sleep_quiet_hours'] = 4
    clock.now = datetime(2026, 11, 3, 16).timestamp()
    r.update_from_message(umo, '我去睡觉了')
    r.update_from_message(umo, '晚上六点叫醒我')
    clock.now = datetime(2026, 11, 3, 18).timestamp()
    monkeypatch.setattr(r, '_generate', AsyncMock(side_effect=RuntimeError('fake outage')))
    await r._fire(umo)
    clock.now += 60
    await r._fire(umo)
    assert r.is_sleeping(umo)
    assert r.appointment_shot_item(umo) is None


async def test_cancel_during_wake_generation_prevents_send(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    plugin.engine_cfg['sleep_quiet_hours'] = 4
    clock.now = datetime(2026, 11, 3, 16).timestamp()
    r.update_from_message(umo, '我去睡觉了')
    r.update_from_message(umo, '晚上六点叫醒我')
    clock.now = datetime(2026, 11, 3, 18).timestamp()
    entered, release = asyncio.Event(), asyncio.Event()
    async def generate(*a, **kw):
        entered.set()
        await release.wait()
        return '该起床啦'
    monkeypatch.setattr(r, '_generate', generate)
    task = asyncio.create_task(r._fire(umo))
    await asyncio.wait_for(entered.wait(), 2)
    r.update_from_message(umo, '晚上六点的叫醒取消')
    release.set()
    result = await task
    assert not result['sent']
    plugin.context.send_message.assert_not_awaited()
    assert r.is_sleeping(umo)


@pytest.mark.parametrize('wording', ['叫醒我', '喊醒我', '叫我起床', '喊我起床', '提醒我起床', '把我叫醒'])
@pytest.mark.parametrize('suffix', ['', '吧', '哦', '一下', '就行'])
def test_wake_polite_requests(plugin, umo, clock, wording, suffix):
    r = plugin.runner
    clock.now = datetime(2026, 11, 3, 22).timestamp()
    text = '明早八点' + wording + suffix
    r.update_from_message(umo, text)
    assert queue(r, umo) == [('2026-11-04 08:00', 'wake')], text


@pytest.mark.parametrize('operation', ['明早八点不要叫醒我了', '那个会改到明天下午四点'])
def test_new_operations_undo_and_stale_guard(plugin, umo, clock, operation):
    r = plugin.runner
    clock.now = datetime(2026, 11, 3, 10).timestamp()
    r.update_from_message(umo, '明早八点叫醒我')
    r.update_from_message(umo, '明天下午三点开会')
    before = queue(r, umo)
    r.update_from_message(umo, operation)
    assert queue(r, umo) != before
    records = [d for d in r.decisions.items(umo) if d['action'] == 'applied' and d.get('undo_state') == 'available']
    record = max(records, key=lambda d: d['id'])
    plugin.panel_service.task_action({'action': 'undo_decision', 'id': record['id']})
    assert queue(r, umo) == before
    clock.now += 1
    r.update_from_message(umo, operation)
    record = max([d for d in r.decisions.items(umo) if d['action'] == 'applied' and d.get('undo_state') == 'available'], key=lambda d: d['id'])
    r.update_from_message(umo, '后天下午五点到家')
    current = copy.deepcopy(r._snapshot(umo))
    from astrbot_plugin_mitchell_minichat.core.panel_api import PanelError
    with pytest.raises(PanelError, match='状态已经变了'):
        plugin.panel_service.task_action({'action': 'undo_decision', 'id': record['id']})
    assert r._snapshot(umo) == current
