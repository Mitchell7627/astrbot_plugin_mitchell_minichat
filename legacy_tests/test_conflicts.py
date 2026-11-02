"""Expanded v0.2.4 behavior and interaction checks; no real QQ/model calls."""
import asyncio
import copy
import itertools
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from test_minichat import runner, UMO, REPO, Plain
from core.appointment import stage_of
from core.event_detector import extract_message_text
from core.panel_api import PanelService
from core.web_panel import PanelHttpServer
from core.probability import dual_forecast
from core.splitter import split_text
from core.migrations import migrate_config


@contextmanager
def at_time(hour=18, minute=0, second=0):
    moment = datetime(2026, 11, 1, hour, minute, second)
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)
    with patch('core.runner.datetime', Frozen), patch('core.runner.time.time', return_value=moment.timestamp()):
        yield moment.timestamp()


EVENTS = [
    ('date', '我在约会'), ('eating', '我在吃饭'), ('cooking', '我在做饭'),
    ('drinking', '我在喝水'), ('commute', '我在路上'), ('meeting', '我在开会'),
    ('working', '我在写代码'), ('shower', '我在洗澡'), ('gaming', '我在打游戏'),
    ('errand', '我在洗衣服'), ('study', '我在学习'), ('movie', '我在看电影'),
    ('shopping', '我在逛街'), ('hosp', '我在看病'), ('exercise', '我在运动'), ('nap', '我午睡了'),
]


@pytest.mark.parametrize('name,text', EVENTS)
def test_all_default_event_categories(runner, name, text):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == name


@pytest.mark.parametrize('prefix', ['明天', '昨天', '我妈', '他说', '不想', '取消'])
@pytest.mark.parametrize('name,text', [('shower', '去洗澡'), ('meeting', '去开会'), ('exercise', '去健身')])
def test_subject_time_negation_matrix(runner, prefix, name, text):
    runner.update_from_message(UMO, prefix + text)
    assert runner.store.get(UMO, 'active_event') is None


@pytest.mark.parametrize('first,second', list(itertools.product(
    ['出门了', '一起出门', '我到家了', '牵着你的手', '去上班了', '各走各的'], repeat=2)))
def test_presence_states_remain_exclusive(runner, first, second):
    for text in (first, second):
        runner.update_from_message(UMO, text)
        away = runner.store.get(UMO, 'away', {})
        together = runner.store.get(UMO, 'together', {})
        assert not (away.get('active') and together.get('active'))


@pytest.mark.parametrize('enabled,sleeping,reminding,shot,capped', list(itertools.product(
    [True, False], [True, False], [True, False], [None, 'lead', 'due', 'follow'], [True, False])))
def test_send_priority_matrix(runner, enabled, sleeping, reminding, shot, capped):
    async def scenario():
        runner.cfg['enable'] = enabled
        runner.store.set(UMO, 'unanswered', 3 if capped else 0)
        runner._generate = AsyncMock(return_value='hello')
        runner._send_split = AsyncMock(return_value='hello')
        runner._append_assistant_history = AsyncMock()
        shot_method = 'appointment_shot_item' if hasattr(runner, 'appointment_shot_item') else 'appointment_shot'
        shot_value = {'item': {'at': 1}, 'shot': shot, 'at': 1} if shot and shot_method == 'appointment_shot_item' else shot
        with patch.object(runner, 'is_sleeping', return_value=sleeping), \
             patch.object(runner, 'should_remind_sleep', return_value=reminding), \
             patch.object(runner, shot_method, return_value=shot_value), \
             patch.object(runner, '_next_wake_minutes', return_value=None), \
             patch.object(runner, '_advance_appointment'), patch.object(runner, 'mark_sleep_reminded'):
            await runner._fire(UMO)
        bypass = (reminding and not shot) or shot in ('due', 'follow')
        assert bool(runner._send_split.await_count) == (enabled and not sleeping and (not capped or bypass))
        assert not runner._firing
    asyncio.run(scenario())


@pytest.mark.parametrize('images,think,window,cap', list(itertools.product([True, False], repeat=4)))
def test_context_switches_do_not_mutate_history(runner, images, think, window, cap):
    history = [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}, {'type': 'image_url', 'image_url': {'url': 'test'}}]},
        {'role': 'assistant', 'content': [{'type': 'think', 'think': 'private'}, {'type': 'text', 'text': 'hi'}]},
    ] * 5
    original = copy.deepcopy(history)
    settings = runner._context_settings()
    settings.update(include_images=images, include_think=think, window_enabled=window,
                    history_cap=4 if cap else 0, window_count=4, window_min=2)
    result = runner._prepare_contexts(history, settings)
    assert history == original
    assert len(result) <= (4 if cap or window else 10)
    if not images:
        assert not any(part.get('type') == 'image_url' for item in result for part in item['content'])
    if not think:
        assert not any(part.get('type') == 'think' for item in result for part in item['content'])


