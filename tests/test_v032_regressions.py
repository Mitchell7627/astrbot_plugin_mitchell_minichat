"""Historical gaps: dated appointments and failed non-config persistence paths."""
import copy
import asyncio
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from astrbot_plugin_mitchell_minichat.core.panel_api import PanelError


@pytest.mark.parametrize('text,hour', [('今天晚上十点到家',22),('11月2日晚上十点到家',22),('周一晚上十点到家',22)])
def test_explicit_today_uses_calendar_range_not_bare_hour_limit(plugin,umo,clock,text,hour):
    plugin.engine_cfg['appointment_max_hours']=1
    plugin.runner.update_from_message(umo,text)
    assert [a['at'] for a in plugin.runner._appointments(umo)]==[datetime(2026,11,2,hour).timestamp()]


def test_bare_clock_still_obeys_hour_limit(plugin,umo,clock):
    plugin.engine_cfg['appointment_max_hours']=1
    plugin.runner.update_from_message(umo,'晚上十点到家')
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize('action',['add','remove'])
def test_failed_session_edit_does_not_change_list_or_timers(plugin,umo,clock,monkeypatch,action):
    before=copy.deepcopy(dict(plugin.config))
    monkeypatch.setattr(plugin.config,'save_config',Mock(side_effect=OSError('synthetic disk failure')))
    with pytest.raises(PanelError):
        plugin.panel_service.task_action({'action':action,'umo':umo if action=='remove' else 'test:FriendMessage:2'})
    assert dict(plugin.config)==before


async def test_failed_rules_save_does_not_change_configuration(plugin,monkeypatch):
    before=copy.deepcopy(dict(plugin.config))
    monkeypatch.setattr(plugin.config,'save_config_async',AsyncMock(side_effect=OSError('synthetic disk failure')))
    with pytest.raises(PanelError):
        await plugin.panel_service.rules_save({'rules':[{'name':'new_activity','keywords':['新活动']}]})
    assert dict(plugin.config)==before


async def test_failed_panel_restart_save_does_not_change_address_or_restart(plugin,monkeypatch):
    before=copy.deepcopy(dict(plugin.config))
    monkeypatch.setattr(plugin.config,'save_config_async',AsyncMock(side_effect=OSError('synthetic disk failure')))
    restart=AsyncMock()
    monkeypatch.setattr(plugin,'restart_panel',restart)
    with pytest.raises(PanelError):
        await plugin.panel_service.panel_restart({'host':'127.0.0.1','port':8888})
    assert dict(plugin.config)==before
    restart.assert_not_awaited()


