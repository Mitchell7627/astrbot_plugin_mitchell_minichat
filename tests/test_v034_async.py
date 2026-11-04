import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from astrbot.core.message.components import Plain

async def send(p,u,t):
    await p.on_private_message(SimpleNamespace(unified_msg_origin=u,message_str=t,get_messages=lambda:[Plain(t)]))

@pytest.mark.parametrize('tail',['慢慢来','没关系','没关系的','没事','没事的','别着急','嗯嗯，知道啦','慢慢来😊'])
async def test_observe_neutral_variants(plugin,umo,clock,monkeypatch,tail):
    started,release=asyncio.Event(),asyncio.Event()
    async def answer(**kwargs):
        started.set();await release.wait()
        return SimpleNamespace(completion_text='YES')
    monkeypatch.setattr(plugin.runner,'_recent_dialogue',AsyncMock(return_value=''))
    monkeypatch.setattr(plugin.runner,'classify_text',AsyncMock(return_value=None))
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=answer)
    await send(plugin,umo,'我明天下午三点左右开会')
    await asyncio.wait_for(started.wait(),3)
    try:await send(plugin,umo,tail)
    finally:release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    count=len(plugin.runner._appointments(umo))
    print('OBS '+json.dumps({'kind':'appointment-neutral-variant','tail':tail,'registered':count},ensure_ascii=True))
    assert count == 1

@pytest.mark.parametrize('tail',['慢慢来','没关系','不着急'])
@pytest.mark.parametrize('correction',['刚才说的是我室友','我不去开会了','明天不去了','这条提醒取消了'])
async def test_correction_after_neutral_drops_review(plugin,umo,clock,monkeypatch,tail,correction):
    started,release=asyncio.Event(),asyncio.Event()
    async def answer(**kwargs):
        started.set();await release.wait()
        return SimpleNamespace(completion_text='YES')
    monkeypatch.setattr(plugin.runner,'_recent_dialogue',AsyncMock(return_value=''))
    monkeypatch.setattr(plugin.runner,'classify_text',AsyncMock(return_value=None))
    plugin.context.get_using_provider=lambda **kw:SimpleNamespace(text_chat=answer)
    await send(plugin,umo,'我明天下午三点左右开会')
    await asyncio.wait_for(started.wait(),3)
    try:
        await send(plugin,umo,tail)
        await send(plugin,umo,correction)
        await send(plugin,umo,tail)
    finally:release.set()
    await asyncio.gather(*list(plugin._bg_tasks))
    assert not plugin.runner._appointments(umo)
