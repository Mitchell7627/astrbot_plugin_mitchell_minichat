"""Cross-feature contracts: use real parser, runner, plugin entry and panel service.

Only external models and QQ delivery are controlled. Failures are intentionally
left failing when the unchanged deployed v0.2.11 violates the stated contract.
"""
import asyncio
import copy
import itertools
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import Plain


ACTIVITIES = [
    ('date','约会'),('eating','吃饭'),('cooking','做饭'),('drinking','喝奶茶'),
    ('commute','坐地铁'),('meeting','开会'),('working','写代码'),('shower','洗澡'),
    ('gaming','打游戏'),('errand','洗衣服'),('study','学习'),('movie','看电影'),
    ('shopping','逛街'),('hosp','看病'),('exercise','运动'),('nap','午睡'),
]


def active(plugin, umo):
    return (plugin.store.get(umo,'active_event') or {}).get('name')


def event(text, umo):
    return SimpleNamespace(unified_msg_origin=umo, get_messages=lambda:[Plain(text)], message_str=text)


@pytest.mark.parametrize('name,verb',ACTIVITIES)
@pytest.mark.parametrize('template',[
    '我现在正在{}','刚才你问我在干嘛，我现在正在{}',
    '听了朋友的话，我现在正在{}','我本来不想{0}，但我现在正在{0}',
])
def test_explicit_current_in_context(plugin,umo,clock,name,verb,template):
    text=template.format(verb)
    plugin.runner.update_from_message(umo,text)
    assert active(plugin,umo)==name, {'text':text,'state':plugin.store.data[umo]}


@pytest.mark.parametrize('name,verb',ACTIVITIES)
@pytest.mark.parametrize('template',[
    '我妈现在正在{}','你现在正在{}吗','昨天我正在{}','明天这个时候我正在{}',
    '他说“我现在正在{}”','我梦见自己正在{}','我现在没有在{}','如果今天下雨，我就去{}',
])
def test_non_current_or_other_subject_never_becomes_current(plugin,umo,clock,name,verb,template):
    text=template.format(verb)
    plugin.runner.update_from_message(umo,text)
    assert active(plugin,umo) is None, {'text':text,'active':active(plugin,umo)}


@pytest.mark.parametrize('other,current',list(itertools.product(ACTIVITIES,repeat=2)))
def test_other_person_clause_does_not_hide_current_action(plugin,umo,clock,other,current):
    text=f'我妈现在正在{other[1]}，我现在正在{current[1]}'
    plugin.runner.update_from_message(umo,text)
    assert active(plugin,umo)==current[0], {'text':text,'active':active(plugin,umo)}


@pytest.mark.parametrize('name,verb',[x for x in ACTIVITIES if x[0]!='nap'])
@pytest.mark.parametrize('first',[False,True])
@pytest.mark.parametrize('enabled',[False,True])
def test_future_appointment_does_not_consume_current_activity(plugin,umo,clock,name,verb,first,enabled):
    plugin.engine_cfg['enable_appointment']=enabled
    clauses=[f'我现在正在{verb}','我下午三点开会']
    if first: clauses.reverse()
    text='，'.join(clauses)
    plugin.runner.update_from_message(umo,text)
    assert active(plugin,umo)==name, {'text':text,'state':plugin.store.data[umo]}
    assert len(plugin.runner._appointments(umo))==int(enabled)


@pytest.mark.parametrize('prefix,state',[('我先睡了','sleeping'),('我待会睡','waiting')])
@pytest.mark.parametrize('separator',['，','。','；'])
@pytest.mark.parametrize('first',[False,True])
def test_sleep_declaration_and_separate_future_appointment_both_survive(plugin,umo,clock,prefix,state,separator,first):
    clauses=[prefix,'我下午三点开会']
    if first: clauses.reverse()
    text=separator.join(clauses)
    plugin.runner.update_from_message(umo,text)
    assert (plugin.runner._sleep_state(umo) or {}).get('state')==state
    assert len(plugin.runner._appointments(umo))==1, {'text':text,'state':plugin.store.data[umo]}


@pytest.mark.parametrize('first',[False,True])
def test_sleep_and_appointment_separate_messages_control(plugin,umo,clock,first):
    clauses=['我先睡了','我下午三点开会']
    if first: clauses.reverse()
    for text in clauses: plugin.runner.update_from_message(umo,text)
    assert plugin.runner.is_sleeping(umo)
    assert len(plugin.runner._appointments(umo))==1


@pytest.mark.parametrize('text',['我到家了，晚安我睡了','我先睡了，我已经到家了','我到家了，待会睡'])
def test_sleep_does_not_hide_explicit_arrival(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,'我去公司上班了')
    assert plugin.store.get(umo,'away')['active']
    plugin.runner.update_from_message(umo,text)
    assert not (plugin.store.get(umo,'away') or {}).get('active'),plugin.store.data[umo]


