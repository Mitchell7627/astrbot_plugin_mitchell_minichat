"""Independent semantic edge cases. No changes to released or production code."""
import asyncio
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


def times(plugin,umo):
    return [(datetime.fromtimestamp(x['at']).strftime('%Y-%m-%d %H:%M'),x['label']) for x in plugin.runner._appointments(umo)]


@pytest.mark.parametrize('text,expected',[
    ('我下下周一下午三点开会','2026-11-16 15:00'),
    ('我下下星期一下午三点开会','2026-11-16 15:00'),
    ('我下下个星期一下午三点开会','2026-11-16 15:00'),
    ('我下下周二下午三点开会',None),
    ('我上上周一下午三点开会',None),
    ('我上上星期一下午三点开会',None),
    ('我下一个星期一下午三点开会','2026-11-09 15:00'),
    ('我本周三下午三点开会','2026-11-04 15:00'),
    ('我这周三下午三点开会','2026-11-04 15:00'),
    ('我十一月四日下午三点开会','2026-11-04 15:00'),
    ('我二〇二六年十一月四日下午三点开会','2026-11-04 15:00'),
    ('我11月4日（周三）下午三点开会','2026-11-04 15:00'),
    ('我11月4日（周一）下午三点开会',None),
    ('明天，我下午三点开会','2026-11-03 15:00'),
    ('明天，下午，三点开会','2026-11-03 15:00'),
    ('明天，下午三点，我要开会','2026-11-03 15:00'),
    ('我明天上午十三点到家',None),
    ('我明天凌晨十三点到家',None),
    ('我明天晚上十三点到家',None),
    ('我明天凌晨十二点到家','2026-11-03 00:00'),
    ('我明天上午十二点到家','2026-11-03 12:00'),
    ('我明晚十二点到家','2026-11-04 00:00'),
])
def test_explicit_date_and_period(plugin,umo,clock,text,expected):
    plugin.runner.update_from_message(umo,text)
    actual=times(plugin,umo)
    want=[] if expected is None else [(expected,'到家' if '到家' in text else '开会')]
    assert actual==want,{'input':text,'expected':want,'actual':actual}


@pytest.mark.parametrize('text,target',[
    ('我2026-11-04下午三点开会','2026-11-04 15:00'),
    ('我2026/11/4下午三点开会','2026-11-04 15:00'),
    ('我11/4下午三点开会','2026-11-04 15:00'),
    ('我11.4下午三点开会','2026-11-04 15:00'),
    ('我4号下午三点开会','2026-11-04 15:00'),
    ('我本月4号下午三点开会','2026-11-04 15:00'),
    ('我这个月4号下午三点开会','2026-11-04 15:00'),
    ('我两天后下午三点开会','2026-11-04 15:00'),
    ('我2天后下午三点开会','2026-11-04 15:00'),
    ('我过两天下午三点开会','2026-11-04 15:00'),
    ('我过2天下午三点开会','2026-11-04 15:00'),
    ('我下个月4号下午三点开会',None),
    ('我明年11月4日下午三点开会',None),
    ('我2026年13月1日下午三点开会',None),
])
def test_unsupported_date_never_silently_becomes_today(plugin,umo,clock,text,target):
    plugin.runner.update_from_message(umo,text)
    actual=times(plugin,umo)
    # 未宣称支持的格式可以拒绝，但不能登记成不同的日期。
    assert not actual or target is not None and all(t==target for t,_ in actual),{'input':text,'acceptable_date':target,'actual':actual}


@pytest.mark.parametrize('cancel',[
    '明天那个取消了','明天的那个取消掉','明天的约定取消了',
    '明天的会议取消了','明天下午三点的会议取消了','明天所有约定取消了',
])
def test_date_alone_uniquely_identifies_cancel_target(plugin,umo,clock,cancel):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    r.update_from_message(umo,'我后天下午四点开会')
    r.update_from_message(umo,cancel)
    assert times(plugin,umo)==[('2026-11-04 16:00','开会')],{'input':cancel,'actual':times(plugin,umo)}