def test_failed_override_save_reports_failure_and_preserves_active_prompt(plugin,monkeypatch):
    before=plugin.web_overrides_all()
    real=Path.write_text
    def fail(path,*args,**kwargs):
        if path==plugin._web_overrides_path or path==plugin._web_overrides_path.with_suffix('.tmp'):
            raise OSError('synthetic disk failure')
        return real(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',fail)
    with pytest.raises(PanelError):
        plugin.panel_service.overrides_save({'overrides':{'classify_sys_prompt':'new test prompt'}})
    assert plugin.web_overrides_all()==before


@pytest.mark.parametrize('text', ['今天晚上十点到家','今晚差一刻零点到家','今天下午三点开会'])
def test_today_date_cancels_and_undoes_with_same_calendar_target(plugin,umo,clock,text):
    plugin.engine_cfg['appointment_max_hours']=1
    r=plugin.runner
    r.update_from_message(umo,text)
    before=copy.deepcopy(r._appointments(umo))
    assert len(before)==1
    r.update_from_message(umo,text+'的安排取消了')
    assert not r._appointments(umo)
    record=next(x for x in r.decisions.items(umo) if x.get('undo_state')=='available' and '取消' in x['text'])
    r.undo_decision(record['id'])
    assert r._appointments(umo)==before


@pytest.mark.parametrize('text', ['昨天晚上十点到家','11月31日晚上十点到家','十五天后晚上十点到家','今天上午九点到家'])
def test_dated_range_fix_does_not_accept_invalid_or_past_dates(plugin,umo,clock,text):
    plugin.runner.update_from_message(umo,text)
    assert not plugin.runner._appointments(umo)


async def test_successful_overrides_survive_reload(plugin):
    patch={'classify_sys_prompt':'test catalog {{catalog}}'}
    result=plugin.panel_service.overrides_save({'overrides':patch})
    assert result['saved']
    plugin._web_overrides={}
    plugin._load_web_overrides()
    assert plugin.web_override('classify_sys_prompt')==patch['classify_sys_prompt']


async def test_atomic_override_replace_failure_preserves_old_file_and_prompt(plugin,monkeypatch):
    from astrbot_plugin_mitchell_minichat.core import persistence
    plugin.set_web_overrides({'classify_sys_prompt':'old prompt'})
    before=plugin._web_overrides_path.read_bytes()
    real=persistence.os.replace
    def fail(source,target):
        if Path(target)==plugin._web_overrides_path: raise OSError('synthetic replace failure')
        return real(source,target)
    monkeypatch.setattr(persistence.os,'replace',fail)
    with pytest.raises(PanelError):
        plugin.panel_service.overrides_save({'overrides':{'classify_sys_prompt':'new prompt'}})
    assert plugin._web_overrides_path.read_bytes()==before
    assert plugin.web_override('classify_sys_prompt')=='old prompt'


async def test_fallback_rule_write_failure_preserves_file_and_engine(plugin,monkeypatch):
    from astrbot_plugin_mitchell_minichat.core import persistence
    plugin.engine_cfg['event_rules']=[]
    plugin.engine._load()
    path=plugin.panel_service._rules_file()
    before=path.read_bytes()
    rules=plugin.engine.rules_snapshot()
    real=persistence.os.replace
    def fail(source,target):
        if Path(target)==path: raise OSError('synthetic replace failure')
        return real(source,target)
    monkeypatch.setattr(persistence.os,'replace',fail)
    with pytest.raises(PanelError):
        await plugin.panel_service.rules_save({'rules':[{'name':'new','keywords':['新活动']}]})
    assert path.read_bytes()==before and plugin.engine.rules_snapshot()==rules


async def test_fallback_rule_success_is_applied_and_reload_failure_is_reported(plugin,monkeypatch):
    plugin.engine_cfg['event_rules']=[]
    plugin.engine._load()
    assert (await plugin.panel_service.rules_save({'rules':[{'name':'new','keywords':['新活动'] }]}))['saved']
    assert plugin.engine.get_rule('new')
    monkeypatch.setattr(plugin.engine,'_load',Mock(side_effect=RuntimeError('synthetic reload failure')))
    result=await plugin.panel_service.rules_save({'rules':[{'name':'later','keywords':['稍后活动']}]})
    assert result['saved'] and result['need_reload']
    assert json.loads(plugin.panel_service._rules_file().read_text(encoding='utf-8'))['rules'][0]['name']=='later'


@pytest.mark.parametrize('port',[1,65536,-1,'invalid',8888.5,float('inf')])
async def test_invalid_restart_port_does_not_save_or_restart(plugin,monkeypatch,port):
    before=copy.deepcopy(dict(plugin.config))
    restart=AsyncMock()
    monkeypatch.setattr(plugin,'restart_panel',restart)
    with pytest.raises(PanelError): await plugin.panel_service.panel_restart({'port':port})
    assert dict(plugin.config)==before
    restart.assert_not_awaited()


async def test_session_edits_cannot_interleave_with_async_rules_save(plugin,umo,monkeypatch):
    started,release=asyncio.Event(),asyncio.Event()
    async def persist(): started.set();await release.wait()
    monkeypatch.setattr(plugin.config,'save_config_async',persist)
    saving=asyncio.create_task(plugin.panel_service.rules_save({'rules':[{'name':'new','keywords':['新活动']}]}))
    await asyncio.wait_for(started.wait(),2)
    try:
        with pytest.raises(PanelError) as caught:
            plugin.panel_service.task_action({'action':'remove','umo':umo})
        assert caught.value.code=='E_BUSY'
        assert umo in plugin.runner.sessions()
    finally: release.set()
    assert (await saving)['saved']


@pytest.mark.parametrize('patch',[{'ttl_minutes':'invalid'},{'ttl_minutes':float('inf')},
                                 {'boost':float('nan')},{'boost':float('inf')},{'name':123},{'name':['bad']},
                                 {'boost_budget':float('inf')}])
async def test_malformed_rules_cannot_be_persisted_and_break_reload(plugin,monkeypatch,patch):
    before=copy.deepcopy(dict(plugin.config))
    save=AsyncMock()
    monkeypatch.setattr(plugin.config,'save_config_async',save)
    with pytest.raises(PanelError) as caught:
        await plugin.panel_service.rules_save({'rules':[{'name':'test','keywords':['test activity'],**patch}]})
    assert caught.value.code=='E_BAD_REQUEST'
    assert dict(plugin.config)==before
    save.assert_not_awaited()


@pytest.mark.parametrize('history',['[]','',[]])
async def test_first_proactive_message_is_recorded_for_empty_history(plugin,umo,history):
    manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='conversation-1'),
                            get_conversation=AsyncMock(return_value=SimpleNamespace(history=copy.deepcopy(history))),
                            update_conversation=AsyncMock())
    plugin.context.conversation_manager=manager
    await plugin.runner._append_assistant_history(umo,'first proactive message')
    manager.update_conversation.assert_awaited_once()
    saved=manager.update_conversation.await_args.kwargs['history']
    assert saved==[{'role':'assistant','content':'first proactive message'}]