@pytest.mark.parametrize('kind', ['conversation', 'platform', 'hybrid', 'empty_platform_fallback'])
def test_generation_context_source_routing(runner, kind):
    async def scenario():
        conf = runner.cfg['context_settings']
        conf['use_conversation_history'] = kind in ('conversation', 'hybrid')
        conf['use_platform_history'] = kind != 'conversation'
        runner._load_history = AsyncMock(return_value=[{'role': 'user', 'content': 'history'}])
        runner._load_persona = AsyncMock(return_value='persona')
        runner._platform_block = AsyncMock(return_value='' if kind == 'empty_platform_fallback' else 'platform')
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='hello')))
        runner.context.get_using_provider = Mock(return_value=provider)
        await runner._generate(UMO)
        args = provider.text_chat.await_args.kwargs
        assert args['system_prompt'] == 'persona'
        assert bool(args['contexts']) == (kind != 'platform')
        assert ('platform' in args['prompt']) == (kind in ('platform', 'hybrid'))
    asyncio.run(scenario())


@pytest.mark.parametrize('name,end', [('eating', '吃完了'), ('cooking', '吃完了'), ('shower', '洗完了'),
                                    ('gaming', '不玩了'), ('meeting', '散会了')])
def test_event_completion(runner, name, end):
    runner._set_event(UMO, runner.engine.get_rule(name), 'test')
    runner.update_from_message(UMO, end)
    assert runner.store.get(UMO, 'active_event') is None


def test_quote_component_does_not_replace_current_text():
    event = SimpleNamespace(get_messages=lambda: [SimpleNamespace(type='Reply', message_str='我去洗澡了'),
                                                   Plain('我在吃饭')])
    assert extract_message_text(event) == '我在吃饭'


@pytest.mark.parametrize('hour', [1, 9, 18])
def test_regression_pause_keeps_remote_sleep_window(runner, hour):
    with at_time(hour):
        runner.engine_cfg['nap_remind_enabled'] = False
        assert runner._next_wake_minutes(UMO) is not None, 'Future enabled night reminder must remain scheduled beyond max_interval'


def test_regression_panel_partial_send_history(runner):
    async def scenario():
        runner.started = True
        runner._send_split = AsyncMock(return_value='delivered')
        runner._append_assistant_history = AsyncMock()
        await PanelService(runner.plugin).send({'umo': UMO, 'text': 'delivered + failed'})
        runner._append_assistant_history.assert_awaited_once_with(UMO, 'delivered')
    asyncio.run(scenario())


@pytest.mark.parametrize('cap,window', [(2, False), (0, True)])
def test_regression_context_trimming_keeps_tool_dependencies(runner, cap, window):
    history = [
        {'role': 'user', 'content': 'search'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'search', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'search result'},
        {'role': 'assistant', 'content': 'answer'},
    ]
    settings = runner._context_settings()
    settings.update(history_cap=cap, window_enabled=window, window_count=2, window_min=0)
    out = runner._prepare_contexts(history, settings)
    calls = {call['id'] for item in out for call in item.get('tool_calls', [])}
    assert all(item.get('tool_call_id') in calls for item in out if item.get('role') == 'tool'), out


@pytest.mark.parametrize('followup', [False, True])
def test_regression_second_appointment_not_blocked_by_first(runner, followup):
    with at_time(12) as now:
        runner.engine_cfg['appointment_followup_enabled'] = followup
        runner.store.set(UMO, 'appointments', [
            {'at': now, 'stage': 'pending', 'label': '下课', 'time_text': '12:00'},
            {'at': now + 300, 'stage': 'pending', 'label': '开会', 'time_text': '12:05'},
        ])
        runner._advance_appointment(UMO, 'due')
        cfg = runner._appointment_cfg()
        with at_time(12, 5) as later:
            assert stage_of(runner._appointments(UMO)[1], later, cfg) == 'due'
            assert runner.appointment_shot(UMO) == 'due', 'Earlier reminder occupies the head of queue after its due message'


