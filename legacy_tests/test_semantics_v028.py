"""v0.2.8 boundaries: real prompt construction, legacy queues and semantic scopes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_minichat import runner, UMO
from test_conflicts import at_time
from test_semantics_v027 import appt, sleep_until


LABELS = ['ALPHA', 'BETA', 'GAMMA']


def generation(runner, mode='conversation'):
    cfg = runner.cfg['context_settings']
    cfg['use_conversation_history'] = mode != 'platform'
    cfg['use_platform_history'] = mode in ('platform', 'hybrid')
    runner._load_history = AsyncMock(return_value=[{'role':'user', 'content':'earlier message'}])
    runner._load_persona = AsyncMock(return_value='test persona')
    runner._platform_block = AsyncMock(return_value='PLATFORM_HISTORY')
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='reply')))
    runner.context.get_using_provider = Mock(return_value=provider)
    runner._send_split = AsyncMock(return_value='reply')
    runner._append_assistant_history = AsyncMock()
    return provider


def queue(runner, now, count, with_ids):
    items = [appt(now+i*300, LABELS[i]) for i in range(count)]
    if with_ids:
        for item in items:
            assert runner._set_appointment(UMO, item)
        stored = runner._appointments(UMO)
        assert all(x.get('id') for x in stored)
        assert len({x['id'] for x in stored}) == count
    else:
        runner.store.set(UMO, 'appointments', items)


@pytest.mark.parametrize('with_ids', [False, True])
@pytest.mark.parametrize('followup', [False, True])
@pytest.mark.parametrize('count', [2, 3])
def test_sleep_deferred_prompts_match_selected_item(runner, with_ids, followup, count):
    async def scenario():
        with at_time(12) as now:
            runner.engine_cfg['appointment_followup_enabled'] = followup
            runner.engine_cfg['appointment_followup_minutes'] = 8
            runner.engine_cfg['appointment_max_followups'] = 1
            sleep_until(runner, now, now+7200)
            queue(runner, now, count, with_ids)
        for i in range(count):
            with at_time(12, i*5):
                await runner._fire(UMO)
        assert all(x.get('deferred') for x in runner._appointments(UMO))
        provider = generation(runner)
        for minute in ([1, 9] if followup else [1]):
            with at_time(14, minute):
                for _ in range(count):
                    selected = runner.appointment_shot_item(UMO)
                    assert selected
                    expected = selected['item']['label']
                    await runner._fire(UMO)
                    prompt = provider.text_chat.call_args.kwargs['prompt']
                    assert expected in prompt, {'expected':expected, 'prompt':prompt}
                    assert not any(x in prompt for x in LABELS if x != expected), prompt
        assert all(x['stage'] == 'done' for x in runner._appointments(UMO))
        assert provider.text_chat.await_count == count*(2 if followup else 1)
    asyncio.run(scenario())


@pytest.mark.parametrize('with_ids', [False, True])
@pytest.mark.parametrize('count', [1, 2, 3])
def test_failed_generation_only_abandons_selected_appointment(runner, with_ids, count):
    async def scenario():
        with at_time(12) as now:
            items = [appt(now, LABELS[i]) for i in range(count)]
            if with_ids:
                for i, item in enumerate(items):
                    item['id'] = f'fixture-id-{i}'
            runner.store.set(UMO, 'appointments', items)
            runner._generate = AsyncMock(return_value='')
            runner._send_split = AsyncMock()
            for _ in range(count):
                selected = runner.appointment_shot_item(UMO)['item']['label']
                for _ in range(2):
                    await runner._fire(UMO)
                stored = runner._appointments(UMO)
                target = next(x for x in stored if x['label'] == selected)
                assert target['stage'] == 'done' and target['fails'] == 2
                assert all(x.get('fails', 0) == 0 for x in stored if x['stage'] != 'done')
            assert all(x['stage'] == 'done' for x in runner._appointments(UMO))
            runner._send_split.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['conversation', 'platform', 'hybrid'])
def test_selected_appointment_identity_reaches_provider_with_context(runner, mode):
    async def scenario():
        with at_time(14, 1) as now:
            first, second = appt(now, LABELS[0]), appt(now, LABELS[1])
            first.update(id='first-id', stage='due', due_sent_at=now, follow_up_at=now+480)
            second['id'] = 'second-id'
            runner.store.set(UMO, 'appointments', [first, second])
            runner.engine_cfg['appointment_followup_enabled'] = False
            provider = generation(runner, mode)
            await runner._fire(UMO)
            call = provider.text_chat.call_args.kwargs
            assert LABELS[1] in call['prompt'] and LABELS[0] not in call['prompt']
            assert bool(call['contexts']) == (mode != 'platform')
            assert ('PLATFORM_HISTORY' in call['prompt']) == (mode != 'conversation')
            assert call['system_prompt'] == 'test persona'
            assert runner._appointments(UMO)[1]['stage'] == 'done'
    asyncio.run(scenario())


@pytest.mark.parametrize('text,expected', [
    ('我现在不得不去坐地铁了', 'commute'),
    ('我现在不得不去坐公交了', 'commute'),
    ('我现在不得不坐地铁', 'commute'),
    ('我现在不得不去看电影', 'movie'),
    ('我现在不得不继续开会', 'meeting'),
])
def test_complete_false_friend_survives_short_window(runner, text, expected):
    with at_time(12):
        runner.update_from_message(UMO, text)
        assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('text,expected', [
    ('听了朋友的话，我现在正在洗澡', 'shower'),
    ('照着医生的话，我现在正在运动', 'exercise'),
    ('我记着老师的话，我现在正在学习', 'study'),
    ('我想起妈妈的话，我现在正在吃饭', 'eating'),
    ('听了你的话，我现在正在洗澡', 'shower'),
    ('照着医生说的话，我现在正在运动', 'exercise'),
])
def test_nominal_dehua_before_comma_keeps_actual_action(runner, text, expected):
    with at_time(12):
        runner.update_from_message(UMO, text)
        assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('text', [
    '有空的话，我现在就去洗澡', '身体好的话，我就去运动',
    '明天没雨的话，我就去买菜', '如果下午有空的话，我就去看电影',
])
def test_actual_dehua_condition_is_not_a_current_fact(runner, text):
    with at_time(12):
        runner.update_from_message(UMO, text)
        assert runner.store.get(UMO, 'active_event') is None, text


@pytest.mark.parametrize('text', ['下午三点我去开会', '下午三点我去吃饭', '下午三点我出门'])
def test_clock_future_is_not_current_when_appointment_feature_is_disabled(runner, text):
    with at_time(10):
        runner.engine_cfg['enable_appointment'] = False
        runner.update_from_message(UMO, text)
        assert not runner._appointments(UMO)
        assert runner.store.get(UMO, 'active_event') is None, text


@pytest.mark.parametrize('text,expected', [
    ('我现在正在吃饭', 'eating'), ('我现在正在洗澡', 'shower'),
])
def test_disabling_appointments_keeps_actual_current_actions(runner, text, expected):
    with at_time(10):
        runner.engine_cfg['enable_appointment'] = False
        runner.update_from_message(UMO, text)
        assert runner.store.get(UMO, 'active_event', {}).get('name') == expected


@pytest.mark.parametrize('text,expected', [
    ('我现在正在吃饭不过下午三点要开会', 'eating'),
    ('下午三点要开会但我现在正在洗澡', 'shower'),
    ('我现在正在吃饭下午三点开会', 'eating'),
    ('下午三点开会现在我正在洗澡', 'shower'),
    ('我现在正在吃饭，不过下午三点要开会', 'eating'),
    ('下午三点要开会，但我现在正在洗澡', 'shower'),
    ('我现在正在吃饭，下午三点开会', 'eating'),
    ('下午三点开会，现在我正在洗澡', 'shower'),
])
def test_future_consumption_preserves_actual_action_in_same_message(runner, text, expected):
    with at_time(10):
        runner.update_from_message(UMO, text)
        assert len(runner._appointments(UMO)) == 1
        assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text
