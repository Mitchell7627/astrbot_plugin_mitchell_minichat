"""Safety boundaries for new temporal links, atomic changes and neutral async turns."""
import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain


async def send(plugin,umo,text):
    await plugin.on_private_message(SimpleNamespace(unified_msg_origin=umo,message_str=text,get_messages=lambda:[Plain(text)]))


async def drain(plugin):
    results=await asyncio.gather(*list(plugin._bg_tasks),return_exceptions=True)
    for result in results:
        if isinstance(result,BaseException) and not isinstance(result,asyncio.CancelledError):
            raise result


@pytest.mark.parametrize('tail',[
    '我妈要开会','你要开会吗','我不开会','如果我要开会',
    '他说“我要开会”','我梦见自己要开会','我后天要开会',
    '我改天要开会','我晚点要开会','我现在正在开会',
])
def test_new_temporal_link_does_not_override_action_context(plugin,umo,clock,tail):
    plugin.runner.update_from_message(umo,'明天，下午三点，'+tail)
    assert not plugin.runner._appointments(umo),tail


@pytest.mark.parametrize('text',[
    '我11/31下午三点开会','我2026-02-30下午三点开会',
    '我32号下午三点开会','我400天后下午三点开会',
    '我下下下周一下午三点开会','我上上周一下午三点开会',
    '我几天后下午三点开会','我过几天下午三点开会',
])
def test_new_date_formats_fail_closed(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo)
    assert plugin.store.get(umo,'active_event') is None


def test_reschedule_is_atomic_at_daily_limit(plugin,umo,clock):
    r=plugin.runner
    plugin.engine_cfg['appointment_max_per_day']=1
    r.update_from_message(umo,'我明天下午三点开会')
    before=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,'会议改到后天下午四点开会')
    assert r._appointments(umo)==before
    assert plugin.store.get(umo,'appointment_counter')['n']==1
    record=next(x for x in r.decisions.items(umo) if x['text']=='会议改到后天下午四点开会')
    assert record['action']=='dropped' and not record['touched']


def test_reschedule_is_atomic_when_destination_occupied(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    r.update_from_message(umo,'我后天下午四点到家')
    before=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,'会议改到后天下午四点开会')
    assert r._appointments(umo)==before


def test_reschedule_ambiguity_does_not_guess(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    r.update_from_message(umo,'我后天下午四点开会')
    before=copy.deepcopy(r._appointments(umo))
    r.update_from_message(umo,'会议改到大后天下午五点开会')
    assert r._appointments(umo)==before


def test_reschedule_exact_target_and_undo_then_stale_undo(plugin,umo,clock):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    r.update_from_message(umo,'我后天下午四点开会')
    before=copy.deepcopy(r._appointments(umo))
    text='我明天下午三点开会改到大后天下午五点'
    r.update_from_message(umo,text)
    items=r._appointments(umo)
    assert [datetime.fromtimestamp(x['at']) for x in items]==[datetime(2026,11,4,16),datetime(2026,11,5,17)]
    rec=next(x for x in r.decisions.items(umo) if x['text']==text and x['undo_state']=='available')
    r.undo_decision(rec['id'])
    assert r._appointments(umo)==before
    clock.now+=1
    r.update_from_message(umo,text)
    rec=next(x for x in r.decisions.items(umo) if x['text']==text and x['undo_state']=='available')
    r.update_from_message(umo,'所有约定取消了')
    with pytest.raises(ValueError):
        r.undo_decision(rec['id'])
    assert not r._appointments(umo)


@pytest.mark.parametrize('tail',['我正在吃饭','我去睡觉了','明天吧','刚才说的是我室友'])
async def test_real_changes_invalidate_inflight_after_neutral_ack(plugin,umo,clock,monkeypatch,tail):
    started,release=asyncio.Event(),asyncio.Event()
    async def classify(*a):
        started.set()
        await release.wait()
        return {'name':'shower','done':False}
    monkeypatch.setattr(plugin.runner,'classify_text',classify)
    await send(plugin,umo,'我准备去冲个澡了')
    await asyncio.wait_for(started.wait(),2)
    await send(plugin,umo,'好的')
    await send(plugin,umo,tail)
    release.set()
    await drain(plugin)
    assert (plugin.store.get(umo,'active_event') or {}).get('name')!='shower'


async def test_gray_appointment_survives_neutral_ack(plugin,umo,clock):
    started,release=asyncio.Event(),asyncio.Event()
    async def answer(**kw):
        started.set()
        await release.wait()
        return SimpleNamespace(completion_text='YES')
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=answer)
    await send(plugin,umo,'我明天下午三点左右开会')
    await asyncio.wait_for(started.wait(),2)
    await send(plugin,umo,'好的')
    release.set()
    await drain(plugin)
    assert datetime.fromtimestamp(plugin.runner._appointments(umo)[0]['at'])==datetime(2026,11,3,15)


async def test_custom_ack_keyword_is_meaningful(plugin,umo,clock,monkeypatch):
    rule=plugin.runner.engine.get_rule('eating')
    rule['keywords'].append('知道了')
    # 配置词进入灰区也必须使旧候选失效，不能按通用确认续用旧请求。
    review=AsyncMock()
    monkeypatch.setattr(plugin.runner,'review_gray',review)
    await send(plugin,umo,'我知道了')
    await drain(plugin)
    assert not plugin.runner.neutral_turn(umo)
    review.assert_awaited_once()