@pytest.mark.parametrize('text', ['我妈醒了', '你醒了吗', '我还没起床', '明天起床再说', '他说“我醒了”'])
def test_regression_wake_requires_current_user_statement(runner, text):
    with at_time(1):
        runner.update_from_message(UMO, '我睡了')
        assert runner.is_sleeping(UMO)
        runner.update_from_message(UMO, text)
        assert runner.is_sleeping(UMO), text


@pytest.mark.parametrize('text', ['不好吃', '还没开饭', '他觉得好吃', '明天开饭再说'])
def test_regression_meal_handoff_obeys_semantics(runner, text):
    runner._set_event(UMO, runner.engine.get_rule('cooking'), '做饭')
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == 'cooking'


def test_regression_real_arrival_does_not_mask_new_activity(runner):
    runner.update_from_message(UMO, '我到家了，我去洗澡了')
    assert runner.store.get(UMO, 'active_event', {}).get('name') == 'shower'


def test_regression_selected_conversation_persona_is_used(runner):
    async def scenario():
        runner.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value='conversation-1'),
            get_conversation=AsyncMock(return_value=SimpleNamespace(history='[]', persona_id='selected-persona')),
        )
        runner.context.persona_manager = SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={'name': 'default-persona', 'prompt': 'DEFAULT'}),
            get_persona_v3_by_id=Mock(return_value={'name': 'selected-persona', 'prompt': 'SELECTED'}),
        )
        assert await runner._load_persona(UMO) == 'SELECTED'
    asyncio.run(scenario())


@pytest.mark.compatibility
def test_regression_empty_conversation_accepts_first_proactive_history(runner):
    async def scenario():
        cm = SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='conversation-1'),
            get_conversation=AsyncMock(return_value=SimpleNamespace(history=[])), update_conversation=AsyncMock())
        runner.context.conversation_manager = cm
        await runner._append_assistant_history(UMO, 'first proactive message')
        cm.update_conversation.assert_awaited_once()
    asyncio.run(scenario())


def test_regression_manual_and_automatic_sends_share_lock(runner):
    async def scenario():
        runner.started = True
        runner._finish_cycle = Mock()
        runner._generate = AsyncMock(return_value='automatic')
        runner._append_assistant_history = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args):
            entered.set()
            await release.wait()
            return True
        runner.context.send_message = AsyncMock(side_effect=send)
        with patch.object(runner, 'should_remind_sleep', return_value=False):
            automatic = asyncio.create_task(runner._fire(UMO))
            await entered.wait()
            manual = asyncio.create_task(PanelService(runner.plugin).send({'umo': UMO, 'text': 'manual'}))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            entered_count = runner.context.send_message.await_count
            release.set()
            await asyncio.gather(automatic, manual, return_exceptions=True)
        assert entered_count == 1, 'Both sends enter the same session concurrently'
    asyncio.run(scenario())


def test_regression_user_reply_between_segments_does_not_lose_history(runner):
    async def scenario():
        runner.started = True
        runner._generate = AsyncMock(return_value='first.second.')
        runner._append_assistant_history = AsyncMock()
        first_sent = asyncio.Event()
        async def send(*args):
            first_sent.set()
            return True
        runner.context.send_message = AsyncMock(side_effect=send)
        with patch('core.runner.split_text', return_value=['first.', 'second.']), \
             patch('core.runner.calc_delay', return_value=3600), \
             patch.object(runner, 'should_remind_sleep', return_value=False):
            task = asyncio.create_task(runner._wait_and_fire(UMO, 0))
            runner.timers[UMO] = task
            await first_sent.wait()
            runner.update_from_message(UMO, '知道了')
            runner.reschedule_after_message(UMO)
            await asyncio.gather(task, return_exceptions=True)
            await runner.stop()
        runner._append_assistant_history.assert_awaited_once_with(UMO, 'first.')
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['arrive', 'start'])
def test_regression_appointment_prompt_respects_event_kind(runner, kind):
    with at_time(12) as now:
        label = '开会' if kind == 'start' else '到家'
        runner.store.set(UMO, 'appointments', [{'at': now, 'stage': 'pending', 'kind': kind,
            'label': label, 'raw': f'十二点{label}', 'time_text': '12:00'}])
        hint = runner.appointment_hint(UMO, 'due')
        assert '当成已经结束' not in hint, hint


def test_regression_far_appointment_survives_pause(runner):
    with at_time(10) as now:
        runner.engine_cfg['enable_sleep_reminder'] = False
        runner.engine_cfg['nap_remind_enabled'] = False
        runner.store.set(UMO, 'appointments', [{'at': now + 7 * 3600, 'stage': 'pending', 'label': '下课'}])
        assert runner._next_wake_minutes(UMO) is not None


