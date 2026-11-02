"""0.2.7 integration boundaries for appointments, presence and generated context."""
import asyncio
import copy
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from test_minichat import runner, UMO
from test_conflicts import at_time
from core.runner import SLEEP_KEY


@pytest.mark.parametrize('text', [
    '下午三点我去开会', '下午三点我去吃饭', '下午三点我出门',
])
@pytest.mark.parametrize('obstacle', ['duplicate', 'daily_limit', 'full_queue'])
def test_future_action_stays_future_when_appointment_not_inserted(runner, text, obstacle):
    with at_time(10) as now:
        if obstacle == 'duplicate':
            runner.update_from_message(UMO, text)
            assert runner.store.get(UMO, 'active_event') is None
        elif obstacle == 'daily_limit':
            runner.engine_cfg['appointment_max_per_day'] = 1
            runner.store.set(UMO, 'appointment_counter', {'date':'2026-11-01','n':1})
        else:
            runner.store.set(UMO, 'appointments', [
                {'at':now+i*3600, 'stage':'pending', 'kind':'end', 'label':'下课'}
                for i in (1,2,3)])
        runner.update_from_message(UMO, text)
        assert runner.store.get(UMO, 'active_event') is None, {'text':text,'obstacle':obstacle}


@pytest.mark.parametrize('text,expected', [
    ('我现在正在吃饭，下午三点我去开会', 'eating'),
    ('下午三点我去开会，我现在正在洗澡', 'shower'),
])
def test_current_action_survives_separate_future_clause(runner, text, expected):
    with at_time(10):
        runner.update_from_message(UMO, text)
        assert len(runner._appointments(UMO)) == 1
        assert runner.store.get(UMO, 'active_event', {}).get('name') == expected


@pytest.mark.parametrize('name,action', [
    ('shower','洗澡'), ('working','写代码'), ('eating','吃饭'), ('gaming','打游戏'),
])
def test_return_prompt_keeps_current_activity(runner, name, action):
    runner.update_from_message(UMO, f'我到家了，我正在{action}')
    current = runner.store.get(UMO, 'active_event')
    assert current and current['name'] == name
    hint = runner.event_hint(UMO)
    assert current['label'] in hint, hint


@pytest.mark.parametrize('name,action', [
    ('eating','吃饭'), ('drinking','喝水'), ('gaming','打游戏'), ('shower','洗澡'),
])
def test_together_presence_does_not_make_every_activity_shared(runner, name, action):
    runner.update_from_message(UMO, '我们牵手')
    runner.update_from_message(UMO, f'我正在{action}')
    current = runner.store.get(UMO, 'active_event')
    assert current and current['name'] == name and current['subject'] == 'self'
    hint = runner.event_hint(UMO)
    assert f"你们现在{current['label']}" not in hint, hint


@pytest.mark.parametrize('name,action', [
    ('eating','吃饭'), ('shower','洗澡'), ('working','写代码'), ('gaming','打游戏'),
])
def test_away_prompt_includes_current_activity_without_company(runner, name, action):
    with at_time(12) as now:
        runner.store.set(UMO, 'away', {'active':True,'reason':'出门','started_at':now,'expires_at':now+3600})
        runner._set_event(UMO, runner.engine.get_rule(name), action, source_text=f'我正在{action}')
        hint = runner.event_hint(UMO)
        # 场景可用「去洗澡」或「在忙」概括输入的动作。
        label = runner.store.get(UMO, 'active_event')['label']
        assert '不在你这边' in hint and (action in hint or label in hint), hint
        assert '他就在旁边陪着你' not in hint and '你们现在' not in hint, hint


@pytest.mark.parametrize('action,physical_action', [
    ('写代码', '拽袖子'), ('喝水', '凑过去讨一口'), ('打游戏', '按他手机'),
])
def test_away_scene_avoids_unavailable_physical_actions(runner, action, physical_action):
    with at_time(12):
        runner.update_from_message(UMO, '我出门了')
        runner.update_from_message(UMO, f'我正在{action}')
        assert runner.store.get(UMO, 'away', {}).get('active')
        assert runner.store.get(UMO, 'active_event')
        hint = runner.event_hint(UMO)
        assert '不在你这边' in hint, hint
        assert physical_action not in hint, hint


def sleep_until(runner, now, wake_at):
    runner.update_from_message(UMO, '我睡了')
    state = runner._sleep_state(UMO)
    state['wake_at'] = wake_at
    runner.store.set(UMO, SLEEP_KEY, state)


def appt(at, label='下课', kind='end'):
    return {'at':at,'stage':'pending','label':label,'kind':kind,
            'time_text':datetime.fromtimestamp(at).strftime('%H:%M'),
            'raw':f'{datetime.fromtimestamp(at):%H:%M}{label}'}