@pytest.mark.parametrize('text',['我到家了','我到家了，我现在正在洗澡'])
def test_arrival_separate_sleep_message_control(plugin,umo,clock,text):
    for t in ['我去公司上班了',text,'我先睡了']: plugin.runner.update_from_message(umo,t)
    assert not (plugin.store.get(umo,'away') or {}).get('active')
    assert plugin.runner.is_sleeping(umo)


@pytest.mark.parametrize('base',['我正在坐地铁','我正在买菜'])
@pytest.mark.parametrize('text',[
    '我妈快到家了','你快到家了吗','我没有快到家','他说“我快到家了”',
    '我梦见自己快到家了','如果不堵车，我就快到家了','明天这个时候我快到家了',
])
def test_accelerator_obeys_subject_time_and_negation(plugin,umo,clock,base,text):
    r=plugin.runner
    r.update_from_message(umo,base)
    before=copy.deepcopy(plugin.store.get(umo,'active_event'))
    assert before and before['name'] in r.RETURN_SOON_BOOSTABLE
    r.update_from_message(umo,text)
    after=plugin.store.get(umo,'active_event')
    assert after==before, {'text':text,'before':before,'after':after}


@pytest.mark.parametrize('text',['我快到家了','我马上到了'])
def test_real_accelerator_control(plugin,umo,clock,text):
    r=plugin.runner
    r.update_from_message(umo,'我正在坐地铁')
    before=copy.deepcopy(plugin.store.get(umo,'active_event'))
    # Use a configured literal keyword for the positive control.
    if text=='我马上到了': text='我马上到家了'
    r.update_from_message(umo,text)
    after=plugin.store.get(umo,'active_event')
    assert after['return_soon'] and after['expires_at']<before['expires_at']


@pytest.mark.parametrize('initial',['我去上课了','我去公司上班了','我出门了'])
@pytest.mark.parametrize('text',['我快到家了','我马上到家了'])
def test_almost_home_never_means_already_home_without_acceleratable_event(plugin,umo,clock,initial,text):
    r=plugin.runner
    r.update_from_message(umo,initial)
    assert (plugin.store.get(umo,'away') or {}).get('active')
    if initial=='我出门了':
        # 「出门」会创建 commute；推进到活动过期、away 仍有效，才是真正的
        # “只有离开记录”场景。旧测试在这里的两个前置断言本身有误。
        clock.now=plugin.store.get(umo,'active_event')['expires_at']+1
        r._sweep_expired_events()
    if active(plugin,umo) in r.RETURN_SOON_BOOSTABLE:
        pytest.fail('Test setup unexpectedly created an acceleratable event')
    r.update_from_message(umo,text)
    assert (plugin.store.get(umo,'away') or {}).get('active'), {'initial':initial,'message':text,'state':plugin.store.data[umo]}


@pytest.mark.parametrize('mode',['normal','gray','appointment_gray'])
async def test_removed_session_cannot_be_resurrected_by_delayed_classification(plugin,umo,clock,monkeypatch,mode):
    r=plugin.runner
    r.started=True
    entered,release=asyncio.Event(),asyncio.Event()
    async def classify(*args):
        entered.set()
        await release.wait()
        return {'name':'shower','done':False}
    monkeypatch.setattr(r,'classify_text',classify)
    monkeypatch.setattr(r,'_generate',AsyncMock(return_value='测试消息'))
    monkeypatch.setattr(r,'_append_assistant_history',AsyncMock())
    if mode!='normal':
        async def verify(**kw):
            entered.set()
            await release.wait()
            return SimpleNamespace(completion_text='YES')
        plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=verify)
        monkeypatch.setattr(r,'_recent_dialogue',AsyncMock(return_value=''))
    text={'normal':'我准备去整点东西','gray':'今天洗澡','appointment_gray':'我大概十二点下课'}[mode]
    await plugin.on_private_message(event(text,umo))
    await asyncio.wait_for(entered.wait(),1)
    plugin.panel_service.task_action({'action':'remove','umo':umo})
    assert umo not in r.sessions() and umo not in r.timers
    release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    resurrected=umo in r.timers
    # Fire the recreated timer's payload without waiting real minutes.
    outcome=await r._fire(umo) if resurrected else {'sent':False}
    assert not resurrected and plugin.context.send_message.await_count==0, {'removed':umo not in r.sessions(),'resurrected_timer':resurrected,'outcome':outcome,'send_count':plugin.context.send_message.await_count}


