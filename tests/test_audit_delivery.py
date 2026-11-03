"""Manual delivery and configuration changes must respect newer user intent."""
import asyncio
import copy
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_mitchell_minichat.core.panel_api import PanelError


@pytest.mark.parametrize('change', ['message', 'remove', 'clear_event', 'disable'])
async def test_manual_generation_cannot_send_after_invalidation(plugin, umo, clock, monkeypatch, change):
    runner = plugin.runner
    runner.started = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return '旧场景的消息。'

    monkeypatch.setattr(runner, '_generate', generate)
    sending = asyncio.create_task(plugin.panel_service.send({'umo': umo, 'reschedule': True}))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        if change == 'message':
            runner.update_from_message(umo, '我正在吃饭')
        elif change == 'disable':
            await plugin.panel_service.config_save({'patch': {'friend_settings': {'enable': False}}})
        else:
            plugin.panel_service.task_action({'action': change, 'umo': umo})
    finally:
        release.set()
    result = await sending
    assert not result['sent']
    plugin.context.send_message.assert_not_awaited()
    assert plugin.store.get(umo, 'unanswered', 0) == 0


async def test_manual_partial_send_preserves_new_counter_and_schedule(plugin, umo, clock, monkeypatch):
    runner = plugin.runner
    runner.started = True
    monkeypatch.setattr(runner, '_append_assistant_history', AsyncMock())
    reschedule = Mock()
    monkeypatch.setattr(runner, 'reschedule', reschedule)

    async def delivered(*args):
        runner.update_from_message(umo, '我正在吃饭')
        plugin.store.reset_unanswered(umo)
        plugin.store.set(umo, 'next_at', clock.now + 777)
        return True

    plugin.context.send_message.side_effect = delivered
    result = await plugin.panel_service.send({'umo': umo, 'text': '第一段。第二段。', 'reschedule': True})
    assert result['sent'] and result['delivery_status'] == 'partial'
    assert plugin.context.send_message.await_count == 1
    assert plugin.store.get(umo, 'unanswered', 0) == 0
    assert plugin.store.get(umo, 'next_at') == clock.now + 777
    assert runner._append_assistant_history.await_args.args[1] == '第一段。'
    reschedule.assert_not_called()


async def test_failed_config_save_restores_active_nested_configuration(plugin, umo, monkeypatch):
    before = copy.deepcopy(dict(plugin.config))
    friend = plugin.friend_cfg
    engine = plugin.engine_cfg
    monkeypatch.setattr(plugin.config, 'save_config_async', AsyncMock(side_effect=OSError('synthetic disk failure')))
    with pytest.raises(PanelError) as error:
        await plugin.panel_service.config_save({'patch': {
            'friend_settings': {'enable': False, 'schedule_settings': {'min_interval_minutes': 10}},
            'custom_engine': {'enable_gray_review': False},
            'web_panel': {'password': 'replacement'},
        }})
    assert error.value.code == 'E_CONFIG_SAVE'
    assert dict(plugin.config) == before
    assert friend is plugin.friend_cfg and engine is plugin.engine_cfg
    assert friend['enable'] == before['friend_settings']['enable']


async def test_cancelled_config_save_restores_configuration(plugin, monkeypatch):
    before = copy.deepcopy(dict(plugin.config))
    entered = asyncio.Event()

    async def persist():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(plugin.config, 'save_config_async', persist)
    saving = asyncio.create_task(plugin.panel_service.config_save({'patch': {'friend_settings': {'enable': False}}}))
    await asyncio.wait_for(entered.wait(), 2)
    saving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await saving
    assert dict(plugin.config) == before


async def test_failed_save_cannot_roll_back_a_later_successful_save(plugin, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    original_minimum = plugin.friend_cfg['schedule_settings']['min_interval_minutes']

    async def persist():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise OSError('first save failed')

    monkeypatch.setattr(plugin.config, 'save_config_async', persist)
    first = asyncio.create_task(plugin.panel_service.config_save({'patch': {
        'friend_settings': {'schedule_settings': {'min_interval_minutes': 10}},
    }}))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(plugin.panel_service.config_save({'patch': {
        'friend_settings': {'schedule_settings': {'max_interval_minutes': 200}},
    }}))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], PanelError) and results[1]['saved']
    assert plugin.friend_cfg['schedule_settings']['min_interval_minutes'] == original_minimum
    assert plugin.friend_cfg['schedule_settings']['max_interval_minutes'] == 200


async def test_enable_cycle_invalidates_pending_work_and_restarts_scheduling(plugin, umo, clock, monkeypatch):
    runner = plugin.runner
    runner.started = True
    runner.update_from_message(umo, '我准备去冲个澡了')
    old_seq = plugin.store.get(umo, 'turn_seq')
    await plugin.panel_service.config_save({'patch': {'friend_settings': {'enable': False}}})
    await plugin.panel_service.config_save({'patch': {'friend_settings': {'enable': True}}})
    runner.update_from_message(umo, 'OK')
    assert not runner._semantic_current(umo, old_seq)
    assert umo in runner.timers


async def test_config_session_changes_start_new_and_cancel_removed_timer(plugin, umo, clock):
    runner = plugin.runner
    runner.started = True
    runner.reschedule(umo, delay_minutes=10)
    old_task = runner.timers[umo]
    other = 'test:FriendMessage:2'
    await plugin.panel_service.config_save({'patch': {'friend_settings': {'session_list': [other]}}})
    await asyncio.sleep(0)
    assert old_task.cancelled() and umo not in runner.timers
    assert other in runner.timers
