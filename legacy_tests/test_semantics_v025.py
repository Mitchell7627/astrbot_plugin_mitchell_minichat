"""Intent-labelled semantic safety and state-machine tests. No model calls.

The corpus checks deterministic state changes. A safe delegation to the model
does not prove that a real model will classify the input correctly.
"""
import asyncio
import copy
import importlib.util
import itertools
import json
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from test_minichat import runner, UMO, REPO, Plain
from test_conflicts import at_time
from core.appointment import parse as parse_appointment
from core.panel_api import PanelService


ACTIVITIES = [
    ('date', '约会', '约会结束了'), ('eating', '吃饭', '吃完了'),
    ('cooking', '做饭', '做好了'), ('drinking', '喝水', '喝完了'),
    ('commute', '坐地铁', '下车了'), ('meeting', '开会', '开完了'),
    ('working', '写代码', '忙完了'), ('shower', '洗澡', '洗完了'),
    ('gaming', '打游戏', '不玩了'), ('errand', '洗衣服', '弄完了'),
    ('study', '学习', '学完了'), ('movie', '看电影', '看完了'),
    ('shopping', '逛街', '逛完了'), ('hosp', '看病', '看完了'),
    ('exercise', '运动', '练完了'), ('nap', '午睡', '醒了'),
]
NONCURRENT = [
    ('negation', '我没有在{a}'), ('cancel', '取消{a}了'),
    ('tomorrow', '明天我去{a}'), ('later', '等会我再去{a}'),
    ('past', '昨天我在{a}'), ('last_week', '上周我去{a}了'),
    ('third_person', '我妈正在{a}'), ('other_person', '朋友正在{a}'),
    ('question', '你在{a}吗'), ('when_question', '什么时候去{a}'),
    ('hypothesis', '如果我去{a}，你就等我'), ('counterfactual', '要是我在{a}就好了'),
    ('chinese_quote', '示例：“我正在{a}”'), ('ascii_quote', '示例："我正在{a}"'),
    ('mentioned_video', '我在看{a}的视频'), ('subject_correction', '不是我在{a}，是同事'),
]


@pytest.mark.parametrize('name,text', [pytest.param(name, tmpl.format(a=action), id=f'{tag}-{name}')
    for name,action,_ in ACTIVITIES for tag,tmpl in NONCURRENT])
def test_noncurrent_semantics_never_commit_current_event(runner, name, text):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event') is None, {'text': text, 'actual': runner.store.get(UMO, 'active_event')}


@pytest.mark.parametrize('name,action,stop', ACTIVITIES, ids=[a[0] for a in ACTIVITIES])
def test_current_action_and_completion_positive_control(runner, name, action, stop):
    runner.update_from_message(UMO, f'我正在{action}')
    assert runner.store.get(UMO, 'active_event', {}).get('name') == name
    runner.update_from_message(UMO, stop)
    assert runner.store.get(UMO, 'active_event') is None


STOP_DISTRACTORS = [
    ('third_person', '我妈{s}'), ('past', '昨天我{s}'),
    ('reported', '他说“{s}”'), ('quote', '示例：“{s}”'),
    ('hypothesis', '如果我{s}就找你'), ('future', '明天我{s}再找你'),
    ('question', '你{s}吗'), ('not_me', '不是我{s}，是朋友'),
]


@pytest.mark.parametrize('name,text', [pytest.param(name, tmpl.format(s=stop), id=f'{tag}-{name}')
    for name,action,stop in ACTIVITIES for tag,tmpl in STOP_DISTRACTORS])
def test_other_or_unreal_completion_cannot_end_current_event(runner, name, text):
    runner._set_event(UMO, runner.engine.get_rule(name), 'setup')
    before = copy.deepcopy(runner.store.get(UMO, 'active_event'))
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event') == before, {'text': text, 'actual': runner.store.get(UMO, 'active_event')}


@pytest.mark.parametrize('old,new,stop,action', [pytest.param(old,new,stop,action,id=f'{old}-to-{new}')
    for old,_,stop in ACTIVITIES for new,action,_ in ACTIVITIES if old != new])
