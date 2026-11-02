#!/usr/bin/env python3
"""Keyword/state/scheduler replay. No production context, credentials, network or real sends."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def defaults(items):
    return {
        k: defaults(v["items"])
        if v.get("type") == "object"
        else copy.deepcopy(v.get("default"))
        for k, v in items.items()
    }


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError("at 必须是 Unix 秒或带时区的 ISO 时间")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("ISO 时间必须包含时区，例如 +08:00")
        result = moment.timestamp()
    if not math.isfinite(result):
        raise ValueError("时间必须是有限值")
    return result


async def replay(document, seed=42, fallback=False):
    # Imports happen after CLI enters its temporary directory: AstrBot's local
    # initialization must never write into an existing installation.
    sys.path.insert(0, str(ROOT.parent))
    from astrbot_plugin_mitchell_minichat.core import (
        runner as mod,
        appointment,
        sleep_reminder,
    )
    from astrbot_plugin_mitchell_minichat.core.event_detector import EventRuleStore
    from astrbot_plugin_mitchell_minichat.core.store import Store

    events = document.get("events", [])
    if not events:
        raise ValueError("events 不能为空")
    times = [timestamp(e["at"]) for e in events]
    if times != sorted(times):
        raise ValueError("events 必须按时间排序")
    until = timestamp(document.get("until", events[-1]["at"]))
    if until < times[-1]:
        raise ValueError("until 不能早于最后一条消息")
    if until - times[0] > 7 * 86400:
        raise ValueError("单次回放最多 7 天")
    clock = SimpleNamespace(now=times[0])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now, tz)

    class VirtualRunner(mod.Runner):
        async def _wait_and_fire(self, *args):
            # Timers are represented by next_at; only advance() may fire them.
            await asyncio.Future()

    cfg = defaults(json.loads((ROOT / "_conf_schema.json").read_text()))
    cfg["friend_settings"]["session_list"] = ["replay:FriendMessage:1"]
    cfg["friend_settings"]["segmented_reply_settings"]["enable"] = False
    # Offline replay intentionally does not evaluate model classification.
    cfg["custom_engine"]["enable_llm_event"] = False
    cfg["custom_engine"]["enable_gray_review"] = False
    if fallback:
        cfg["custom_engine"]["event_rules"] = []
    data_dir = Path.cwd() / "state"
    captures = []

    async def generate(**kw):
        captures.append(copy.deepcopy(kw))
        return SimpleNamespace(completion_text="（离线回放消息）")

    async def memory_send(*args):
        return True

    provider = SimpleNamespace(text_chat=generate)
    context = SimpleNamespace(
        get_using_provider=lambda **kw: provider, send_message=memory_send
    )
    plugin = SimpleNamespace(
        context=context,
        config=cfg,
        engine_cfg=cfg["custom_engine"],
        friend_cfg=cfg["friend_settings"],
        data_dir=data_dir,
        store=Store(data_dir),
        web_override=lambda k: None,
    )
    plugin.engine = EventRuleStore(data_dir, plugin.engine_cfg)
    r = VirtualRunner(plugin)
    output = []
    tick_count = 0
    next_sweep = times[0] + 30
    rng = random.Random(seed)
    ids = random.Random(seed)

    def snapshot(kind, **extra):
        output.append(
            {
                "at": clock.now,
                "kind": kind,
                **extra,
                "state": copy.deepcopy(plugin.store.data.get(umo, {})),
                "schedule": r.schedule_diagnostics(umo),
                "event_hint": r.event_hint(umo),
            }
        )

    async def advance(target):
        nonlocal tick_count, next_sweep
        while True:
            due = (
                float(plugin.store.get(umo, "next_at", 0))
                if umo in r.timers
                else math.inf
            )
            tick = min(due, next_sweep)
            if tick > target:
                break
            clock.now = max(clock.now, tick)
            if next_sweep <= due:
                before = copy.deepcopy(plugin.store.data)
                r._sweep_expired_events()
                if plugin.store.data != before:
                    snapshot("expiry")
                next_sweep += 30
                continue
            tick_count += 1
            if tick_count > 2000:
                raise ValueError("回放超过 2000 次触发，请缩短时间范围")
            r.timers.pop(umo).cancel()
            before = len(captures)
            result = await r._fire(umo)
            snapshot("timer", delivery=result, prompts=copy.deepcopy(captures[before:]))
        clock.now = target

    umo = cfg["friend_settings"]["session_list"][0]
    from astrbot_plugin_mitchell_minichat.core.interval_sampler import (
        sample_interval_seconds,
        sample_uniform_seconds,
    )

    with ExitStack() as stack:
        stack.enter_context(patch("time.time", lambda: clock.now))
        stack.enter_context(
            patch.object(mod.uuid, "uuid4", lambda: uuid.UUID(int=ids.getrandbits(128)))
        )
        stack.enter_context(patch.object(mod, "random", rng))
        for module in (mod, appointment, sleep_reminder):
            stack.enter_context(patch.object(module, "datetime", Clock))
        stack.enter_context(
            patch.object(
                mod,
                "sample_interval_seconds",
                lambda *a, **kw: sample_interval_seconds(*a, **kw, rng=rng),
            )
        )
        stack.enter_context(
            patch.object(
                mod,
                "sample_uniform_seconds",
                lambda *a, **kw: sample_uniform_seconds(*a, **kw, rng=rng),
            )
        )
        # Reminder offsets also use randomness; keep the process-wide RNG untouched.
        stack.enter_context(patch.object(sleep_reminder, "random", rng))
        r.started = True
        try:
            for event, at in zip(events, times):
                await advance(at)
                plugin.store.touch_message(umo)
                plugin.store.reset_unanswered(umo)
                r.update_from_message(umo, str(event["text"]))
                r.reschedule_after_message(umo)
                snapshot("message", text=event["text"])
            await advance(until)
            snapshot("end")
        finally:
            await r.stop()
    return {
        "mode": "offline-keywords-only",
        "seed": seed,
        "rules": len(plugin.engine.rules_snapshot()),
        "timeline": output,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fallback", action="store_true", help="使用 8 类文件回退规则")
    parser.add_argument("--timezone", default="Asia/Hong_Kong")
    args = parser.parse_args()
    try:
        ZoneInfo(args.timezone)
        document = json.loads(args.scenario.read_text(encoding="utf-8"))
        os.environ["TZ"] = args.timezone
        time.tzset()
        with tempfile.TemporaryDirectory(prefix="minichat-replay-") as directory:
            old = Path.cwd()
            try:
                os.chdir(directory)
                with redirect_stdout(sys.stderr):
                    result = asyncio.run(replay(document, args.seed, args.fallback))
            finally:
                os.chdir(old)
        result["timezone"] = args.timezone
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"回放失败：{exc}\n")


if __name__ == "__main__":
    main()