@pytest.mark.parametrize('text', ['他说“我去洗澡了”，我在吃饭', '“好吃”是他的评价，我在开会'])
def test_regression_quotation_does_not_hide_current_clause(runner, text):
    expected = 'eating' if '吃饭' in text else 'meeting'
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == expected


def test_regression_legacy_reply_text_is_not_current_user_text():
    event = SimpleNamespace(get_messages=lambda: [SimpleNamespace(type='Reply', text='我去洗澡了'),
                                                   Plain('好的')])
    assert extract_message_text(event) == '好的'


@pytest.mark.parametrize('text', ['下午三点开会', '下午三点我去开会', '下午三点出门', '下午三点我去吃饭'])
def test_regression_clock_appointment_is_not_current_activity(runner, text):
    with at_time(10):
        runner.update_from_message(UMO, text)
        assert runner._appointments(UMO), text
        assert runner.store.get(UMO, 'active_event') is None, runner.store.get(UMO, 'active_event')
        assert not runner.store.get(UMO, 'away', {}).get('active')


def test_regression_classifier_sequence_captured_before_task_starts(runner):
    async def scenario():
        runner.classify_text = AsyncMock(return_value={'name': 'shower', 'done': False})
        assert runner.update_from_message(UMO, '准备洗香香了') is False
        seq = runner.store.get(UMO, 'turn_seq')
        if 'seq' in __import__('inspect').signature(runner.classify_with_llm).parameters:
            task = asyncio.create_task(runner.classify_with_llm(UMO, '准备洗香香了', seq))
        else:
            task = asyncio.create_task(runner.classify_with_llm(UMO, '准备洗香香了'))
        runner.update_from_message(UMO, '我在吃饭')
        await task
        assert runner.store.get(UMO, 'active_event')['name'] == 'eating'
    asyncio.run(scenario())


@pytest.mark.parametrize('password', ['abc123', '中文密码', '🔑密码'])
def test_login_authorization_logout_flow(runner, password):
    from fastapi.testclient import TestClient
    runner.plugin.config['web_panel']['password'] = password
    service = SimpleNamespace(status=lambda: {'ok': True})
    server = PanelHttpServer(runner.plugin, service)
    with TestClient(server._app, raise_server_exceptions=False) as client:
        assert client.get('/api/status').status_code == 401
        assert client.post('/api/login', json={'password': 'wrong'}).status_code == 401
        assert client.post('/api/login', json={'password': password}).status_code == 200
        assert client.get('/api/status').status_code == 200
        assert client.post('/api/logout').status_code == 200
        assert client.get('/api/status').status_code == 401


def test_login_rate_limit(runner):
    from fastapi.testclient import TestClient
    runner.plugin.config['web_panel']['password'] = 'secret'
    server = PanelHttpServer(runner.plugin, SimpleNamespace())
    with TestClient(server._app, raise_server_exceptions=False) as client:
        for _ in range(5):
            assert client.post('/api/login', json={'password': 'bad'}).status_code == 401
        assert client.post('/api/login', json={'password': 'bad'}).status_code == 429


def test_sessions_do_not_share_event_or_sleep_state(runner):
    other = 'test:FriendMessage:99999'
    runner.cfg['session_list'].append(other)
    with at_time(1):
        runner.update_from_message(UMO, '我睡了')
        runner.update_from_message(other, '我在吃饭')
        assert runner.is_sleeping(UMO)
        assert not runner.is_sleeping(other)
        assert runner.store.get(UMO, 'active_event') is None
        assert runner.store.get(other, 'active_event')['name'] == 'eating'


def test_regression_reset_unanswered_resumes_paused_timer(runner):
    async def scenario():
        runner.started = True
        runner.store.set(UMO, 'unanswered', 3)
        service = PanelService(runner.plugin)
        service.tasks = Mock(return_value={})
        service.task_action({'action': 'reset_unanswered', 'umo': UMO})
        pending = [task for task in runner.timers.values() if not task.done()]
        await runner.stop()
        assert pending, 'Counter reset alone does not recreate the paused timer'
    asyncio.run(scenario())


