import copy
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_mitchell_minichat.main import ProactiveMiniPlugin
from astrbot_plugin_mitchell_minichat.core.runner import Runner
from astrbot.api.star import StarTools


def schema_defaults(items):
    return {
        k: schema_defaults(v["items"])
        if v.get("type") == "object"
        else copy.deepcopy(v.get("default"))
        for k, v in items.items()
    }


class Config(dict):
    def save_config(self):
        self.saves += 1

    async def save_config_async(self):
        self.save_config()


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(StarTools, "get_data_dir", lambda *a: tmp_path)
    cfg = Config(schema_defaults(json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))))
    cfg.saves = 0
    cfg["friend_settings"]["session_list"] = ["test:FriendMessage:1"]
    cfg["friend_settings"]["segmented_reply_settings"].update(
        enable_smart_split=False,
        min_segment_length=1,
        delay_strategy="fixed",
        fixed_delay=0.001,
    )
    context = SimpleNamespace(
        send_message=AsyncMock(return_value=True), get_using_provider=lambda **kw: None
    )
    p = ProactiveMiniPlugin(context, cfg)
    p.runner = Runner(p)
    return p


@pytest.fixture
def umo(plugin):
    return plugin.runner.sessions()[0]


@pytest.fixture(autouse=True)
async def cleanup_plugin(plugin):
    yield
    await plugin.terminate()


@pytest.fixture
def clock(monkeypatch):
    import time
    from astrbot_plugin_mitchell_minichat.core import (
        runner,
        appointment,
        sleep_reminder,
    )

    state = SimpleNamespace(now=datetime(2026, 11, 2, 10).timestamp())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(state.now, tz)

    monkeypatch.setattr(time, "time", lambda: state.now)
    for module in (runner, appointment, sleep_reminder):
        monkeypatch.setattr(module, "datetime", Clock)
    rng = random.Random(42)
    monkeypatch.setattr(
        runner,
        "sample_interval_seconds",
        lambda *a, **kw: __import__(
            "astrbot_plugin_mitchell_minichat.core.interval_sampler", fromlist=[""]
        ).sample_interval_seconds(*a, **kw, rng=rng),
    )
    return state
