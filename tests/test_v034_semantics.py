import asyncio
import copy
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain


def queue(p,u):
    return sorted(datetime.fromtimestamp(a['at']).strftime('%Y-%m-%d %H:%M') for a in p.runner._appointments(u))


async def send(p,u,t):
    await p.on_private_message(SimpleNamespace(unified_msg_origin=u,message_str=t,get_messages=lambda:[Plain(t)]))
    for r in await asyncio.gather(*list(p._bg_tasks),return_exceptions=True):
        if isinstance(r,BaseException) and not isinstance(r,asyncio.CancelledError):raise r


def observe(kind,text,expected,actual):
    print('OBS '+json.dumps(dict(kind=kind,text=text,expected=expected,actual=actual),ensure_ascii=True))


@pytest.mark.parametrize('now,text,expected',[
 ('2026-11-30 10:00','我12月1日下午三点开会',['2026-12-01 15:00']),
 ('2026-12-31 10:00','我明天下午三点开会',['2027-01-01 15:00']),
 ('2026-12-31 10:00','我2027年1月1日下午三点开会',['2027-01-01 15:00']),
 ('2028-02-28 10:00','我2月29日下午三点开会',['2028-02-29 15:00']),
 ('2026-02-28 10:00','我2月29日下午三点开会',[]),
 ('2026-11-02 10:00','我11月16日下午三点开会',['2026-11-16 15:00']),
 ('2026-11-02 10:00','我11月17日下午三点开会',[]),
 ('2026-11-02 10:00','我明天下午三点开会，后天下午四点下课',['2026-11-03 15:00','2026-11-04 16:00']),
 ('2026-11-02 10:00','明天下午三点开会，或者后天下午四点开会',[]),
 ('2026-11-02 10:00','明天或者后天下午三点开会',[]),
 ('2026-11-02 10:00','我明天下午25点开会',[]),
 ('2026-11-02 10:00','我明天下午三点七十分开会',[]),
 ('2026-11-02 10:00','我明天15:70开会',[]),
 ('2026-11-02 10:00','我明天15:00开会',['2026-11-03 15:00']),
 ('2026-11-02 22:00','我明天凌晨十二点到家',['2026-11-03 00:00']),
 ('2026-11-02 22:00','我后天凌晨十二点到家',['2026-11-04 00:00']),
])
def test_calendar_and_multiple_clocks(plugin,umo,clock,now,text,expected):
    clock.now=datetime.strptime(now,'%Y-%m-%d %H:%M').timestamp()
    plugin.runner.update_from_message(umo,text)
    actual=queue(plugin,umo);observe('date',text,expected,actual)
    assert actual==expected


@pytest.mark.parametrize('text,remaining',[
 ('取消明天下午三点的会议',['2026-11-03 16:00','2026-11-04 15:00']),
 ('取消后天下午三点的会议',['2026-11-03 15:00','2026-11-03 16:00']),
 ('明天的会议取消了',['2026-11-03 16:00','2026-11-04 15:00']),
 ('取消明天的会议',['2026-11-03 16:00','2026-11-04 15:00']),
 ('明天下午三点的会议不用提醒我了',['2026-11-03 16:00','2026-11-04 15:00']),
 ('后天的会议不用提醒我了',['2026-11-03 15:00','2026-11-03 16:00']),
 ('不要取消明天的会议',None),
 ('明天的会议取消了吗',None),
 ('我妈说取消明天的会议',None),
 ('如果明天的会议取消了',None),
 ('明天下午三点的理发预约取消了',None),
 ('明天的会议取消失败了',None),
])
def test_dated_cancellation_scope_and_undo(plugin,umo,clock,text,remaining):
    r=plugin.runner
    for t in ('明天下午三点开会','明天下午四点下课','后天下午三点开会'):r.update_from_message(umo,t)
    before=copy.deepcopy(r._appointments(umo))
    expected=queue(plugin,umo) if remaining is None else remaining
    r.update_from_message(umo,text)
    actual=queue(plugin,umo);observe('cancel',text,expected,actual)
    assert actual==expected
    if remaining is not None:
        rec=next(x for x in r.decisions.items(umo) if x['rule']=='CANCEL' and x['action']=='applied')
        r.undo_decision(rec['id']);assert r._appointments(umo)==before


@pytest.mark.parametrize('text',[
 '我不确定明天下午三点开会',
 '明天下午三点可能开会',
 '明天下午三点不一定开会',
 '还没决定明天下午三点开会',
 '我本来明天下午三点开会，现在不用去了',
 '朋友说：明天下午三点开会',
 '我说“明天下午三点开会”只是举个例子',
 '我原计划明天下午三点开会，但是取消了',
 '我明天下午三点开会吗',
])
async def test_unconfirmed_or_cancelled_plan_is_not_registered(plugin,umo,clock,monkeypatch,text):
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='NO')))
    monkeypatch.setattr(plugin.runner,'classify_text',AsyncMock(return_value=None))
    monkeypatch.setattr(plugin.runner,'_recent_dialogue',AsyncMock(return_value=''))
    await send(plugin,umo,text)
    actual=queue(plugin,umo);observe('unconfirmed',text,[],actual)
    assert not actual


@pytest.mark.parametrize('text',[
 '我不去洗澡，明天下午三点开会',
 '他说他去洗澡，明天下午三点开会',
 '如果我去洗澡，明天下午三点开会',
 '明天下午三点开会，我不去洗澡',
])
async def test_protected_activity_does_not_block_independent_appointment(plugin,umo,clock,monkeypatch,text):
    plugin.context.get_using_provider=lambda **kw:None
    monkeypatch.setattr(plugin.runner,'classify_text',AsyncMock(return_value={'name':'shower','done':False}))
    await send(plugin,umo,text)
    observe('cross',text,['2026-11-03 15:00'],queue(plugin,umo))
    assert queue(plugin,umo)==['2026-11-03 15:00']
    assert plugin.store.get(umo,'active_event') is None
