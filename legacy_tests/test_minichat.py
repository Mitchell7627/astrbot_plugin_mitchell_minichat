"""Offline checks of d0f58f5. AstrBot and model/network calls are simulated.

Run: python -m pytest minichat-review/test_minichat.py -q
The regression_* tests assert the intended behavior and expose current bugs.
"""
import asyncio
import copy
import json
import logging
import os
import random
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(os.environ.get("MINICHAT_REPO", str(Path(__file__).resolve().parents[1] / "astrbot_plugin_mitchell_minichat")))
sys.path.insert(0, str(REPO))
for name in ("astrbot", "astrbot.api", "astrbot.core", "astrbot.core.message",
             "astrbot.core.message.components", "astrbot.core.message.message_event_result"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["astrbot.api"].logger = logging.getLogger("minichat-review")
class Plain:
    type = 'Plain'
    def __init__(self, text):
        self.text = text
sys.modules["astrbot.core.message.components"].Plain = Plain
sys.modules["astrbot.core.message.message_event_result"].MessageChain = list

from core.appointment import parse as parse_appointment
from core.event_detector import EventRuleStore
from core.interval_sampler import sample_interval_seconds
from core.runner import Runner
from core.sleep_reminder import in_remind_window, match_sleep, DEFAULT_SLEEP_KEYWORDS
from core.splitter import split_text
from core.store import Store
from core.web_panel import PanelHttpServer

UMO = "test:FriendMessage:12345"


def defaults(schema):
    return {key: defaults(value["items"]) if value.get("type") == "object"
            else copy.deepcopy(value.get("default")) for key, value in schema.items()}


@pytest.fixture
def runner(tmp_path):
    config = defaults(json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8-sig")))
    config["friend_settings"]["session_list"] = [UMO]
    plugin = types.SimpleNamespace(
        config=config, friend_cfg=config["friend_settings"], engine_cfg=config["custom_engine"],
        context=types.SimpleNamespace(send_message=AsyncMock(return_value=True)),
        data_dir=tmp_path, plugin_dir=REPO, store=Store(tmp_path),
        engine=EventRuleStore(tmp_path, config["custom_engine"]),
    )
    result = Runner(plugin)
    plugin.runner = result
    return result


@pytest.mark.parametrize("text,name", [
    ("我去洗澡了", "shower"), ("我在吃饭", "eating"), ("准备做饭了", "cooking"),
    ("我在开会", "meeting"), ("我在打游戏", "gaming"), ("我在看电影", "movie"),
    ("我在逛街", "shopping"), ("我在运动", "exercise"),
])
def test_event_positive(runner, text, name):
    hit = runner.engine.match(text)
    assert hit and hit["name"] == name


@pytest.mark.parametrize("text", [
    "不去健身", "没在开会", "明天去洗澡", "我妈去洗澡了", "谁在洗澡啊",
    "洗澡的水太烫了", "开会的内容我忘了", "会去洗澡的", "开会开到几点",
])
def test_event_negative(runner, text):
    assert runner.engine.match(text) is None


@pytest.mark.parametrize("text,expected", [
    ("我睡了", True), ("晚安", True), ("睡觉了", True), ("我午睡了", False),
    ("睡了吗", False), ("你睡了没", False), ("孩子睡了", False), ("我妈睡了", False),
])
def test_sleep_phrases(text, expected):
    assert bool(match_sleep(text, DEFAULT_SLEEP_KEYWORDS)) is expected


@pytest.mark.parametrize("hour,minute,expected", [
    (22, 59, False), (23, 0, True), (23, 59, True), (0, 0, True), (0, 19, True),
    (0, 21, False), (12, 0, False),
])
def test_cross_midnight_sleep_window(hour, minute, expected):
    assert in_remind_window("23:00", "00:20", datetime(2026, 11, 1, hour, minute)) is expected


@pytest.mark.parametrize("text,hour,minute", [
    ("11:50 下课", 11, 50), ("我上到十二点", 12, 0),
    ("下午三点开会", 15, 0), ("差一刻五点下班", 16, 45),
])
def test_appointment_parsing(text, hour, minute):
    result = parse_appointment(text, datetime(2026, 11, 1, 10), {"max_hours": 14})
    assert result["hit"], result
    moment = datetime.fromtimestamp(result["hit"]["at"])
    assert (moment.hour, moment.minute) == (hour, minute)


@pytest.mark.parametrize("lo,hi", [(60, 60), (300, 14400), (60, 3600), (3000, 600)])
def test_weighted_sampling_bounds(lo, hi):
    source = random.Random(12345)
    for _ in range(250):
        seconds, _mode = sample_interval_seconds(lo, hi, rng=source)
        assert lo <= seconds <= max(lo, hi)


@pytest.mark.parametrize("text", ["", "一段很短的文字。", "第一句话。第二句话！第三句话？",
    "这是代码：```python\nx = 1\nprint(x)\n```。接着说。", "不要把（括号里的。句子！）切开。下一句。"])
def test_split_text_basic(text):
    parts = split_text(text, min_length=0, max_segments=3)
    assert len(parts) <= 3
    assert all(part.strip() for part in parts)
    assert "".join("".join(parts).split()) == "".join(text.split())


def test_context_cap_does_not_mutate_history(runner):
    runner.cfg["context_settings"]["history_hard_limit"] = 4
    history = [{"role": "user" if n % 2 == 0 else "assistant", "content": str(n)} for n in range(10)]
    original = copy.deepcopy(history)
    out = runner._prepare_contexts(history, runner._context_settings())
    assert [item["content"] for item in out] == ["6", "7", "8", "9"]
    assert history == original


def test_store_round_trip(runner):
    runner.store.set(UMO, "unanswered", 2)
    runner.store.save()
    assert Store(runner.plugin.data_dir).get(UMO, "unanswered") == 2


def test_event_window_not_overwritten_by_sleep_alignment(runner):
    async def scenario():
        runner.started = True
        runner._set_event(UMO, runner.engine.get_rule("shower"), "洗澡")
        with patch.object(runner, "_sample_minutes", return_value=5), \
             patch.object(runner, "_sleep_reminder_delay_minutes", return_value=120) as align:
            runner.reschedule(UMO)
            align.assert_not_called()
            assert runner.store.get(UMO, "next_at") < runner.store.get(UMO, "active_event")["expires_at"]
        await runner.stop()
        await asyncio.sleep(0)
    asyncio.run(scenario())


@pytest.mark.parametrize("password", ["ascii-password", "中文密码123"])
def test_regression_panel_password_supports_unicode(runner, password):
    from fastapi.testclient import TestClient
    runner.plugin.config["web_panel"]["password"] = password
    server = PanelHttpServer(runner.plugin, types.SimpleNamespace())
    with TestClient(server._app, raise_server_exceptions=False) as client:
        response = client.post("/api/login", json={"password": password})
    assert response.status_code == 200, (response.status_code, response.text)


def test_regression_pause_preserves_appointment_timer(runner):
    async def scenario():
        now = datetime(2026, 11, 1, 18).timestamp()
        runner.started = True
        runner.store.set(UMO, "unanswered", 3)
        runner.store.set(UMO, "appointments", [{"at": now + 120, "stage": "pending", "label": "下课"}])
        with patch("core.runner.time.time", return_value=now):
            assert runner.appointment_shot(UMO) == "lead"
            timer = asyncio.create_task(runner._wait_and_fire(UMO, 0))
            runner.timers[UMO] = timer
            await timer
            pending = [task for task in runner.timers.values() if not task.done()]
            await runner.stop()
            assert pending, "Unanswered limit drops the only timer, so the due reminder cannot run"
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_regression_late_classifier_cannot_overwrite_new_event(runner):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_classifier(*_args):
            entered.set()
            await release.wait()
            return {"name": "shower", "done": False}
        runner.classify_text = slow_classifier
        task = asyncio.create_task(runner.classify_with_llm(UMO, "准备洗香香了"))
        await entered.wait()
        runner.update_from_message(UMO, "我在吃饭")
        assert runner.store.get(UMO, "active_event")["name"] == "eating"
        release.set()
        await task
        assert runner.store.get(UMO, "active_event")["name"] == "eating"
    asyncio.run(scenario())


def test_regression_partial_send_history_records_only_delivered_text(runner):
    async def scenario():
        first, second = "这是已经送达的第一段。", "这是没有送达的第二段。"
        text = first + second
        runner._generate = AsyncMock(return_value=text)
        runner._append_assistant_history = AsyncMock()
        runner.plugin.context.send_message.side_effect = [True, False]
        with patch("core.runner.split_text", return_value=[first, second]), \
             patch("core.runner.calc_delay", return_value=0), \
             patch.object(runner, "should_remind_sleep", return_value=False):
            await runner._fire(UMO)
        assert runner.plugin.context.send_message.await_count == 2
        runner._append_assistant_history.assert_awaited_once_with(UMO, first)
    asyncio.run(scenario())