def test_completion_then_new_activity_keeps_latest_action(runner, old, new, stop, action):
    runner._set_event(UMO, runner.engine.get_rule(old), 'setup')
    text = f'{stop}，我正在{action}'
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == new, {'text': text, 'actual': runner.store.get(UMO, 'active_event')}


@pytest.mark.parametrize('text', [
    '昨天我醒了', '上周我起床了', '如果我醒了就找你', '要是我醒了就好了',
    '起床时间太早了', '梦见我起床了', '示例："我醒了"', '不是我醒了，是朋友',
])
def test_wake_hypothesis_past_and_mention_keep_sleeping(runner, text):
    with at_time(1):
        runner.update_from_message(UMO, '我睡了')
        runner.update_from_message(UMO, text)
        assert runner.is_sleeping(UMO), text


@pytest.mark.parametrize('text', [
    '如果我睡了你会想我吗', '要是我睡了就好了', '我梦见自己睡了',
    '不是我睡了，是我妈', '示例："我睡了"',
])
def test_sleep_hypothesis_mention_does_not_silence(runner, text):
    with at_time(1):
        runner.update_from_message(UMO, text)
        assert not runner.is_sleeping(UMO), text


@pytest.mark.parametrize('text', [
    '我和朋友一起去吃饭', '我跟同事一块出门了', '我不想和你一起出门',
    '如果我们一起出门就好了', '昨天牵着你的手', '他说“我们一起出门”',
])
def test_together_requires_real_current_bot_companion(runner, text):
    runner.update_from_message(UMO, text)
    assert not runner.store.get(UMO, 'together', {}).get('active'), text


def test_arrival_place_does_not_invent_home_presence(runner):
    with at_time(12) as now:
        runner.update_from_message(UMO, '我出门了')
        runner.store.set(UMO, 'appointments', [{'at':now, 'stage':'pending', 'kind':'arrive',
            'label':'到公司', 'raw':'十二点到公司', 'time_text':'12:00'}])
        hint = runner.appointment_hint(UMO, 'due')
        assert '他已经按说好的时间到家了' not in hint, hint


@pytest.mark.parametrize('first_follow_minutes,second_due_minutes', [(8,5),(15,4),(8,7)])
def test_timer_schedules_second_appointment_before_first_followup(runner, first_follow_minutes, second_due_minutes):
    with at_time(12) as now:
        runner.store.set(UMO, 'appointments', [
            {'at':now,'stage':'due','due_sent_at':now,'follow_up_at':now+first_follow_minutes*60},
            {'at':now+second_due_minutes*60,'stage':'pending','label':'开会'},
        ])
        delay = runner._appointment_next_delay(UMO, 120)
        assert delay is not None and delay <= second_due_minutes, delay


@pytest.mark.parametrize('text', [
    '如果我十二点下课就找你', '要是我十二点下课就好了', '示例：“十二点下课”',
    '我妈说她十二点下课', '我十二点下课的安排取消了',
])
def test_noncommitment_cannot_schedule_appointment(runner, text):
    with at_time(10):
        runner.update_from_message(UMO, text)
        assert not runner._appointments(UMO), {'text':text, 'appointments':runner._appointments(UMO)}


@pytest.mark.parametrize('persona_id,expected', [('[%None]',''),('selected','FORCED')])
def test_real_persona_resolution_contract(runner, persona_id, expected):
    async def scenario():
        async def resolve(*, umo, conversation_persona_id, platform_name, provider_settings=None):
            return (persona_id, None if persona_id=='[%None]' else {'prompt':'FORCED'}, None, False)
        runner.context.conversation_manager = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value='cid'),
            get_conversation=AsyncMock(return_value=types.SimpleNamespace(history='[]', persona_id=persona_id)))
        runner.context.persona_manager = types.SimpleNamespace(
            resolve_selected_persona=resolve,
            get_persona_v3_by_id=Mock(return_value=None if persona_id=='[%None]' else {'prompt':'SELECTED'}),
            get_default_persona_v3=AsyncMock(return_value={'prompt':'DEFAULT'}))
        assert await runner._load_persona(UMO) == expected
    asyncio.run(scenario())