def test_regression_normal_reply_and_appointment_scene_agree(runner):
    with at_time(15) as now:
        runner.update_from_message(UMO, '我出门了')
        runner.store.set(UMO, 'appointments', [{'at': now, 'stage': 'pending', 'kind': 'arrive',
            'label': '到家', 'raw': '十五点到家', 'time_text': '15:00'}])
        hint = runner.appointment_hint(UMO, 'due')
        assert not ('当成已经结束' in hint and '现在还没回来' in hint), hint


@pytest.mark.parametrize('budget', [0, 1])
def test_regression_event_forecast_matches_sampling_window(runner, budget):
    with at_time(12) as now:
        runner._set_event(UMO, runner.engine.get_rule('shower'), '洗澡')
        event = runner.store.get(UMO, 'active_event')
        event['expires_at'] = now + 10 * 60
        event['boost_budget'] = budget
        forecast = dual_forecast(schedule=runner.cfg['schedule_settings'], engine_cfg=runner.engine_cfg,
                                 active_event=event, now_ts=now)['event']
        samples = [runner._sample_minutes(UMO) for _ in range(20)]
        assert max(samples) <= 10
        assert forecast['mean_minutes'] <= 10, forecast
        assert forecast['boost_budget'] == budget


def test_regression_multiple_followups_schedule_next_shot(runner):
    with at_time(12) as now:
        runner.engine_cfg['appointment_max_followups'] = 2
        runner.store.set(UMO, 'appointments', [{'at': now - 8 * 60, 'stage': 'due',
            'due_sent_at': now - 8 * 60, 'follow_up_at': now, 'follow_ups': 0}])
        runner._advance_appointment(UMO, 'follow')
        item = runner._appointments(UMO)[0]
        assert item['follow_ups'] == 1
        assert item['follow_up_at'] > now, item


@pytest.mark.parametrize('text,protected', [
    ('开头。“里面第一句。里面第二句。”结尾。', '“里面第一句。里面第二句。”'),
    ('开头。（外层（内层）仍在括号里。还没结束。）结尾。', '（外层（内层）仍在括号里。还没结束。）'),
])
def test_regression_smart_split_protects_chinese_and_nested_pairs(text, protected):
    parts = split_text(text, smart=True, balanced=False, min_length=0, max_segments=10)
    assert any(protected in part for part in parts), parts


def test_regression_zero_width_split_regex_cannot_hang():
    try:
        result = subprocess.run([sys.executable, '-c',
            "from core.splitter import split_text; print(split_text('hello', pattern=r'\\s*'))"],
            cwd=REPO, capture_output=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail('A valid user-supplied regex with an empty match hangs smart_split indefinitely')
    assert result.returncode == 0, result.stderr


class SavingConfig(dict):
    def save_config(self):
        pass
    async def save_config_async(self):
        pass


def test_regression_enabling_initially_disabled_plugin_requests_restart(runner):
    async def scenario():
        runner.plugin.config = SavingConfig(runner.plugin.config)
        runner.cfg['enable'] = False
        runner.plugin.runner = None
        result = await PanelService(runner.plugin).config_save({'patch': {'friend_settings': {'enable': True}}})
        assert result['need_reload'] or (runner.plugin.runner and runner.plugin.runner.started), result
    asyncio.run(scenario())


def test_config_migration_preserves_customization_and_is_idempotent(runner):
    runner.cfg['proactive_prompt'] = 'MY CUSTOM PROMPT'
    runner.engine_cfg['away_keywords'] = '自定义离开词'
    migrate_config(runner.plugin.config)
    assert runner.cfg['proactive_prompt'] == 'MY CUSTOM PROMPT'
    assert runner.engine_cfg['away_keywords'] == '自定义离开词'
    assert migrate_config(runner.plugin.config) == []


def test_undo_restores_changed_event_and_rejects_stale_undo(runner):
    runner.update_from_message(UMO, '我在洗澡')
    record = runner.decisions.items(UMO)[0]
    assert record['undo_state'] == 'available'
    runner.undo_decision(record['id'])
    assert runner.store.get(UMO, 'active_event') is None
    runner.update_from_message(UMO, '我在吃饭')
    older = runner.decisions.items(UMO)[0]
    runner.update_from_message(UMO, '我在开会')
    with pytest.raises(ValueError):
        runner.undo_decision(older['id'])


def test_runner_stop_cancels_scheduled_tasks(runner):
    async def scenario():
        await runner.start()
        old = list(runner.timers.values()) + [runner._expiry_task]
        assert runner.started and old
        await runner.stop()
        await asyncio.sleep(0)
        assert not runner.started and not runner.timers
        assert all(task.done() for task in old)
    asyncio.run(scenario())
