"""Neutral follow-ups preserve pending appointment reviews without reviving old intent."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.message.components import Plain

async def send(plugin, umo, text):
    await plugin.on_private_message(SimpleNamespace(
        unified_msg_origin=umo, message_str=text, get_messages=lambda: [Plain(text)]))

async def drain(plugin):
    for result in await asyncio.gather(*list(plugin._bg_tasks), return_exceptions=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result

TAILS=['不错','没关系','别急','还行','不着急','慢慢来','慢慢来呀','没关系哦','别急啊！',' 不着急。 ']


@pytest.mark.parametrize('tail',TAILS)
@pytest.mark.parametrize('mode',['activity','event','appointment'])
async def test_neutral_reply_preserves_inflight_result(plugin,umo,clock,monkeypatch,tail,mode):
    started,release=asyncio.Event(),asyncio.Event()
    async def answer(**kwargs):
        started.set();await release.wait()
        return SimpleNamespace(completion_text='YES')
    async def classify(*args):
        started.set();await release.wait()
        return {'name':'study','done':False}
    monkeypatch.setattr(plugin.runner,'_recent_dialogue',AsyncMock(return_value=''))
    monkeypatch.setattr(plugin.runner,'classify_text',classify)
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=answer)
    text={'activity':'我准备埋头码字了','event':'今天洗澡','appointment':'我明天下午三点左右开会'}[mode]
    await send(plugin,umo,text)
    await asyncio.wait_for(started.wait(),3)
    try:await send(plugin,umo,tail)
    finally:release.set()
    await drain(plugin)
    if mode=='appointment':assert len(plugin.runner._appointments(umo))==1
    else:assert plugin.store.get(umo,'active_event')['name']==('study' if mode=='activity' else 'shower')


@pytest.mark.parametrize('tail',TAILS[:6])
async def test_neutral_reply_preserves_queued_activity(plugin,umo,clock,monkeypatch,tail):
    classify=AsyncMock(return_value={'name':'study','done':False})
    monkeypatch.setattr(plugin.runner,'classify_text',classify)
    await send(plugin,umo,'我准备埋头码字了')
    await send(plugin,umo,tail)
    await drain(plugin)
    classify.assert_awaited_once()
    assert classify.await_args.args[1]=='我准备埋头码字了'
    assert plugin.store.get(umo,'active_event')['name']=='study'


@pytest.mark.parametrize('tail',TAILS[:6])
@pytest.mark.parametrize('action',['clear_appointment','remove_add','new_intent'])
async def test_neutral_reply_cannot_revive_invalidated_appointment(plugin,umo,clock,monkeypatch,tail,action):
    started,release=asyncio.Event(),asyncio.Event()
    async def answer(**kwargs):
        started.set();await release.wait()
        return SimpleNamespace(completion_text='YES')
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=answer)
    monkeypatch.setattr(plugin.runner,'_recent_dialogue',AsyncMock(return_value=''))
    await send(plugin,umo,'我明天下午三点左右开会')
    await asyncio.wait_for(started.wait(),3)
    try:
        if action=='new_intent':await send(plugin,umo,'刚才说的是我室友')
        elif action=='remove_add':
            for a in ('remove','add'):plugin.panel_service.task_action({'action':a,'umo':umo})
        else:plugin.panel_service.task_action({'action':action,'umo':umo})
        await send(plugin,umo,tail)
    finally:release.set()
    await drain(plugin)
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('text',['慢慢来？','没关系吗','别急着洗澡','慢慢来，我不去了','算了','不是我','明天吧','我在忙','取消','不着急开会'])
def test_short_or_related_text_is_not_automatically_neutral(plugin,text):
    assert not plugin.runner.is_confirmation(text)


@pytest.mark.parametrize('tail',TAILS[:6])
async def test_custom_keyword_keeps_priority(plugin,umo,clock,monkeypatch,tail):
    plugin.runner.engine.get_rule('eating')['keywords'].append(tail)
    review=AsyncMock()
    monkeypatch.setattr(plugin.runner,'review_gray',review)
    await send(plugin,umo,tail)
    await drain(plugin)
    assert not plugin.runner.neutral_turn(umo)
    assert review.await_count or (plugin.store.get(umo,'active_event') or {}).get('name')=='eating'

