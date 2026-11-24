from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_mitchell_minichat.core.splitter import explicit_short_lines, split_text


@pytest.fixture
def split_settings(plugin):
    settings = plugin.runner.cfg["segmented_reply_settings"]
    settings.update(enable=True, enable_smart_split=True, min_segment_length=8,
                    max_segments=7, balanced_split=False, no_split_around=['"'])
    return settings


@pytest.mark.parametrize("body,expected", [
    ("还在呀\n刚刚想起来你说过的那件事", ["还在呀", "刚刚想起来你说过的那件事"]),
    ("还在呀。刚刚想起来你说过的那件事", ["还在呀。刚刚想起来你说过的那件事"]),
    ("刚刚想起来你说过的那件事，想再聊聊", ["刚刚想起来你说过的那件事，想再聊聊"]),
    ("不过\n刚刚想起来你说过的那件事", ["不过，刚刚想起来你说过的那件事"]),
    ("我想跟你说\n晚一点有空再聊", ["我想跟你说，晚一点有空再聊"]),
    ("哥哥\n刚刚想起来你说过的那件事", ["哥哥，刚刚想起来你说过的那件事"]),
    ("还在呀", ["还在呀"]),
    ("还在呀\n刚刚想起来你说过的那件事\n戳戳你", ["还在呀", "刚刚想起来你说过的那件事", "戳戳你"]),
])
def test_authored_short_lines_only(plugin, split_settings, body, expected):
    assert plugin.runner.prepare_segments(body, proactive=True, authored_text=body) == expected


def test_split_setting_and_quotes_still_apply(plugin, split_settings):
    runner = plugin.runner
    quoted = "「还在呀\n刚刚想起你说的那件事」\n戳戳你"
    assert runner.prepare_segments(quoted, proactive=True, authored_text=quoted) == [
        "「还在呀 刚刚想起你说的那件事」", "戳戳你",
    ]
    code = "```\n还在呀\n刚刚想起你说的那件事\n```\n戳戳你"
    assert len(split_text(code, min_length=8, balanced=False, preserve_lines=explicit_short_lines(code))) == 2
    body = "还在呀\n刚刚想起你说的那件事\n戳戳你"
    assert len(runner.prepare_segments(body)) == 1
    split_settings["max_segments"] = 2
    assert len(runner.prepare_segments(body, proactive=True, authored_text=body)) == 2
    split_settings["enable"] = False
    breakdown = {}
    assert len(runner.prepare_segments(body, proactive=True, authored_text=body, breakdown=breakdown)) == 1
    assert breakdown["candidates"] == 3


async def test_preview_and_delivery_preserve_intended_lines(plugin, umo, clock, split_settings, monkeypatch):
    clock.now = datetime(2026, 11, 24, 13, 42).timestamp()
    runner = plugin.runner
    runner.started = True
    generate = AsyncMock(return_value="[[SEND:GREETING]]\n还在呀\n想来陪你说会儿话")
    monkeypatch.setattr(runner, "_generate", generate)
    preview = await plugin.panel_service.preview({"umo": umo})
    assert preview["segments"] == ["还在呀", "想来陪你说会儿话"]
    result = await runner._fire(umo)
    assert [call.args[1].get_plain_text() for call in plugin.context.send_message.await_args_list] == preview["segments"]
    assert result["delivered_segments"] == 2
    row = runner.proactive_review.recent()[0]
    assert (row["generated_segments"], row["filtered_segments"], row["split_segments"]) == (2, 2, 2)
    assert generate.await_count == 2


async def test_review_distinguishes_filtered_and_merged(plugin, umo, clock, split_settings, monkeypatch):
    clock.now = datetime(2026, 11, 24, 13, 42).timestamp()
    runner = plugin.runner
    generate = AsyncMock(return_value="[[SEND:CONFIRMED_TOPIC]]\n你好。想找你随便聊两句。\n我这边的布丁都摆凉了")
    monkeypatch.setattr(runner, "_generate", generate)
    result = await runner._fire(umo)
    assert result["delivered_segments"] == 1
    row = runner.proactive_review.recent()[0]
    assert (row["generated_segments"], row["filtered_segments"], row["split_segments"]) == (3, 2, 1)
    assert "unconfirmed_scene_detail" in row["checks"]
    generate.assert_awaited_once()
