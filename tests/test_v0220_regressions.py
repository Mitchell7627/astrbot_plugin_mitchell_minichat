"""New probes after v0.2.19, including the real missed wake-up request."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain


async def send(plugin,umo,text):
    await plugin.on_private_message(SimpleNamespace(unified_msg_origin=umo,message_str=text,get_messages=lambda:[Plain(text)]))


async def drain(plugin):
    values=await asyncio.gather(*list(plugin._bg_tasks),return_exceptions=True)
    for value in values:
        if isinstance(value,BaseException) and not isinstance(value,asyncio.CancelledError):raise value


def times(plugin,umo):
    return [(datetime.fromtimestamp(x['at']).strftime('%Y-%m-%d %H:%M'),x['label']) for x in plugin.runner._appointments(umo)]


@pytest.mark.parametrize('text',[
    '六点再起来','晚上六点再起来','晚上六点起床','晚上六点叫醒我',
])
def test_wake_request_expected_by_live_user(plugin,umo,clock,text):
    clock.now=datetime(2026,11,3,16,37,37).timestamp()
    plugin.runner.update_from_message(umo,text)
    assert any(datetime.fromtimestamp(x['at'])==datetime(2026,11,3,18) for x in plugin.runner._appointments(umo)),{'input':text,'appointments':times(plugin,umo),'decisions':[(d['rule'],d['reason']) for d in plugin.runner.decisions.items(umo)]}


@pytest.mark.parametrize('text',['六点再起来','晚上六点再起来','晚上六点起床'])
def test_adding_wake_keyword_alone_must_not_be_claimed_as_solution(plugin,umo,clock,text):
    clock.now=datetime(2026,11,3,16,37,37).timestamp()
    plugin.engine_cfg['appointment_keywords']=['起床','起来','叫醒']
    r=plugin.runner
    r.update_from_message(umo,'我去睡觉了')
    before=r._sleep_state(umo)
    assert before and before['state']=='sleeping'
    r.update_from_message(umo,text)
    items=r._appointments(umo)
    assert items and datetime.fromtimestamp(items[0]['at'])==datetime(2026,11,3,18),{'input':text,'appointments':times(plugin,umo),'sleep':r._sleep_state(umo),'decisions':[(d['rule'],d['reason']) for d in r.decisions.items(umo)]}
    assert r._sleep_state(umo),{'input':text,'error':'wake plan immediately cleared sleeping'}


async def test_naive_wake_keyword_still_deferred_by_sleep(plugin,umo,clock):
    clock.now=datetime(2026,11,3,16,37,37).timestamp()
    plugin.engine_cfg['appointment_keywords']=['叫醒']
    plugin.engine_cfg['sleep_quiet_hours']=2
    r=plugin.runner
    r.update_from_message(umo,'我去睡觉了')
    r.update_from_message(umo,'晚上六点叫醒我')
    assert r._appointments(umo)
    original=r._appointments(umo)[0]['at']
    clock.now=original
    await r._fire(umo)
    assert not any(x.get('deferred') for x in r._appointments(umo)),{'original':datetime.fromtimestamp(original).isoformat(),'actual':times(plugin,umo),'sleep':r._sleep_state(umo)}


@pytest.mark.parametrize('text',[
    '我两天前下午三点开会','我2天前下午三点开会','我前两天下午三点开会',
    '我一周后下午三点开会','我两周后下午三点开会',
    '我26/11/4下午三点开会','我２０２６／１１／４下午三点开会',
])
def test_other_date_shapes_never_silently_register_today(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    actual=times(plugin,umo)
    assert not actual or all(t[:10]!='2026-11-02' for t,_ in actual),{'input':text,'actual':actual}


@pytest.mark.parametrize('text',[
    '我明天或者后天下午三点开会','我明天或后天下午三点开会',
    '我明天下午三点或者四点开会','我明天下午三点或四点开会',
])
def test_alternative_dates_and_clocks_do_not_guess_committed_appointment(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo),{'input':text,'actual':times(plugin,umo)}


@pytest.mark.parametrize('cancel',[
    '我妈2026.11.3下午三点的会议取消了','如果2026.11.3下午三点的会议取消了',
    '你2026.11.3下午三点的会议取消了吗','不要取消2026.11.3下午三点的会议',
    '2026.11.3下午三点的会议取消失败了',
])
def test_numeric_date_punctuation_never_weakens_cancel_guards(plugin,umo,clock,cancel):
    r=plugin.runner
    r.update_from_message(umo,'我明天下午三点开会')
    before=times(plugin,umo)
    r.update_from_message(umo,cancel)
    assert times(plugin,umo)==before,{'input':cancel,'actual':times(plugin,umo)}


@pytest.mark.parametrize('shape',['11月3日','2026-11-03','2026/11/3','2026.11.3','11/3','3号'])
def test_supported_date_cancel_round_trip(plugin,umo,clock,shape):
    r=plugin.runner
    r.update_from_message(umo,'我'+shape+'下午三点开会')
    assert times(plugin,umo)==[('2026-11-03 15:00','开会')]
    r.update_from_message(umo,shape+'下午三点的会议取消了')
    assert not r._appointments(umo)


@pytest.mark.parametrize('tail',['明天吧','改天吧','刚才说的是我室友','这是引用'])
async def test_v19_short_corrections_stay_fixed(plugin,umo,clock,monkeypatch,tail):
    model=AsyncMock(return_value={'name':'shower','done':False})
    monkeypatch.setattr(plugin.runner,'classify_text',model)
    await send(plugin,umo,'我准备去冲个澡了')
    await send(plugin,umo,tail)
    await drain(plugin)
    assert plugin.store.get(umo,'active_event') is None


async def test_confirmation_chain_then_actual_change_stays_safe(plugin,umo,clock,monkeypatch):
    entered,release=asyncio.Event(),asyncio.Event()
    async def classify(*a):
        entered.set();await release.wait();return {'name':'shower','done':False}
    monkeypatch.setattr(plugin.runner,'classify_text',classify)
    await send(plugin,umo,'我准备去冲个澡了')
    await asyncio.wait_for(entered.wait(),2)
    for text in ('好的','我知道了','收到','我正在吃饭'):
        await send(plugin,umo,text)
    release.set()
    await drain(plugin)
    assert plugin.store.get(umo,'active_event')['name']=='eating'


async def test_panel_clear_event_not_resurrected_by_inflight_classifier(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    r.update_from_message(umo,'我正在吃饭')
    entered,release=asyncio.Event(),asyncio.Event()
    async def classify(*a):
        entered.set();await release.wait();return {'name':'shower','done':False}
    monkeypatch.setattr(r,'classify_text',classify)
    await send(plugin,umo,'我准备去冲个澡了')
    await asyncio.wait_for(entered.wait(),2)
    plugin.panel_service.task_action({'umo':umo,'action':'clear_event'})
    assert plugin.store.get(umo,'active_event') is None
    release.set()
    await drain(plugin)
    assert plugin.store.get(umo,'active_event') is None,{'error':'manual clear overwritten by outstanding classifier','active':plugin.store.get(umo,'active_event')}
@pytest.mark.parametrize('text',['我妈晚上六点起床','他说“晚上六点叫醒我”','如果晚上六点起床','晚上六点起床吗','晚上六点玩起来'])
def test_wake_guards(plugin,umo,clock,text):
    clock.now=datetime(2026,11,3,16,37).timestamp()
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo)

async def test_wake_delivers_once_without_early_wakeup(plugin,umo,clock,monkeypatch):
    from astrbot_plugin_mitchell_minichat.core.appointment import stage_of
    clock.now=datetime(2026,11,3,16,37).timestamp()
    r=plugin.runner
    plugin.engine_cfg['sleep_quiet_hours']=2
    r.update_from_message(umo,'我去睡觉了')
    r.update_from_message(umo,'晚上六点叫醒我')
    ap=r._appointments(umo)[0]
    assert ap['kind']=='wake' and r.is_sleeping(umo)
    assert stage_of(ap,ap['at']-1,r._appointment_cfg()) is None
    monkeypatch.setattr(r,'_generate',AsyncMock(return_value='该起床啦'))
    clock.now=ap['at']
    result=await r._fire(umo)
    assert result['sent'] and not r.is_sleeping(umo)
    plugin.context.send_message.assert_awaited()
    assert not any(x.get('deferred') for x in r._appointments(umo))
    assert not r.appointment_shot_item(umo)

@pytest.mark.parametrize('text',['六点起来了','六点已经起床','晚上六点玩起来'])
def test_completed_wake_not_registered(plugin,umo,clock,text):
    clock.now=datetime(2026,11,3,16,37).timestamp()
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo)

def test_explicit_request_to_bot(plugin,umo,clock):
    clock.now=datetime(2026,11,3,16,37).timestamp()
    plugin.runner.update_from_message(umo,'你晚上六点叫醒我')
    assert plugin.runner._appointments(umo)[0]['kind']=='wake'