@pytest.mark.parametrize('new_text',['晚安，我睡了','我到家了','我不洗澡了'])
async def test_gray_result_must_not_override_a_newer_turn(plugin,umo,clock,monkeypatch,new_text):
    r=plugin.runner
    entered,release=asyncio.Event(),asyncio.Event()
    async def verify(**kw):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text='YES')
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=verify)
    monkeypatch.setattr(r,'_recent_dialogue',AsyncMock(return_value=''))
    await plugin.on_private_message(event('今天洗澡',umo))
    await asyncio.wait_for(entered.wait(),1)
    await plugin.on_private_message(event(new_text,umo))
    release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    assert active(plugin,umo) is None, {'latest_text':new_text,'state':plugin.store.data[umo]}


async def test_gray_acceptance_without_new_turn_control(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='YES')))
    monkeypatch.setattr(r,'_recent_dialogue',AsyncMock(return_value=''))
    await plugin.on_private_message(event('今天洗澡',umo))
    await asyncio.gather(*list(plugin._bg_tasks))
    assert active(plugin,umo)=='shower'


@pytest.mark.parametrize('new_text',['取消了','我不去了'])
async def test_appointment_gray_result_does_not_restore_cancelled_intention(plugin,umo,clock,monkeypatch,new_text):
    r=plugin.runner
    entered,release=asyncio.Event(),asyncio.Event()
    async def verify(**kw):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text='YES')
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=verify)
    await plugin.on_private_message(event('我大概十二点下课',umo))
    await asyncio.wait_for(entered.wait(),1)
    await plugin.on_private_message(event(new_text,umo))
    release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    assert not r._appointments(umo), {'newer_text':new_text,'appointments':r._appointments(umo)}


async def test_gray_result_drops_if_new_activity_exists_control(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    entered,release=asyncio.Event(),asyncio.Event()
    async def verify(**kw):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text='YES')
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=verify)
    monkeypatch.setattr(r,'_recent_dialogue',AsyncMock(return_value=''))
    await plugin.on_private_message(event('今天洗澡',umo))
    await asyncio.wait_for(entered.wait(),1)
    await plugin.on_private_message(event('我现在正在吃饭',umo))
    release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    assert active(plugin,umo)=='eating'


@pytest.mark.parametrize('word',['洗澡','吃饭','约会','开会'])
def test_short_third_person_shape_control(plugin,umo,clock,word):
    plugin.runner.update_from_message(umo,f'我妈在{word}')
    assert active(plugin,umo) is None


@pytest.mark.parametrize('text',['我下午三点的会议取消了','我下午三点不开会了'])
async def test_existing_appointment_is_not_sent_after_explicit_cancellation(plugin,umo,clock,monkeypatch,text):
    r=plugin.runner
    await plugin.on_private_message(event('我下午三点开会',umo))
    assert len(r._appointments(umo))==1
    await plugin.on_private_message(event(text,umo))
    await asyncio.gather(*list(plugin._bg_tasks))
    clock.now=datetime(2026,11,2,15).timestamp()
    monkeypatch.setattr(r,'_generate',AsyncMock(return_value='这场会已经取消'))
    monkeypatch.setattr(r,'_append_assistant_history',AsyncMock())
    result=await r._fire(umo)
    # Ordinary chat would be allowed; the cancelled appointment must not be selected.
    call=r._generate.await_args
    assert call.kwargs.get('appointment') is None, {'text':text,'generation_args':call.kwargs,'appointments':r._appointments(umo),'result':result}


