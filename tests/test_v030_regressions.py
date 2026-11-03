"""Neutral follow-ups preserve pending recognition; actual intent still wins."""
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


@pytest.mark.parametrize('tail', ['好', '好。', '对啊', '嗯呢', '行', '可以', 'OK', 'ok', 'Ok！'])
async def test_short_ack_preserves_inflight_activity(plugin, umo, clock, monkeypatch, tail):
    started, release = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        started.set()
        await release.wait()
        return {'name': 'study', 'done': False}

    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    await asyncio.wait_for(started.wait(), 2)
    try:
        await send(plugin, umo, tail)
    finally:
        release.set()
    await drain(plugin)
    assert plugin.store.get(umo, 'active_event')['name'] == 'study'


@pytest.mark.parametrize('tail', ['好', '对啊', '嗯呢', '行', '可以', 'OK'])
async def test_short_ack_preserves_appointment_review(plugin, umo, clock, tail):
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(completion_text='YES')

    plugin.context.get_using_provider = lambda **kw: SimpleNamespace(text_chat=answer)
    await send(plugin, umo, '我明天下午三点左右开会')
    await asyncio.wait_for(started.wait(), 2)
    try:
        await send(plugin, umo, tail)
    finally:
        release.set()
    await drain(plugin)
    assert len(plugin.runner._appointments(umo)) == 1


@pytest.mark.parametrize('tail', [
    '我正在吃饭', '我去睡觉了', '我不去冲个澡', '明天吧',
    '刚才说的是我室友', '我明天下午三点开会', '可以不去洗澡吗',
])
async def test_real_intent_after_ack_still_invalidates_old_result(plugin, umo, clock, monkeypatch, tail):
    started, release = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        started.set()
        await release.wait()
        return {'name': 'shower', 'done': False}

    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备去冲个澡了')
    await asyncio.wait_for(started.wait(), 2)
    try:
        await send(plugin, umo, 'OK')
        await send(plugin, umo, tail)
    finally:
        release.set()
    await drain(plugin)
    assert (plugin.store.get(umo, 'active_event') or {}).get('name') != 'shower'


@pytest.mark.parametrize('word', ['好', '行', '可以', 'OK'])
async def test_configured_ack_keyword_is_not_neutral(plugin, umo, clock, monkeypatch, word):
    plugin.runner.engine.get_rule('eating')['keywords'].append(word)
    review = AsyncMock()
    monkeypatch.setattr(plugin.runner, 'review_gray', review)
    await send(plugin, umo, word)
    await drain(plugin)
    assert not plugin.runner.neutral_turn(umo)
    assert review.await_count or (plugin.store.get(umo, 'active_event') or {}).get('name') == 'eating'


@pytest.mark.parametrize('text', ['行程改了', '好想洗澡', '可以去睡觉了', 'OK，我不去洗澡', '好？'])
def test_ack_filter_requires_complete_unambiguous_text(plugin, text):
    assert not plugin.runner.is_confirmation(text)


@pytest.mark.parametrize('tail', ['好', '对啊', '嗯呢', '行', '可以', 'OK'])
async def test_short_ack_before_model_starts_preserves_activity(plugin, umo, clock, monkeypatch, tail):
    classify = AsyncMock(return_value={'name': 'study', 'done': False})
    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    await send(plugin, umo, tail)
    await drain(plugin)
    classify.assert_awaited_once()
    assert plugin.store.get(umo, 'active_event')['name'] == 'study'


async def test_shutdown_cancels_recognition_before_it_can_apply(plugin, umo, clock, monkeypatch):
    classify = AsyncMock(return_value={'name': 'study', 'done': False})
    monkeypatch.setattr(plugin.runner, 'classify_text', classify)
    await send(plugin, umo, '我准备埋头码字了')
    await send(plugin, umo, 'OK')
    await plugin.terminate()
    assert not plugin._bg_tasks
    assert plugin.store.get(umo, 'active_event') is None
    classify.assert_not_awaited()
