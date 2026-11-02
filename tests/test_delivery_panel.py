import asyncio
import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot_plugin_mitchell_minichat.core.panel_api import PanelError


def ready(plugin, monkeypatch, text="你好。再见。"):
    r = plugin.runner
    r.started = True
    monkeypatch.setattr(r, "_finish_cycle", lambda u: None)
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value=text))
    monkeypatch.setattr(r, "_append_assistant_history", AsyncMock())
    monkeypatch.setattr(r, "should_remind_sleep", lambda umo: False)
    return r


@pytest.mark.parametrize(
    "successes,status,count", [(0, "not_sent", 0), (1, "partial", 1), (2, "sent", 2)]
)
async def test_delivery_reports_actual_segments(
    plugin, umo, monkeypatch, successes, status, count
):
    r = ready(plugin, monkeypatch)
    plugin.context.send_message.side_effect = [True] * successes + [False]
    result = await plugin.panel_service.fire({"umo": umo})
    assert result["delivery_status"] == status
    assert result["sent"] == bool(count)
    assert result["planned_segments"] == 2 and result["delivered_segments"] == count
    if count:
        recorded = r._append_assistant_history.await_args.args[1]
        assert recorded == ("你好。" if count == 1 else "你好。\n再见。")
    else:
        r._append_assistant_history.assert_not_awaited()


async def test_sleep_reminder_sent_without_counter_increment(plugin, umo, monkeypatch):
    r = ready(plugin, monkeypatch, "该睡了。")
    monkeypatch.setattr(r, "should_remind_sleep", lambda u: True)
    monkeypatch.setattr(r, "mark_sleep_reminded", lambda u: None)
    plugin.store.set(umo, "unanswered", 3)
    result = await plugin.panel_service.fire({"umo": umo})
    assert result["sent"] is True and result["delivery_status"] == "sent"
    assert result["unanswered"] == 3


async def test_send_exception_and_empty_generation(plugin, umo, monkeypatch):
    r = ready(plugin, monkeypatch)
    plugin.context.send_message.side_effect = RuntimeError("failed")
    result = await plugin.panel_service.fire({"umo": umo})
    assert result["sent"] is False
    r._generate.return_value = ""
    result = await plugin.panel_service.fire({"umo": umo})
    assert result["reason"] == "empty_generation"


async def test_cancellation_records_only_delivered(plugin, umo, monkeypatch):
    r = ready(plugin, monkeypatch)
    entered = asyncio.Event()

    async def send(*args):
        entered.set()
        return True

    plugin.context.send_message.side_effect = send
    plugin.friend_cfg["segmented_reply_settings"]["fixed_delay"] = 60
    task = asyncio.create_task(r._fire(umo))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*list(r._bg_tasks))
    r._append_assistant_history.assert_awaited_once_with(umo, "你好。")
    assert not r.session_lock(umo).locked()


async def test_concurrent_fire_does_not_double_send(plugin, umo, monkeypatch):
    r = ready(plugin, monkeypatch, "你好。")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(*a, **kw):
        entered.set()
        await release.wait()
        return "你好。"

    r._generate = generate
    first = asyncio.create_task(r._fire(umo))
    await entered.wait()
    duplicate = await r._fire(umo)
    assert duplicate["reason"] == "busy" and not duplicate["sent"]
    with pytest.raises(PanelError) as e:
        await plugin.panel_service.fire({"umo": umo})
    assert e.value.code == "E_BUSY"
    release.set()
    await first
    assert plugin.context.send_message.await_count == 1


async def test_stop_and_terminate_cancel_background(plugin, umo):
    ptask = asyncio.create_task(asyncio.Event().wait())
    rtask = asyncio.create_task(asyncio.Event().wait())
    plugin._bg_tasks.add(ptask)
    plugin.runner._bg_tasks.add(rtask)
    r = plugin.runner
    r.started = True
    r.reschedule(umo, delay_minutes=5)
    await plugin.terminate()
    assert ptask.cancelled() and rtask.cancelled()
    assert not r.timers and not r.started


async def test_panel_auth_and_headers(plugin, umo):
    plugin.config["web_panel"]["password"] = "测试密码"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plugin.panel_http._app),
        base_url="http://panel",
    ) as c:
        assert (await c.get("/api/tasks")).status_code == 401
        assert (
            await c.post("/api/login", json={"password": "wrong"})
        ).status_code == 401
        assert (
            await c.post("/api/login", json={"password": "测试密码"})
        ).status_code == 200
        response = await c.get("/api/tasks")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "schedule_diagnostics" in response.json()["data"]["sessions"][0]
        await c.post("/api/logout")
        assert (await c.get("/api/tasks")).status_code == 401


async def test_probability_preview_does_not_save(plugin, umo):
    before = copy.deepcopy(dict(plugin.config))
    state = copy.deepcopy(plugin.store.data)
    result = plugin.panel_service.probability_preview(
        {"umo": umo, "patch": {"custom_engine": {"early_weight": 2}}}
    )
    assert result
    assert plugin.config == before and plugin.store.data == state
    assert plugin.config.saves == 0


async def test_save_configuration_and_redact_password(plugin):
    saved = await plugin.panel_service.config_save(
        {
            "patch": {
                "friend_settings": {"schedule_settings": {"min_interval_minutes": 10}}
            }
        }
    )
    assert saved["saved"] and plugin.config.saves == 1
    assert plugin.friend_cfg["schedule_settings"]["min_interval_minutes"] == 10
    plugin.config["web_panel"]["password"] = "not-exported"
    exported = plugin.panel_service.config_get()["web_panel"]
    assert "password" not in exported and exported["has_password"]


async def test_manual_text_send_partial(plugin, umo, monkeypatch):
    ready(plugin, monkeypatch)
    plugin.context.send_message.side_effect = [True, False]
    result = await plugin.panel_service.send({"umo": umo, "text": "你好。再见。"})
    assert result["sent"] and result["delivery_status"] == "partial"
    assert result["delivered_segments"] == 1