async def test_explicit_early_wake_realigns_sleep_deferred_appointment(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    await plugin.on_private_message(event('我十一点下课',umo))
    await plugin.on_private_message(event('我先睡了',umo))
    planned_wake=r._sleep_state(umo)['wake_at']
    clock.now=datetime(2026,11,2,11).timestamp()
    await r._fire(umo)
    assert r.is_sleeping(umo)
    assert r._appointments(umo)[0]['deferred']
    clock.now=datetime(2026,11,2,11,30).timestamp()
    await plugin.on_private_message(event('我醒了',umo))
    assert not r.is_sleeping(umo)
    next_delay=r._appointment_next_delay(umo,1440)
    assert next_delay is not None and next_delay<=1, {'actual_wake':clock.now,'planned_wake':planned_wake,'delay_minutes':next_delay,'appointment':r._appointments(umo)[0]}


async def test_automatic_timer_cancellation_during_generation_preserves_new_user_schedule(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    r.started=True
    entered=asyncio.Event()
    async def generate(*args,**kw):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(r,'_generate',generate)
    task=asyncio.create_task(r._wait_and_fire(umo,0))
    r.timers[umo]=task
    await asyncio.wait_for(entered.wait(),1)
    await plugin.on_private_message(event('我现在正在洗澡',umo))
    scheduled=plugin.store.get(umo,'next_at')
    current_timer=r.timers[umo]
    await asyncio.gather(task,return_exceptions=True)
    after=plugin.store.get(umo,'next_at')
    assert after==scheduled and not current_timer.cancelled(), {'after_user_seconds':scheduled-clock.now,'after_cancel_seconds':after-clock.now,'event':plugin.store.get(umo,'active_event')}


async def test_fire_rechecks_sleep_after_slow_generation(plugin,umo,clock,monkeypatch):
    r=plugin.runner
    r.started=True
    entered,release=asyncio.Event(),asyncio.Event()
    async def generate(*args,**kw):
        entered.set()
        await release.wait()
        return '这条不应在睡眠免打扰后发出'
    monkeypatch.setattr(r,'_generate',generate)
    monkeypatch.setattr(r,'_append_assistant_history',AsyncMock())
    operation=asyncio.create_task(plugin.panel_service.fire({'umo':umo}))
    await asyncio.wait_for(entered.wait(),1)
    await plugin.on_private_message(event('晚安，我睡了',umo))
    assert r.is_sleeping(umo)
    release.set()
    result=await operation
    assert plugin.context.send_message.await_count==0, {'sleeping':r.is_sleeping(umo),'send_count':plugin.context.send_message.await_count,'result':result}


@pytest.mark.parametrize('sleeping,limit,appointment,reminder',list(itertools.product([False,True],repeat=4)))
async def test_real_state_priority_matrix(plugin,umo,clock,monkeypatch,sleeping,limit,appointment,reminder):
    r=plugin.runner
    clock.now=datetime(2026,11,2,23,10).timestamp()
    if appointment:
        # Starting appointments at >=23:00 are intentionally suppressed by M1;
        # an ending appointment is supported at night and tests the real clash.
        r.update_from_message(umo,'我晚上十一点十一分下课')
        assert len(r._appointments(umo))==1
        clock.now+=60
    if sleeping: r.update_from_message(umo,'我睡了')
    plugin.store.set(umo,'unanswered',3 if limit else 0)
    plugin.engine_cfg['enable_sleep_reminder']=reminder
    # Explicitly test a live reminder window, regardless of profile's clock.
    plugin.engine_cfg['remind_window_start']='23:00'
    plugin.engine_cfg['remind_window_end']='00:20'
    monkeypatch.setattr(r,'_generate',AsyncMock(return_value='隔离测试'))
    monkeypatch.setattr(r,'_append_assistant_history',AsyncMock())
    result=await r._fire(umo)
    expected=not sleeping and (not limit or appointment or reminder)
    assert bool(plugin.context.send_message.await_count)==expected, {'flags':[sleeping,limit,appointment,reminder],'result':result}
    if sleeping and appointment: assert r._appointments(umo)[0].get('deferred')


@pytest.mark.parametrize('seed',range(12))
def test_presence_exclusivity_over_conversation_sequences(plugin,umo,clock,seed):
    import random
    rng=random.Random(seed)
    messages=['我出门了','我们一起出门了','我到家了','我牵着你的手','我去公司上班了','我们各走各的','我妈到家了','明天才去上班','如果下雨我就出门','我正在吃饭','晚安我睡了','我醒了']
    for _ in range(40):
        text=rng.choice(messages)
        plugin.runner.update_from_message(umo,text)
        away=plugin.store.get(umo,'away') or {}
        together=plugin.store.get(umo,'together') or {}
        assert not (away.get('active') and together.get('active')), {'text':text,'state':plugin.store.data[umo]}


async def test_two_sessions_do_not_cancel_or_override_each_other(plugin,umo,clock,monkeypatch):
    second='test:FriendMessage:2'
    plugin.friend_cfg['session_list'].append(second)
    r=plugin.runner
    r.started=True
    for text in ['我去公司上班了','我正在开会']: await plugin.on_private_message(event(text,umo))
    await plugin.on_private_message(event('晚安，我睡了',second))
    assert not r.is_sleeping(umo) and r.is_sleeping(second)
    assert active(plugin,umo)=='meeting' and active(plugin,second) is None
    assert len(r.timers)==2
    monkeypatch.setattr(r,'_generate',AsyncMock(return_value='仅会话一'))
    monkeypatch.setattr(r,'_append_assistant_history',AsyncMock())
    await r._fire(umo)
    assert plugin.context.send_message.await_count==1
    await r._fire(second)
    assert plugin.context.send_message.await_count==1
    assert all(call.args[0]==umo for call in plugin.context.send_message.await_args_list)