@pytest.mark.parametrize('cancel',[
    '明天下午三点的会议不用取消','明天下午三点的会议取消不了',
    '我考虑取消明天下午三点的会议','你取消明天下午三点的会议了吗',
    '他说明天下午三点的会议取消了','如果明天下午三点的会议取消了',
    '明天下午三点的音乐会取消了',
])
def test_cancellation_negative_controls(plugin,umo,clock,cancel):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    before=times(plugin,umo)
    r.update_from_message(umo,cancel)
    assert times(plugin,umo)==before,{'input':cancel,'actual':times(plugin,umo)}


@pytest.mark.parametrize('change',[
    '会议改到后天下午四点开会','我明天下午三点开会改到后天下午四点',
])
def test_explicit_reschedule_does_not_keep_old_reminder(plugin,umo,clock,change):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    r.update_from_message(umo,change)
    assert times(plugin,umo)==[('2026-11-04 16:00','开会')],{'input':change,'actual':times(plugin,umo)}


@pytest.mark.parametrize('prefix',[
    '我明天下午三点开会','我下下周二下午三点开会',
    '我2026-11-04下午三点开会','我11月17日下午三点开会',
    '我2月30日下午三点开会','你明天下午三点开会吗',
])
async def test_date_handling_does_not_block_current_activity(plugin,umo,clock,monkeypatch,prefix):
    model=AsyncMock(return_value={'name':'shower','done':False})
    monkeypatch.setattr(plugin.runner,'classify_text',model)
    await send(plugin,umo,prefix+'，我现在去冲个澡')
    await drain(plugin)
    assert (plugin.store.get(umo,'active_event') or {}).get('name')=='shower',{'input':prefix,'actual':plugin.store.get(umo,'active_event')}


@pytest.mark.parametrize('text',[
    '我不去洗澡','我妈正在洗澡','他说“我去洗澡了”',
    '如果下雨我就去洗澡','我明天去洗澡','你去洗澡了吗',
])
async def test_protected_activities_never_reenter_model(plugin,umo,clock,monkeypatch,text):
    model=AsyncMock(return_value={'name':'shower','done':False})
    monkeypatch.setattr(plugin.runner,'classify_text',model)
    await send(plugin,umo,text)
    await drain(plugin)
    assert plugin.store.get(umo,'active_event') is None
    model.assert_not_awaited()


@pytest.mark.parametrize('tail',['好的','明白了','收到啦','我知道了'])
async def test_confirmation_after_request_started_does_not_drop_activity(plugin,umo,clock,monkeypatch,tail):
    entered,release=asyncio.Event(),asyncio.Event()
    async def slow(*a):
        if '冲个澡' not in a[1]:
            return {'name':'none','done':False}
        entered.set()
        await release.wait()
        return {'name':'shower','done':False}
    monkeypatch.setattr(plugin.runner,'classify_text',slow)
    await send(plugin,umo,'我准备去冲个澡了')
    await asyncio.wait_for(entered.wait(),2)
    await send(plugin,umo,tail)
    release.set()
    await drain(plugin)
    assert (plugin.store.get(umo,'active_event') or {}).get('name')=='shower',{'tail':tail,'active_event':plugin.store.get(umo,'active_event')}


@pytest.mark.parametrize('tail',[
    '明天吧','改天吧','晚点吧','等会儿吧','以后再说',
    '刚才说的是我室友','说的是他','这是引用',
])
async def test_short_correction_does_not_rebase_old_current_activity(plugin,umo,clock,monkeypatch,tail):
    model=AsyncMock(return_value={'name':'shower','done':False})
    monkeypatch.setattr(plugin.runner,'classify_text',model)
    await send(plugin,umo,'我准备去冲个澡了')
    await send(plugin,umo,tail)
    await drain(plugin)
    assert plugin.store.get(umo,'active_event') is None,{'tail':tail,'active_event':plugin.store.get(umo,'active_event')}