def test_sleeping_due_appointment_is_deferred_without_sending(runner):
    async def scenario():
        with at_time(12) as now:
            sleep_until(runner, now, now+3600)
            runner.store.set(UMO, 'appointments', [appt(now)])
            runner._generate = AsyncMock(return_value='should not generate')
            runner._send_split = AsyncMock(return_value='sent')
            await runner._fire(UMO)
            item = runner._appointments(UMO)[0]
            assert item.get('deferred') and item['at'] == now+3660
            runner._generate.assert_not_awaited()
            runner._send_split.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('count', [1,2,3])
def test_multiple_sleep_deferred_appointments_can_all_finish(runner, count):
    async def scenario():
        with at_time(12) as now:
            wake_at = now+2*3600
            sleep_until(runner, now, wake_at)
            runner.engine_cfg['appointment_followup_enabled'] = False
            assert runner._appointment_cfg()['followup_enabled'] is False
            runner.store.set(UMO, 'appointments', [appt(now+i*300, label=f'事项{i}') for i in range(count)])
        for i in range(count):
            with at_time(12, i*5):
                await runner._fire(UMO)
        assert all(item.get('deferred') for item in runner._appointments(UMO))
        runner._generate = AsyncMock(return_value='reminder')
        runner._send_split = AsyncMock(return_value='delivered')
        runner._append_assistant_history = AsyncMock()
        with at_time(14,1):
            for _ in range(count):
                await runner._fire(UMO)
            assert runner._send_split.await_count == count
            assert all(item['stage'] == 'done' for item in runner._appointments(UMO)), runner._appointments(UMO)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,label', [('start','开会'),('end','下课')])
def test_daytime_deferred_prompt_does_not_call_it_last_night(runner, kind, label):
    with at_time(12) as now:
        sleep_until(runner, now, now+3600)
        runner.store.set(UMO, 'appointments', [appt(now,label,kind)])
        assert runner._defer_appointment(UMO, now, now+3600)
    with at_time(13,1):
        hint = runner.appointment_hint(UMO, 'due')
        assert '昨晚' not in hint, hint


@pytest.mark.parametrize('label,expected_away', [('到家',False),('到公司',True)])
def test_arrival_appointment_updates_appropriate_presence(runner, label, expected_away):
    with at_time(12) as now:
        runner.update_from_message(UMO, '我出门了')
        runner.engine_cfg['appointment_followup_enabled'] = False
        runner.store.set(UMO, 'appointments', [appt(now,label,'arrive')])
        runner._advance_appointment(UMO, 'due', now)
        assert bool(runner.store.get(UMO, 'away', {}).get('active')) == expected_away
        if label == '到家':
            assert runner.store.get(UMO, 'active_event') is None


@pytest.mark.parametrize('mode', ['complete','expire','renew','semantic_complete'])
def test_linked_together_duration_tracks_long_event(runner, mode):
    with at_time(12) as now:
        runner.update_from_message(UMO, '我们牵手')
        rule = copy.deepcopy(runner.engine.get_rule('date'))
        rule['ttl_minutes'] = 240
        runner._set_event(UMO, rule, '约会', source_text='我们一起约会')
        assert runner.store.get(UMO, 'together')['event_linked']
        if mode == 'complete':
            runner.update_from_message(UMO, '约会结束了')
            assert runner.store.get(UMO, 'together')['until'] <= now+15*60
        elif mode == 'expire':
            event = runner.store.get(UMO, 'active_event')
            event['expires_at'] = now-1
            runner._sweep_expired_events()
            assert runner.store.get(UMO, 'active_event') is None
            assert runner.store.get(UMO, 'together')['until'] <= now+15*60
        elif mode == 'semantic_complete':
            runner.classify_text = AsyncMock(return_value={'name':'date','done':True})
            asyncio.run(runner.classify_with_llm(UMO, '这次约会玩得差不多了'))
            assert runner.store.get(UMO, 'active_event') is None
            assert runner.store.get(UMO, 'together')['until'] <= now+15*60
        else:
            # A later explicit renewal must extend presence with the event.
            runner.engine.get_rule('date')['ttl_minutes'] = 240
    if mode == 'renew':
        with at_time(14) as later:
            runner.update_from_message(UMO, '我们正在约会')
            event = runner.store.get(UMO, 'active_event')
            assert runner.store.get(UMO, 'together')['until'] >= event['expires_at']


@pytest.mark.parametrize('text,expected', [
    ('我现在不得不开会', 'meeting'), ('我现在不得不去洗澡了', 'shower'),
    ('不过我正在吃饭', 'eating'), ('我忍不住去打游戏了', 'gaming'),
])
def test_nonnegative_words_containing_bu_keep_positive_action(runner, text, expected):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('text', [
    '下雨的话，我就去洗澡', '天气好的话，我就去运动',
    '你同意的话，我就去吃饭', '没事的话，我就去打游戏',
])
def test_conditional_particle_before_comma_is_not_a_fact(runner, text):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event') is None, text
