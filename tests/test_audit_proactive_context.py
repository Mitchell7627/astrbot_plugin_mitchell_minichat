from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_mitchell_minichat.core.proactive_quality import duplicate, fingerprint, polish_reply, topics


UNKNOWN = ["unknown", "", "", ""]
INCIDENT = "还在里边吗，水快凉了，别泡这么久呀"


@pytest.mark.parametrize("body,removed", [
    (INCIDENT, True),
    ("还在里面吗？水都凉了，别泡太久喔", True),
    ("茶水都凉了，这茶别泡太久", False),
    ("如果你在浴缸里，就别泡太久喵", False),
    ("下次洗澡水太热的话，别泡太久喵", False),
    ("她念了一句「还在里边吗，水快凉了，别泡这么久呀」", False),
    ("刚刚那句好可爱，又想听你说一遍了喵", False),
])
def test_only_unsupported_current_bath_is_removed(body, removed):
    assert polish_reply(body, UNKNOWN) == (
        ("", {"unconfirmed_bath"}) if removed else (body, set())
    )


def dated(role, body, stamp):
    return {"role": role, "content": f"{body}\n<system_reminder>Current datetime: {stamp:%Y-%m-%d %H:%M:%S}</system_reminder>"}


def manager(plugin, current, histories):
    def conversation(_, cid):
        return SimpleNamespace(history=histories.get(cid, []))

    plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(side_effect=lambda _: current[0]),
        get_conversation=AsyncMock(side_effect=conversation),
        update_conversation=AsyncMock(),
    )
    return plugin.context.conversation_manager


async def test_recent_six_user_turns_preserve_tone_within_thirty_minutes(plugin, umo, clock):
    r = plugin.runner
    now = datetime.fromtimestamp(clock.now)
    history = [dated("user", "older than the window", now - timedelta(minutes=30, seconds=1))]
    for i in range(8):
        history.extend([dated("user", str(i), now - timedelta(minutes=8-i)),
                        {"role": "assistant", "content": f"reply {i}"}])
    history.extend([{"role": "system", "content": "system line"},
                    {"role": "user", "content": "undated"},
                    {"role": "assistant", "content": "undated continuation"}])
    m = manager(plugin, ["target"], {"target": history, "other": [dated("user", "wrong cid", now)]})
    dialogue = await r._proactive_dialogue(umo, "target", now)
    users = [r._plain_content(it["content"]).splitlines()[0] for it in dialogue if it["role"] == "user"]
    assert users == list("234567")
    assert [it["content"] for it in dialogue if it["role"] == "assistant"] == [f"reply {i}" for i in range(2, 8)]
    m.get_conversation.assert_awaited_once_with(umo, "target")


async def test_switched_conversation_filters_old_bath_and_preserves_reminders(plugin, umo, clock):
    r = plugin.runner
    now = datetime.fromtimestamp(clock.now)
    current = ["first"]
    histories = {
        "first": [dated("user", "我正在洗澡", now)],
        "second": [dated("user", "刚才那句很有意思", now), {"role": "assistant", "content": "热水已经放进浴缸"}],
    }
    m = manager(plugin, current, histories)
    r.bind_message_scene(umo, current[0])
    r.update_from_message(umo, "我正在洗澡")
    future = {"at": clock.now + 86400, "label": "开会", "stage": "pending"}
    r._save_appointments(umo, [future])
    assert r._proactive_scene(umo, cid="first")[1] == "shower"
    assert polish_reply(INCIDENT, r._proactive_scene(umo, cid="first"))[0] == INCIDENT

    current[0] = "second"
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=INCIDENT)))
    plugin.context.get_using_provider = lambda **_: provider
    result = await r._fire(umo)
    assert result["reason"] == "proactive_filtered"
    assert not plugin.store.get(umo, "unanswered")
    provider.text_chat.assert_awaited_once()
    call = provider.text_chat.await_args.kwargs
    assert "他：在洗澡" not in call["prompt"]
    assert "开会" in call["prompt"]
    assert "助手说过的话只作话题和语气参考" in call["prompt"]
    assert "热水已经放进浴缸" in str(call["contexts"])
    assert "我正在洗澡" not in str(call["contexts"])
    assert r._appointments(umo) == [future]
    plugin.context.send_message.assert_not_awaited()
    m.update_conversation.assert_not_awaited()


async def test_first_user_message_can_bind_to_newly_created_conversation(plugin, umo):
    r = plugin.runner
    current = [None]
    manager(plugin, current, {})
    extras = {}
    event = SimpleNamespace(unified_msg_origin=umo, message_str="我正在洗澡",
                            get_messages=lambda: [], set_extra=extras.__setitem__,
                            get_extra=extras.get)
    # Use the real private hook and agent-entry hook.
    from astrbot.core.message.components import Plain
    event.get_messages = lambda: [Plain(event.message_str)]
    await plugin.on_private_message(event)
    current[0] = "new"
    await plugin.guard_chat_history(event, SimpleNamespace(messages=[]))
    assert r._proactive_scene(umo, cid="new")[1] == "shower"


@pytest.mark.parametrize("end", ["我洗完了", "其实我没在洗澡", "我其实不是在洗澡"])
def test_confirmed_bath_ends_before_next_topic(plugin, umo, end):
    r = plugin.runner
    r.update_from_message(umo, "我正在洗澡")
    r.update_from_message(umo, end)
    r.update_from_message(umo, "抱抱喵")
    scene = r._proactive_scene(umo)
    assert scene[1] != "shower"
    assert polish_reply(INCIDENT, scene)[0] == ""


