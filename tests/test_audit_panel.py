"""Panel boundary probes: origins, session revocation and failed configuration writes."""
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot_plugin_mitchell_minichat.core.panel_api import PanelError


def client(plugin, base_url='http://127.0.0.1:7627'):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plugin.panel_http._app, raise_app_exceptions=False),
        base_url=base_url,
    )


async def test_cross_origin_simple_form_cannot_disable_local_panel_plugin(plugin):
    # This is a valid text/plain HTML-form body: an input's name ends in the
    # opening pad quote and its value is '"}', so the inserted '=' stays JSON data.
    body = '{"patch":{"friend_settings":{"enable":false}},"pad":"="}\r\n'
    assert json.loads(body)['patch']['friend_settings']['enable'] is False
    async with client(plugin) as c:
        response = await c.post('/api/config/save', content=body, headers={
            'Origin': 'http://attacker.invalid', 'Content-Type': 'text/plain',
            'Sec-Fetch-Site': 'cross-site',
        })
    assert response.status_code in (400, 403, 415)
    assert plugin.friend_cfg['enable'] is True


async def test_same_site_other_origin_cannot_use_logged_in_cookie_to_change_state(plugin):
    plugin.config['web_panel']['password'] = 'audit-password'
    async with client(plugin) as c:
        assert (await c.post('/api/login', json={'password': 'audit-password'})).status_code == 200
        response = await c.post('/api/config/save', content='{"patch":{"friend_settings":{"enable":false}}}',
                                headers={'Origin': 'http://127.0.0.1:8765', 'Content-Type': 'text/plain',
                                         'Sec-Fetch-Site': 'same-site'})
    assert response.status_code in (400, 403, 415)
    assert plugin.friend_cfg['enable'] is True


async def test_bearer_logout_invalidates_the_bearer_session(plugin):
    plugin.config['web_panel']['password'] = 'audit-password'
    async with client(plugin) as c:
        token = (await c.post('/api/login', json={'password': 'audit-password'})).json()['data']['token']
        c.cookies.clear()
        header = {'Authorization': 'Bearer ' + token}
        assert (await c.get('/api/tasks', headers=header)).status_code == 200
        assert (await c.post('/api/logout', headers=header)).status_code == 200
        assert (await c.get('/api/tasks', headers=header)).status_code == 401


async def test_password_change_revokes_existing_sessions(plugin):
    plugin.config['web_panel']['password'] = 'old-audit-password'
    async with client(plugin) as c:
        token = (await c.post('/api/login', json={'password': 'old-audit-password'})).json()['data']['token']
        await plugin.panel_service.config_save({'patch': {'web_panel': {'password': 'new-audit-password'}}})
        c.cookies.clear()
        assert (await c.get('/api/tasks', headers={'Authorization': 'Bearer ' + token})).status_code == 401
        assert (await c.post('/api/login', json={'password': 'new-audit-password'})).status_code == 200


async def test_failed_configuration_save_does_not_apply_in_memory(plugin, monkeypatch):
    before = copy.deepcopy(dict(plugin.config))
    monkeypatch.setattr(plugin.config, 'save_config_async', AsyncMock(side_effect=OSError('disk full')))
    with pytest.raises(PanelError) as caught:
        await plugin.panel_service.config_save({'patch': {
            'friend_settings': {'enable': False},
            'web_panel': {'password': 'not-successfully-saved'},
        }})
    assert caught.value.code == 'E_CONFIG_SAVE'
    assert dict(plugin.config) == before


@pytest.mark.parametrize('body', [[1], 'not-an-object', 123])
async def test_login_rejects_non_object_bodies_without_internal_error(plugin, body):
    plugin.config['web_panel']['password'] = 'audit-password'
    async with client(plugin) as c:
        response = await c.post('/api/login', json=body)
    assert response.status_code == 400


@pytest.mark.parametrize('path', ['config', 'tasks', 'status', 'decisions', 'overrides', 'rules', 'schema'])
async def test_protected_read_endpoints_require_login(plugin, path):
    plugin.config['web_panel']['password'] = 'audit-password'
    async with client(plugin) as c:
        response = await c.get('/api/' + path)
    assert response.status_code == 401


@pytest.mark.parametrize('path', ['/../metadata.yaml', '/%2e%2e/metadata.yaml', '/..%2fmetadata.yaml'])
async def test_asset_path_does_not_escape_views(plugin, path):
    async with client(plugin) as c:
        response = await c.get(path)
    assert response.status_code == 404


async def test_same_origin_cookie_json_save_remains_usable(plugin):
    plugin.config['web_panel']['password'] = 'audit-password'
    async with client(plugin) as c:
        assert (await c.post('/api/login', json={'password': 'audit-password'},
                             headers={'Origin': 'http://127.0.0.1:7627'})).status_code == 200
        response = await c.post('/api/config/save', json={'patch': {'web_panel': {'chart_ceiling_pct': 15}}},
                                headers={'Origin': 'http://127.0.0.1:7627'})
    assert response.status_code == 200
    assert plugin.config['web_panel']['chart_ceiling_pct'] == 15


async def test_explicit_valid_bearer_can_call_from_other_origin(plugin):
    plugin.config['web_panel']['password'] = 'audit-password'
    async with client(plugin) as c:
        token = (await c.post('/api/login', json={'password': 'audit-password'})).json()['data']['token']
        c.cookies.clear()
        response = await c.post('/api/config/save', json={'patch': {'web_panel': {'chart_ceiling_pct': 15}}},
                                headers={'Origin': 'https://explicit-api-client.invalid', 'Authorization': 'Bearer ' + token})
    assert response.status_code == 200


async def test_https_proxy_origin_with_forwarded_host_remains_usable(plugin):
    async with client(plugin, base_url='http://panel.example') as c:
        response = await c.post('/api/config/save', json={'patch': {'web_panel': {'chart_ceiling_pct': 15}}},
                                headers={'Origin': 'https://panel.example'})
    assert response.status_code == 200


@pytest.mark.parametrize('origin', ['null', 'http://attacker.invalid', 'http://127.0.0.1:9999',
                                   'http://127.0.0.1:7627/path', 'http://user@127.0.0.1:7627'])
async def test_unsafe_origins_cannot_modify_even_with_json(plugin, origin):
    async with client(plugin) as c:
        response = await c.post('/api/config/save', json={'patch': {'friend_settings': {'enable': False}}},
                                headers={'Origin': origin})
    assert response.status_code == 403
    assert plugin.friend_cfg['enable']