def validate_tool_history(history):
    pending = set()
    for message in history:
        if message.get('role') == 'tool':
            call_id = message.get('tool_call_id')
            assert call_id in pending, 'orphan tool result'
            pending.remove(call_id)
        else:
            assert not pending, 'tool results were dropped before next assistant/user message'
            pending.update(c['id'] for c in message.get('tool_calls', []))
    assert not pending, 'incomplete tool-call group'


@pytest.mark.parametrize('kind', ['image_url','think','empty_text','whitespace_text','timestamp'])
def test_context_filtering_preserves_tool_result_protocol(runner, kind):
    if kind in ('empty_text', 'whitespace_text'):
        content = [{'type':'text', 'text':'' if kind == 'empty_text' else '   '}]
    elif kind == 'timestamp':
        content = '<system_reminder>Current datetime: 2026-11-02 10:00</system_reminder>'
    else:
        content = [{'type':kind,kind:{'url':'example'} if kind=='image_url' else 'reasoning'}]
    history = [{'role':'assistant','content':'','tool_calls':[{'id':'c1','type':'function','function':{'name':'tool','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'c1','content':content},
        {'role':'assistant','content':'I saw the result'},
        {'role':'user','content':'继续<system_reminder>Current datetime: 2026-11-02 10:01</system_reminder>'}]
    validate_tool_history(runner._prepare_contexts(history, runner._context_settings()))


def test_large_tool_group_obeys_history_hard_limit(runner):
    calls = [{'id':f'c{i}','type':'function','function':{'name':'tool','arguments':'{}'}} for i in range(10)]
    history = [{'role':'assistant','content':'','tool_calls':calls}]
    history += [{'role':'tool','tool_call_id':c['id'],'content':'result'} for c in calls]
    history += [{'role':'assistant','content':'answer'}]
    settings = runner._context_settings()
    settings['history_cap'] = 4
    out = runner._prepare_contexts(history, settings)
    assert len(out) <= 4
    validate_tool_history(out)


@pytest.mark.parametrize('done', ['false','0',[],{}])
def test_classifier_requires_json_boolean_done(runner, done):
    async def scenario():
        runner._set_event(UMO, runner.engine.get_rule('shower'), 'setup')
        runner._recent_dialogue = AsyncMock(return_value='')
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text=json.dumps({'event':'shower','done':done}))))
        runner.context.get_using_provider = Mock(return_value=provider)
        await runner.classify_with_llm(UMO, '还在洗香香呢')
        assert runner.store.get(UMO, 'active_event') is not None, done
    asyncio.run(scenario())


def load_main_class():
    def module(name):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
        parent,_,child = name.rpartition('.')
        if parent in sys.modules:
            setattr(sys.modules[parent], child, sys.modules[name])
        return sys.modules[name]
    star=module('astrbot.api.star')
    star.Star=type('Star',(),{})
    star.Context=object
    star.StarTools=types.SimpleNamespace(get_data_dir=lambda name: '.')
    event=module('astrbot.api.event')
    event.AstrMessageEvent=object
    decorator=lambda *a,**k: lambda fn: fn
    event.filter=types.SimpleNamespace(event_message_type=decorator,after_message_sent=decorator,
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE='private'))
    module('astrbot.core.config')
    module('astrbot.core.config.astrbot_config').AstrBotConfig=dict
    name='review_minichat_plugin'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name, REPO/'main.py',submodule_search_locations=[str(REPO)])
        value=importlib.util.module_from_spec(spec)
        sys.modules[name]=value
        spec.loader.exec_module(value)
    return sys.modules[name].ProactiveMiniPlugin


def plugin_for(runner):
    plugin=load_main_class().__new__(load_main_class())
    plugin.runner=runner
    plugin.store=runner.store
    plugin._bg_tasks=set()
    plugin._stop_panel=AsyncMock()
    return plugin


def event(text):
    return types.SimpleNamespace(unified_msg_origin=UMO,get_messages=lambda:[Plain(text)],message_str=text)