@pytest.mark.parametrize('rule',[{'name':'bad','ttl_minutes':'invalid'},{'name':123},{'name':'bad','boost':float('inf')}])
async def test_generic_configuration_save_cannot_bypass_rule_validation(plugin,monkeypatch,rule):
    before=copy.deepcopy(dict(plugin.config))
    save=AsyncMock()
    monkeypatch.setattr(plugin.config,'save_config_async',save)
    with pytest.raises(PanelError):
        await plugin.panel_service.config_save({'patch':{'custom_engine':{'event_rules':[rule]}}})
    assert dict(plugin.config)==before
    save.assert_not_awaited()


async def test_empty_rule_configuration_still_selects_file_fallback(plugin):
    result=await plugin.panel_service.config_save({'patch':{'custom_engine':{'event_rules':[]}}})
    assert result['saved']
    assert plugin.panel_service.rules_get()['source']=='file'


def test_atomic_prompt_save_preserves_existing_permissions(plugin):
    plugin.set_web_overrides({'classify_sys_prompt':'old'})
    path=plugin._web_overrides_path
    path.chmod(0o600)
    before=stat.S_IMODE(path.stat().st_mode)
    plugin.set_web_overrides({'classify_sys_prompt':'new'})
    assert stat.S_IMODE(path.stat().st_mode)==before
    assert plugin.web_override('classify_sys_prompt')=='new'


def test_new_prompt_file_is_private_on_posix(plugin):
    plugin.set_web_overrides({'classify_sys_prompt':'test prompt'})
    assert plugin._web_overrides_path.is_file()
    if os.name=='posix':
        assert stat.S_IMODE(plugin._web_overrides_path.stat().st_mode)==0o600


@pytest.mark.parametrize('route',['rules','config'])
async def test_invalid_engine_can_be_repaired_from_panel_without_engine_dependency(plugin,route):
    plugin.engine=None
    rules=[{'name':'repaired','keywords':['修复后的活动'],'ttl_minutes':30}]
    if route=='rules':
        result=await plugin.panel_service.rules_save({'rules':rules})
    else:
        result=await plugin.panel_service.config_save({'patch':{'custom_engine':{'event_rules':rules}}})
    assert result['saved'] and result['need_reload']
    assert plugin.config['custom_engine']['event_rules'][0]['name']=='repaired'
