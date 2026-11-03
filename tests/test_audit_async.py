"""Audit: an acknowledgement must not resurrect manually invalidated work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain


async def send(plugin, umo, text):
    await plugin.on_private_message(SimpleNamespace(
        unified_msg_origin=umo, message_str=text, get_messages=lambda: [Plain(text)],
    ))


async def drain(plugin):
    for result in await asyncio.gather(*list(plugin._bg_tasks), return_exceptions=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


@pytest.mark.parametrize('action', ['clear_event', 'clear_sleep', 'clear_appointment', 'remove_add'])
async def test_panel_invalidation_survives_later_ack(plugin, umo, clock, monkeypatch, action):
    started, release = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        started.set()
        await release.wait()
        return {'name': 'study', 'done': False}

    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    await asyncio.wait_for(started.wait(), 2)
    try:
        if action == 'remove_add':
            plugin.panel_service.task_action({'action': 'remove', 'umo': umo})
            plugin.panel_service.task_action({'action': 'add', 'umo': umo})
        else:
            plugin.panel_service.task_action({'action': action, 'umo': umo})
        await send(plugin, umo, 'OK')
    finally:
        release.set()
    await drain(plugin)
    assert plugin.store.get(umo, 'active_event') is None


@pytest.mark.parametrize('action', ['clear_event', 'remove_add'])
async def test_pending_batch_not_resurrected_after_panel_and_ack(plugin, umo, clock, monkeypatch, action):
    classify = AsyncMock(return_value={'name': 'study', 'done': False})
    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    if action == 'remove_add':
        plugin.panel_service.task_action({'action': 'remove', 'umo': umo})
        plugin.panel_service.task_action({'action': 'add', 'umo': umo})
    else:
        plugin.panel_service.task_action({'action': action, 'umo': umo})
    await send(plugin, umo, 'OK')
    await drain(plugin)
    assert plugin.store.get(umo, 'active_event') is None
    classify.assert_not_awaited()


async def test_panel_barrier_drops_old_fragment_before_new_activity(plugin, umo, clock, monkeypatch):
    classify = AsyncMock(return_value={'name': 'study', 'done': False})
    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备去冲个澡了')
    plugin.panel_service.task_action({'action': 'clear_event', 'umo': umo})
    await send(plugin, umo, '我准备埋头码字了')
    await drain(plugin)
    classify.assert_awaited_once()
    assert classify.await_args.args[1] == '我准备埋头码字了'
    assert plugin.store.get(umo, 'active_event')['name'] == 'study'


async def test_disable_reenable_invalidates_old_request(plugin, umo, clock, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        started.set()
        await release.wait()
        return {'name': 'study', 'done': False}

    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    await asyncio.wait_for(started.wait(), 2)
    try:
        await plugin.panel_service.config_save({'patch': {'friend_settings': {'enable': False}}})
        await plugin.panel_service.config_save({'patch': {'friend_settings': {'enable': True}}})
    finally:
        release.set()
    await drain(plugin)
    assert plugin.store.get(umo, 'active_event') is None


@pytest.mark.parametrize('mode', ['event', 'appointment'])
@pytest.mark.parametrize('action', ['clear', 'remove_add'])
async def test_gray_review_not_resurrected_by_ack(plugin, umo, clock, monkeypatch, mode, action):
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(completion_text='YES')

    monkeypatch.setattr(plugin.runner, '_recent_dialogue', AsyncMock(return_value=''))
    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=answer)
    await send(plugin, umo, '今天洗澡' if mode == 'event' else '我明天下午三点左右开会')
    await asyncio.wait_for(started.wait(), 2)
    try:
        if action == 'remove_add':
            plugin.panel_service.task_action({'action': 'remove', 'umo': umo})
            plugin.panel_service.task_action({'action': 'add', 'umo': umo})
        else:
            plugin.panel_service.task_action({
                'action': 'clear_event' if mode == 'event' else 'clear_appointment', 'umo': umo,
            })
        await send(plugin, umo, 'OK')
    finally:
        release.set()
    await drain(plugin)
    assert plugin.store.get(umo, 'active_event') is None
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('text', ['', ' ', '\n', '\t'])
async def test_non_text_message_does_not_drop_previous_activity(plugin, umo, clock, monkeypatch, text):
    classify = AsyncMock(return_value={'name': 'study', 'done': False})
    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    plugin.store.set(umo, 'unanswered', 2)
    await send(plugin, umo, text)
    await drain(plugin)
    classify.assert_awaited_once()
    assert classify.await_args.args[1] == '我准备埋头码字了'
    assert plugin.store.get(umo, 'active_event')['name'] == 'study'
    assert plugin.store.get(umo, 'unanswered') == 0
