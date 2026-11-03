"""Regressions from the overnight audit: routing and truthful decision history."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import Plain
from astrbot_plugin_mitchell_minichat.core.appointment import find_clock


def event(text, umo):
    return SimpleNamespace(
        unified_msg_origin=umo, get_messages=lambda: [Plain(text)], message_str=text
    )


async def drain(plugin):
    if plugin._bg_tasks:
        await asyncio.gather(*list(plugin._bg_tasks))


@pytest.mark.parametrize("text", [
    "快一点", "轻一点嘛", "稍微慢一点", "早一点", "晚一点", "好一点了",
    "多一点", "少一点", "一点都不想开会", "一点也不好", "一点点就够了",
    "下午一点都不想开会", "今晚快一点", "晚上轻一点", "再说得直白一点",
])
def test_degree_expression_is_not_a_clock(text):
    assert find_clock(text) is None


@pytest.mark.parametrize("text,hour,minute", [
    ("一点开会", 1, 0), ("一点半下课", 1, 30),
    ("下午一点开会", 1, 0), ("快一点半了", 1, 30),
    ("今早一点开会", 1, 0), ("今晚一点开会", 1, 0),
    ("快一点，我下午三点开会", 3, 0),
    ("轻一点，十一点半下课", 11, 30),
    ("一点都不想说，下午两点出发", 2, 0),
    ("快一点，14:30开会", 14, 30),
])
def test_real_clock_after_degree_expression_survives(text, hour, minute):
    clock = find_clock(text)
    assert (clock["h"], clock["m"]) == (hour, minute)
    assert "点" in text[clock["idx"]:clock["idx"] + clock["length"]] or ":" in text


@pytest.mark.parametrize("text", [
    "我去冲个澡", "我去冲个澡，稍微快一点", "快一点，我去冲个澡",
])
async def test_degree_does_not_prevent_semantic_fallback(plugin, umo, clock, monkeypatch, text):
    classify = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classify)
    await plugin.on_private_message(event(text, umo))
    await drain(plugin)
    classify.assert_awaited_once()
    assert plugin.store.get(umo, "active_event")["name"] == "shower"
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize("blocked", [
    "你下午三点开会吗", "我妈下午三点开会", "他说‘下午三点开会’",
    "如果下雨我下午三点开会", "我明天下午三点开会", "我下午三点不开会",
    "现在十点了",
])
@pytest.mark.parametrize("first", [True, False])
async def test_rejected_appointment_only_blocks_its_own_clause(
    plugin, umo, clock, monkeypatch, blocked, first
):
    fallback = AsyncMock()
    monkeypatch.setattr(plugin.runner, "classify_with_llm", fallback)
    current = "不过我现在去冲个澡" if blocked.startswith("如果") else "我去冲个澡"
    clauses = [blocked, current]
    if not first:
        clauses.reverse()
    await plugin.on_private_message(event("，".join(clauses), umo))
    await drain(plugin)
    fallback.assert_awaited_once()
    assert fallback.await_args.args == (umo, current, plugin.store.get(umo, "turn_seq"))
    assert not plugin.runner._appointments(umo)


@pytest.mark.parametrize("text", [
    "我不去冲个澡", "我没有在冲个澡", "你去冲个澡吧", "我妈现在去冲个澡",
    "他说“我去冲个澡”", "他说“准备冲个澡，真的要洗”",
    "如果下雨，我就去冲个澡", "明天，我去冲个澡", "昨天我去冲个澡了",
    "如果下雨我下午三点开会，我去冲个澡",
    "我去冲个澡吗", "我去冲个澡？", "我梦见自己去冲个澡", "取消去冲个澡",
    "我不去洗澡", "他说“我去洗澡了”", "我下午三点开会",
])
async def test_protected_intent_is_not_resubmitted_to_model(plugin, umo, clock, monkeypatch, text):
    fallback = AsyncMock()
    monkeypatch.setattr(plugin.runner, "classify_with_llm", fallback)
    await plugin.on_private_message(event(text, umo))
    await drain(plugin)
    fallback.assert_not_awaited()
    assert plugin.store.get(umo, "active_event") is None


async def test_accepted_appointment_and_unknown_current_action_are_independent(plugin, umo, clock, monkeypatch):
    classify = AsyncMock(return_value={"name": "shower", "done": False})
    monkeypatch.setattr(plugin.runner, "classify_text", classify)
    await plugin.on_private_message(event("我下午三点开会，我去冲个澡", umo))
    await drain(plugin)
    assert [v["label"] for v in plugin.runner._appointments(umo)] == ["开会"]
    classify.assert_awaited_once_with(umo, "我去冲个澡")
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


def test_degree_before_real_appointment_is_not_a_gray_time(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "快一点，我下午三点开会")
    assert [v["time_text"] for v in r._appointments(umo)] == ["15:00"]
    assert r.take_appointment_review() is None
    assert r.take_llm_candidate(umo) is None


def test_fallback_candidate_is_one_shot_and_tied_to_session_and_turn(plugin, umo, clock):
    r = plugin.runner
    assert r.update_from_message(umo, "准备洗香香了") is False
    assert r.take_llm_candidate("other:FriendMessage:2") is None
    candidate = r.take_llm_candidate(umo)
    assert candidate == {"umo": umo, "text": "准备洗香香了", "turn_seq": plugin.store.get(umo, "turn_seq")}
    assert r.take_llm_candidate(umo) is None
    r.update_from_message(umo, "准备洗香香了")
    r.update_from_message(umo, "我正在吃饭")
    assert r.take_llm_candidate(umo) is None
    r.update_from_message(umo, "准备洗香香了")
    r.invalidate_session(umo)
    assert r.take_llm_candidate(umo) is None


def test_empty_message_cannot_reuse_old_candidate(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "准备洗香香了")
    r.update_from_message(umo, "")
    assert r.take_llm_candidate(umo) is None


@pytest.mark.parametrize("changed_after", [False, True])
def test_gray_stop_records_execution_and_validates_undo(plugin, umo, clock, changed_after):
    r = plugin.runner
    r.update_from_message(umo, "我们一起洗澡吧")
    before = r._snapshot(umo)
    r.update_from_message(umo, "我要吹头")
    record = next(v for v in r.decisions.items(umo) if v["kind"] == "stop")
    assert plugin.store.get(umo, "active_event") is None
    assert record["verdict"] == "gray"
    assert record["action"] == "applied"
    assert "按结束信号执行" in record["reason"]
    assert record["undo_state"] == "available" and record["touched"]
    if changed_after:
        r.update_from_message(umo, "我正在吃饭")
        with pytest.raises(ValueError):
            r.undo_decision(record["id"])
        assert plugin.store.get(umo, "active_event")["name"] == "eating"
    else:
        r.undo_decision(record["id"])
        assert r._snapshot(umo) == before


def test_rejected_record_does_not_own_other_clause_changes(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我正在洗澡")
    r.update_from_message(umo, "你下午三点开会吗，我要吹头")
    records = r.decisions.items(umo)
    rejected = next(v for v in records if v["kind"] == "appointment")
    applied = next(v for v in records if v["kind"] == "stop")
    assert rejected["action"] == "dropped" and rejected["touched"] == []
    assert rejected["undo_state"] == "none"
    assert applied["action"] == "applied" and applied["undo_state"] == "available"


def test_new_stop_records_survive_store_reload_without_changing_old_history(plugin, umo, clock):
    r = plugin.runner
    old = {"id": clock.now - 1, "ts": clock.now - 1, "umo": umo, "kind": "stop",
           "verdict": "gray", "action": "dropped", "touched": [], "undo_state": "none"}
    r.decisions.add(copy.deepcopy(old))
    r.update_from_message(umo, "我正在洗澡")
    r.update_from_message(umo, "我要吹头")
    r.decisions.save()
    from astrbot_plugin_mitchell_minichat.core.decisions import DecisionLog
    restored = DecisionLog(r.decisions.path)
    assert restored.get(old["id"]) == old
    record = next(v for v in restored.items(umo) if v.get("action") == "applied" and v["kind"] == "stop")
    assert record["undo_state"] == "available"


@pytest.mark.parametrize("text", ["先不睡嘛", "不想睡喵"])
def test_sleep_refusal_keeps_existing_night_policy(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, text)
    assert r._sleep_state(umo) is None
    r.update_from_message(umo, "等会再睡")
    assert r._sleep_state(umo)["state"] == "waiting"


async def test_meal_praise_prefilter_is_unchanged(plugin, umo, clock):
    assert await plugin.runner.classify_text(umo, "妈妈做的饭好好吃") == {"skipped": "prefilter"}


async def test_late_fallback_from_residual_cannot_override_new_message(plugin, umo, clock, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        entered.set()
        await release.wait()
        return {"name": "shower", "done": False}

    monkeypatch.setattr(plugin.runner, "classify_text", classify)
    await plugin.on_private_message(event("你下午三点开会吗，我去冲个澡", umo))
    await asyncio.wait_for(entered.wait(), 1)
    await plugin.on_private_message(event("我正在吃饭", umo))
    release.set()
    await drain(plugin)
    assert plugin.store.get(umo, "active_event")["name"] == "eating"