def test_actual_message_hook_captures_queued_classifier_turn(runner):
    async def scenario():
        plugin=plugin_for(runner)
        runner.classify_text=AsyncMock(return_value={'name':'shower','done':False})
        await plugin.on_private_message(event('准备洗香香了'))
        await plugin.on_private_message(event('我在吃饭'))
        await asyncio.gather(*list(plugin._bg_tasks))
        assert runner.store.get(UMO,'active_event')['name']=='eating'
    asyncio.run(scenario())


def test_terminate_cancels_pending_classifier_tasks(runner):
    async def scenario():
        plugin=plugin_for(runner)
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return {'name':'shower','done':False}
        runner.classify_text=slow
        await plugin.on_private_message(event('准备洗香香了'))
        await entered.wait()
        tasks=list(plugin._bg_tasks)
        await plugin.terminate()
        await asyncio.sleep(0)
        stopped=all(task.done() for task in tasks)
        release.set()
        await asyncio.gather(*tasks,return_exceptions=True)
        assert stopped, 'Old plugin classification keeps running after unload/reload'
    asyncio.run(scenario())


@pytest.mark.parametrize('text', [
    '我妈出门了', '朋友去上课了', '你去上班吧',
    '如果我出门了你就等我', '我没有在出门', '示例："我出门了"',
])
def test_presence_away_requires_actual_user_departure(runner, text):
    runner.update_from_message(UMO, text)
    assert not runner.store.get(UMO, 'away', {}).get('active'), text


@pytest.mark.parametrize('text', [
    '我妈到家了', '朋友回来了', '你进门了',
    '如果我到家了就找你', '我没有到家', '示例："我到家了"',
])
def test_presence_return_requires_actual_user_arrival(runner, text):
    runner.update_from_message(UMO, '我出门了')
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'away', {}).get('active'), text


@pytest.mark.parametrize('stop,action,expected', [
    (stop, action, name) for stop in ('下课了', '下班了', '放学了')
    for name, action in [('eating', '吃饭'), ('shower', '洗澡')]
])
def test_period_end_must_not_swallow_current_action(runner, stop, action, expected):
    runner.update_from_message(UMO, '我出门了')
    text = f'{stop}，我正在{action}'
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('name,action,stop', ACTIVITIES, ids=[a[0] for a in ACTIVITIES])
def test_second_person_activity_is_not_assigned_to_user(runner, name, action, stop):
    text = f'你正在{action}'
    runner.update_from_message(UMO, text)
    current = runner.store.get(UMO, 'active_event')
    assert not current or current.get('subject') != 'self', {'text': text, 'actual': current}


@pytest.mark.parametrize('text', [
    '我正在写代码，昨天我们牵手了',
    '我正在写代码，他说“我们一起出门”',
    '我正在写代码，不想陪你出门',
])
def test_current_event_subject_uses_its_own_clause(runner, text):
    runner.update_from_message(UMO, text)
    current = runner.store.get(UMO, 'active_event')
    assert current and current.get('name') == 'working' and current.get('subject') == 'self', current


@pytest.mark.parametrize('text', [
    '明天才到公司', '我妈到公司了', '如果我去上课就等我',
    '我不去开会了', '他说“我到学校了”',
])
def test_noncurrent_solo_mention_does_not_break_together_state(runner, text):
    runner.update_from_message(UMO, '我们牵手')
    assert runner.store.get(UMO, 'together', {}).get('active')
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'together', {}).get('active'), text


@pytest.mark.parametrize('text', [
    '如果我去洗澡，你就等我', '我没有在吃饭', '什么时候去开会',
    '示例："我正在洗澡"', '不是我在打游戏，是同事',
])
def test_real_hook_does_not_bypass_model_on_false_keyword_commit(runner, text):
    async def scenario():
        plugin = plugin_for(runner)
        runner.classify_text = AsyncMock(return_value={'name': 'none', 'done': False})
        await plugin.on_private_message(event(text))
        if plugin._bg_tasks:
            await asyncio.gather(*list(plugin._bg_tasks))
        assert runner.store.get(UMO, 'active_event') is None, text
    asyncio.run(scenario())