def test_unconfirmed_model_state_is_not_a_fact(plugin, umo):
    r = plugin.runner
    r._set_event(umo, r.engine.get_rule("eating"), "语义:eating", confirmed=False)
    assert r._proactive_scene(umo) == UNKNOWN
    r.update_from_message(umo, "我正在吃饭")
    assert r._proactive_scene(umo)[1] == "eating"


@pytest.mark.parametrize("switch", [False, True])
def test_old_undo_cannot_restore_bath_in_new_conversation(plugin, umo, clock, switch):
    r = plugin.runner
    current = ["first"]
    manager(plugin, current, {})
    r.bind_message_scene(umo, current[0])
    r.update_from_message(umo, "我正在洗澡")
    clock.now += 1
    r.update_from_message(umo, "我洗完了")
    finish = next(row for row in r.decisions.items(umo) if row["kind"] == "stop")
    if switch:
        clock.now += 1
        current[0] = "second"
        r.bind_message_scene(umo, current[0])
        with pytest.raises(ValueError):
            r.undo_decision(finish["id"])
        assert r._proactive_scene(umo, cid=current[0]) == UNKNOWN
    else:
        r.undo_decision(finish["id"])
        assert r._proactive_scene(umo, cid=current[0])[1] == "shower"


async def test_generated_panel_preview_cannot_offer_wrong_scene(plugin, umo, monkeypatch):
    r = plugin.runner
    r.started = True
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value=INCIDENT))
    assert (await plugin.panel_service.preview({"umo": umo}))["segments"] == []
    plugin.context.send_message.assert_not_awaited()


async def test_expired_conversation_does_not_allow_invented_proactive_experience(plugin, umo, clock, monkeypatch):
    runner = plugin.runner
    now = datetime.fromtimestamp(clock.now)
    safe = "晚上想吃点什么喵？"
    generated = "我今天下班早，刚到家\n回家路上遇到一只白猫在晒太阳，可悠闲了\n" + safe
    old = now - timedelta(minutes=45)
    manager(plugin, ["target"], {"target": [dated("user", "午饭吃完了，聊点别的吧", old),
                                            dated("assistant", "冰箱里还有布丁", old)]})
    generate = AsyncMock(return_value=generated)
    monkeypatch.setattr(runner, "_generate", generate)
    assert (await runner._fire(umo))["sent"]
    generate.assert_awaited_once()
    assert generate.await_args.kwargs["dialogue"] == []
    sent = plugin.context.send_message.await_args.args[1].get_plain_text()
    assert sent.rstrip("？?") == safe.rstrip("？?")


@pytest.mark.parametrize("body,history,allow", [
    ("冰淇淋我留好了，下午再拿出来", [], False),
    ("你再不来我就先把冰淇淋吃掉", [], False),
    ("布丁我留着了", ["我留了布丁给你"], False),
    ("布丁我留着了喵", ["布丁没留", "你给我留了布丁"], False),
    ("布丁我留着了喵", ["你给我留了布丁"], True),
    ("冰箱里还有布丁", ["吃布丁吧"], False),
    ("冰箱里还有布丁", ["冰箱里还有布丁"], True),
    ("冰箱里还有布丁", ["布丁吃完了", "冰箱里还有布丁"], False),
    ("晚上想一起吃布丁吗？", [], True),
    ("如果冰箱还有布丁，晚上就一起吃吧", [], True),
    ("如果你想吃，布丁我已经留好了", [], False),
    ("刚下班喵", ["我刚下班"], False),
    ("刚下班喵", ["你刚下班吧？"], False),
    ("刚下班喵", ["你刚下班了"], True),
    ("刚到家喵", ["你刚下班了"], False),
    ("路上碰见一只橘猫", ["你路上看见一只黑猫"], False),
    ("路上碰见一只橘猫", ["你路上没看见一只橘猫"], False),
    ("路上碰见一只橘猫", ["你路上看见一只橘猫"], True),
    ("刚想到你，想抱抱喵", [], True),
    ("假装我刚到家，一起进去吧", [], True),
    ("台词是「我刚到家，路上撞见一只橘猫」", [], True),
])
def test_proactive_experience_and_food_require_matching_user_evidence(body, history, allow):
    output, reasons = polish_reply(body, UNKNOWN, [(text, None) for text in history])
    if allow:
        assert (output, reasons) == (body, set())
    else:
        assert not output and reasons.intersection({"unconfirmed_experience", "unconfirmed_dessert"})


def test_dessert_pressure_shares_topic_but_user_offer_can_reopen_it():
    first = fingerprint("蛋糕给你留着，下午再一起吃", 1, UNKNOWN)
    second = fingerprint("你再不回来我就先把蛋糕吃掉", 2, UNKNOWN)
    assert duplicate(second, [first], {}) == "same_topic"
    assert not topics("你吃了没", from_user=True)
    assert not topics("我不想吃蛋糕", from_user=True)
    updates = dict.fromkeys(topics("吃蛋糕吧", from_user=True), 1.5)
    assert updates == {"dessert:蛋糕": 1.5}
    assert duplicate(second, [first], updates) is None
