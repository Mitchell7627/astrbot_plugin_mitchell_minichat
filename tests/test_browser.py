"""Real Chromium check; run with MINICHAT_CHROMIUM=/path/to/chromium if needed."""

import os
import shutil
import socket
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from playwright.async_api import async_playwright, expect


async def test_task_page_diagnostics_and_delivery_states(plugin, umo, monkeypatch, clock):
    executable = os.environ.get("MINICHAT_CHROMIUM") or shutil.which("chromium")
    r = plugin.runner
    r.update_from_message(umo, "我正在洗澡")
    r.update_from_message(umo, "我要吹头")
    r.started = True
    r._trace_schedule(
        umo, "sample", [{"reason": "sample", "minutes": 20}], next_at=1234567890
    )
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value="你好。再见。"))
    monkeypatch.setattr(r, "_append_assistant_history", AsyncMock())
    monkeypatch.setattr(r, "_finish_cycle", lambda u: None)
    monkeypatch.setattr(r, "should_remind_sleep", lambda u: False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    await plugin.panel_http.start(port=port)
    try:
        async with async_playwright() as pw:
            executable = executable or pw.chromium.executable_path
            if not Path(executable).exists():
                pytest.skip(
                    "Chromium missing; run python -m playwright install chromium"
                )
            browser = await pw.chromium.launch(executable_path=executable)
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                async def local_only(route):
                    if urlparse(route.request.url).hostname == "127.0.0.1":
                        await route.continue_()
                    else:
                        await route.abort()

                await page.route("**/*", local_only)
                await page.goto(f"http://127.0.0.1:{port}/")
                await page.locator('[data-tab="tasks"]').click()
                await page.locator('#task-list details').filter(
                    has=page.locator("summary", has_text="最近判定")
                ).locator("summary").click()
                stop_row = page.locator("#task-list .log-row").filter(has_text="结束·吹头")
                await expect(stop_row.locator(".badge")).to_have_text("已记录")
                await expect(stop_row).to_contain_text("按结束信号执行")
                await stop_row.locator('[data-op="undo"]').click()
                await expect(stop_row).to_contain_text("已撤销")
                assert plugin.store.get(umo, "active_event")["name"] == "shower"
                # 取消记录也必须显示已应用，面板撤销恢复原约定。
                r.update_from_message(umo, "我下午三点开会")
                original = r._appointments(umo)
                r.update_from_message(umo, "下午三点的那个安排取消掉")
                assert not r._appointments(umo)
                await page.reload()
                await page.locator('[data-tab="tasks"]').click()
                await page.locator('#task-list details').filter(
                    has=page.locator("summary", has_text="最近判定")
                ).locator("summary").click()
                cancel_row = page.locator("#task-list .log-row").filter(has_text="取消 1 条匹配约定")
                await expect(cancel_row.locator(".badge")).to_have_text("已记录")
                await cancel_row.locator('[data-op="undo"]').click()
                await expect(cancel_row).to_contain_text("已撤销")
                assert r._appointments(umo) == original
                await expect(page.locator("#task-list")).to_contain_text("排程原因")
                await (
                    page.locator("#task-list details")
                    .filter(has=page.locator("summary", has_text="排程原因"))
                    .locator("summary")
                    .click()
                )
                await expect(page.locator("#task-list")).to_contain_text(
                    "按频率采样：20.0 分钟"
                )
                send = page.locator('#task-list [data-op="send"]')
                plugin.context.send_message.side_effect = [True, False]
                await send.click()
                await expect(
                    page.locator(".toast").filter(has_text="部分发送：1/2 段")
                ).to_be_visible()
                plugin.context.send_message.side_effect = [False]
                await send.click()
                await expect(
                    page.locator(".toast").filter(has_text="没有发出")
                ).to_be_visible()
                plugin.context.send_message.side_effect = None
                monkeypatch.setattr(r, "should_remind_sleep", lambda u: True)
                monkeypatch.setattr(r, "mark_sleep_reminded", lambda u: None)
                await send.click()
                await expect(
                    page.locator(".toast").filter(has_text="已发送，未回复计数")
                ).to_be_visible()
                assert not errors
                await page.screenshot(
                    path=str(plugin.data_dir / "minichat-task-page.png"), full_page=True
                )
            finally:
                await browser.close()
    finally:
        await plugin.panel_http.stop()
