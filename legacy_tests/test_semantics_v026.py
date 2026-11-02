"""Release 0.2.6 boundary tests; all model and platform calls remain simulated."""
import asyncio
import copy
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_minichat import runner, UMO
from test_conflicts import at_time
from test_semantics_v025 import ACTIVITIES, validate_tool_history


@pytest.mark.parametrize('name,action,stop', ACTIVITIES, ids=[a[0] for a in ACTIVITIES])
def test_dream_action_is_not_a_current_fact(runner, name, action, stop):
    text = f'我梦见自己正在{action}'
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event') is None, text


@pytest.mark.parametrize('name,action,stop', ACTIVITIES, ids=[a[0] for a in ACTIVITIES])
def test_conditional_scope_survives_comma(runner, name, action, stop):
    text = f'如果下雨，我就去{action}'
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event') is None, text


@pytest.mark.parametrize('text', [
    '我没有在坐公交', '我没在坐高铁', '我并没有坐地铁', '不是我在坐公交，是朋友',
    '我没有在看电视剧', '我根本没在看电影', '不是我在看电影，是朋友', '我没在看影片',
])
def test_negation_covers_auxiliary_before_short_keyword(runner, text):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event') is None, text


@pytest.mark.parametrize('text,expected', [
    ('吃饱了，我正在洗澡', 'shower'), ('吃完了，我正在写代码', 'working'),
    ('开饭了，我正在吃饭', 'eating'), ('吃不下了，我正在学习', 'study'),
])
def test_cooking_handoff_keeps_following_current_action(runner, text, expected):
    runner._set_event(UMO, runner.engine.get_rule('cooking'), 'setup')
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('text,expected', [
    ('吃完了我正在洗澡', 'shower'), ('吃完了然后我去洗澡了', 'shower'),
    ('吃完了现在我正在写代码', 'working'),
])
def test_completion_with_unpunctuated_next_action(runner, text, expected):
    runner._set_event(UMO, runner.engine.get_rule('eating'), 'setup')
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('text', [
    '我醒了', '醒了', '起床了', '我已经醒了', '我刚睡醒了',
    '我已经睡醒了', '我不睡了', '我睡不着',
])
def test_current_wake_positive_controls(runner, text):
    with at_time(1):
        runner.update_from_message(UMO, '我睡了')
        assert runner.is_sleeping(UMO)
        runner.update_from_message(UMO, text)
        assert not runner.is_sleeping(UMO), text


@pytest.mark.parametrize('text', [
    '他说“等会睡”', '示例："等会睡"', '要是我等会睡就好了',
    '我梦见自己等会睡', '昨天我说等会睡', '朋友等会睡',
])
def test_unreal_waiting_sleep_does_not_create_reminder(runner, text):
    with at_time(1):
        runner.update_from_message(UMO, text)
        assert not runner.is_waiting_to_sleep(UMO), text


@pytest.mark.parametrize('text', [
    '示例："我们一起出门"', '他说“我们牵手”', '我梦见我们一起出门',
    '我正在写代码，他说“我们一起出门”', '我和陌生人一起出门', '我和我妈一起出门',
])
def test_noncurrent_or_other_companion_does_not_set_together(runner, text):
    runner.update_from_message(UMO, text)
    assert not runner.store.get(UMO, 'together', {}).get('active'), text


@pytest.mark.parametrize('text', [
    '我们牵手', '我和你一起出门', '我陪你吃饭',
    '我和朋友告别了，现在和你一起出门',
    '别和朋友一起走，我现在和你一起出门',
    '刚才我们讨论了如果下雨怎么办，现在我们确实正在一起吃饭',
])
def test_real_bot_companion_positive_controls(runner, text):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'together', {}).get('active'), text


@pytest.mark.parametrize('first,second,expected', [
    ('你正在洗澡', '我正在洗澡', 'self'),
    ('我正在洗澡', '你正在洗澡', 'her'),
])
def test_same_event_renewal_updates_subject(runner, first, second, expected):
    runner.update_from_message(UMO, first)
    runner.update_from_message(UMO, second)
    assert runner.store.get(UMO, 'active_event')['subject'] == expected


@pytest.mark.parametrize('text,expected', [
    ('我正在吃饭顺便听你的话', 'eating'),
    ('我正在洗澡也在听你的话', 'shower'),
    ('我正在写代码同时听你的话', 'working'),
    ('我正在开会先别打断我的话', 'meeting'),
    ('我正在喝水也在听你的话', 'drinking'),
    ('我正在打游戏也在听你的话', 'gaming'),
])
def test_possessive_speech_is_not_a_conditional(runner, text, expected):
    runner.update_from_message(UMO, text)
    assert runner.store.get(UMO, 'active_event', {}).get('name') == expected, text


@pytest.mark.parametrize('conversation_id,forced_id,expected', [
    (None, 'forced', 'FORCED'), ('', 'forced', 'FORCED'),
    ('[%None]', 'forced', 'FORCED'), ('selected', '[%None]', ''),
])
def test_final_persona_resolution_overrides_local_fallback(runner, conversation_id, forced_id, expected):
    async def scenario():
        async def resolve(*, umo, conversation_persona_id, platform_name, provider_settings=None):
            return (forced_id, None if forced_id == '[%None]' else {'prompt':'FORCED'}, forced_id, False)
        runner.context.conversation_manager = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value='cid'),
            get_conversation=AsyncMock(return_value=types.SimpleNamespace(history='[]', persona_id=conversation_id)))
        runner.context.persona_manager = types.SimpleNamespace(
            resolve_selected_persona=resolve,
            get_persona_v3_by_id=Mock(return_value={'prompt':'SELECTED'}),
            get_default_persona_v3=AsyncMock(return_value={'prompt':'DEFAULT'}))
        assert await runner._load_persona(UMO) == expected
    asyncio.run(scenario())


@pytest.mark.parametrize('shared_id', [False, True])
@pytest.mark.parametrize('blank_first', [False, True])
def test_tool_repairs_respect_each_call_group(runner, shared_id, blank_first):
    history = []
    for i in range(2):
        cid = 'c' if shared_id else f'c{i}'
        history.extend([
            {'role':'assistant','content':'','tool_calls':[{'id':cid,'type':'function','function':{'name':'fn','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':cid,'content':[{'type':'text','text':'' if blank_first and i == 0 else 'result'}]},
            {'role':'assistant','content':f'answer{i}'},
        ])
    before = copy.deepcopy(history)
    validate_tool_history(runner._prepare_contexts(history, runner._context_settings()))
    assert history == before


@pytest.mark.parametrize('text,expected', [
    ('吃完了，我正在洗澡，下午三点开会', 'shower'),
    ('下班了，我正在吃饭，下午三点开会', 'eating'),
])
def test_compound_current_action_and_future_appointment(runner, text, expected):
    with at_time(10):
        runner._set_event(UMO, runner.engine.get_rule('eating'), 'setup')
        if text.startswith('下班'):
            runner.update_from_message(UMO, '我出门了')
        runner.update_from_message(UMO, text)
        assert len(runner._appointments(UMO)) == 1
        assert runner.store.get(UMO, 'appointment_counter')['n'] == 1
        assert runner.store.get(UMO, 'active_event', {}).get('name') == expected
