import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_mitchell_minichat.core import proactive_quality as quality
from astrbot_plugin_mitchell_minichat.core.runner import Runner
from astrbot_plugin_mitchell_minichat.core.store import Store

def turn(role, body, stamp=None):
    if stamp:
        body += f"\n<system_reminder>Current datetime: {stamp:%Y-%m-%d %H:%M:%S}</system_reminder>"
    return {"role": role, "content": body}


def attach(plugin, history, text):
    plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="cid"),
        get_conversation=AsyncMock(return_value=SimpleNamespace(history=history)),
        update_conversation=AsyncMock(),
    )
    model = AsyncMock(return_value=SimpleNamespace(completion_text=text))
    plugin.context.get_using_provider = lambda **_: SimpleNamespace(text_chat=model)
    return model



@pytest.mark.parametrize("statement,expected", [
    ("哥哥洗完了没，小星这边布丁都摆凉了。", ""),
    ("哥哥，饭都凉了。趁热快来吃。想跟你说句话喵。", "想跟你说句话喵。"),
    ("哥哥到底进不进来呀，水都烧上了。快进厨房。", ""),
    ("如果水烧上了，就喊我一声。", "如果水烧上了，就喊我一声。"),
])
def test_scene_premises(statement, expected):
    now = datetime(2026, 11, 24, 10, 10)
    assert quality.check_scene_facts(statement, ("unknown", "", "", ""), (), now=now)[0] == expected


@pytest.mark.parametrize("utterance,reply", [
    ("我正在洗脸", "哥哥洗完了没？"),
    ("布丁都摆凉了", "小星这边布丁都摆凉了。"),
    ("我把水烧上了", "水都烧上了。"),
    ("我正在洗澡", "洗好了吗？"),
])
def test_confirmed_current_details_pass(utterance, reply):
    now = datetime(2026, 11, 24, 10, 10)
    scene = ("unknown", "shower" if "洗澡" in utterance else "", "self", "active")
    assert quality.check_scene_facts(reply, scene, [(utterance, now)], now=now) == (reply, set())


@pytest.mark.parametrize("replied", [True, False, None])
def test_reply_pressure_and_device_sound(replied):
    now = datetime(2026, 11, 24, 10, 10)
    check = lambda text: quality.check_proactive_assumptions(text, (), (), now, replied, bot_nicknames=("小星",))[0]
    assert "没声音" not in check("哥哥…一上午都没声音，小星等得呆毛都蔫了喵")
    assert "没理" not in check("哥哥，布丁都吃完了你还没理小星")
    assert check("新耳机一上午都没声音，还要调音量吗？") == "新耳机一上午都没声音，还要调音量吗？"
    assert check("你说过「一上午都没声音」呀") == "你说过「一上午都没声音」呀"


async def test_same_state_in_model_prompt_and_delivery(plugin, umo, clock):
    now = datetime(2026, 11, 22, 19, 23)
    clock.now = now.timestamp()
    runner = plugin.runner
    plugin.engine_cfg["bot_nicknames"] = "小星"
    history = [turn("user", "晚餐准备做什么", now - timedelta(minutes=56)),
               turn("user", "小星弄吧", now - timedelta(minutes=32)),
               turn("assistant", "那就小星来弄，哥哥坐着等")]
    model = attach(plugin, history, "[[SEND:CONFIRMED_TOPIC]]\n哥哥，晚餐到底吃什么？\n想来跟你聊两句")
    await runner._fire(umo)
    model.assert_awaited_once()
    assert "由你负责" in model.await_args.kwargs["prompt"]
    bodies = [call.args[1].get_plain_text() for call in plugin.context.send_message.await_args_list]
    assert any("想来跟你聊" in body for body in bodies)
    assert not any("到底" in body for body in bodies)
    assert "meal_already_assigned" in runner.proactive_review.recent()[0]["checks"]


@pytest.fixture
def night(plugin, clock):
    plugin.engine_cfg.update(enable_sleep_reminder=True, nap_remind_enabled=False, night_start_time="18:00",
                             remind_window_start="23:50", remind_window_end="00:20")
    clock.now = datetime(2026, 11, 23, 21, 59).timestamp()
    return plugin.runner


def fake_event(umo, message):
    values = {}
    return SimpleNamespace(unified_msg_origin=umo, message_str=message, get_messages=lambda: [],
                           set_extra=values.__setitem__, get_extra=values.get)


async def test_real_elliptical_request_and_prompt(plugin, umo, clock, night):
    now = datetime.fromtimestamp(clock.now)
    attach(plugin, [turn("user", "这本书还差一章", now - timedelta(minutes=1)),
                    turn("assistant", "早点睡吧，明天再看。")], "")
    event = fake_event(umo, "我还想看会儿书\n十二点再催吧")
    await plugin.on_private_message(event)
    result = event.get_extra("_minichat_timed_sleep")
    at = datetime(2026, 11, 24).timestamp()
    assert result["status"] == "saved" and result["at"] == at
    assert Store(night.store.path.parent).get(umo, "sleep_state")["remind_at"] == at
    request = ProviderRequest(system_prompt="角色")
    await plugin.timed_sleep_request(event, request)
    assert "角色" in request.system_prompt and "11月24日 00:00" in request.system_prompt
    clock.now = at - 1
    assert not night.should_remind_sleep(umo)
    clock.now = at
    assert night.should_remind_sleep(umo)


async def test_outside_window_reschedule_reload_and_once(plugin, umo, clock, night, monkeypatch):
    assert night.update_from_message(umo, "凌晨一点半催我睡觉")
    assert night.take_timed_sleep_result()["status"] == "saved"
    assert night.update_from_message(umo, "改到凌晨一点四十")
    at = datetime(2026, 11, 24, 1, 40).timestamp()
    assert night._timed_sleep_at(umo) == at
    plugin.store = Store(night.store.path.parent)
    plugin.runner = restored = Runner(plugin)
    await restored.start()
    assert restored.store.get(umo, "next_at") <= at
    clock.now = datetime(2026, 11, 23, 23, 56).timestamp()
    assert not restored.should_remind_sleep(umo)
    clock.now = at
    generate = AsyncMock(return_value="时间到了，早点休息喵")
    monkeypatch.setattr(restored, "_generate", generate)
    assert (await restored._fire(umo))["sent"]
    assert generate.await_args.kwargs["reminder"]
    assert restored._timed_sleep_at(umo) == 0
    assert not restored.should_remind_sleep(umo)


@pytest.mark.parametrize("message,status", [
    ("随口说「十二点再催」", None),
    ("如果十二点再催我睡觉", None),
    ("没让你十二点叫我睡觉", None),
    ("十二点再催", "unknown_topic"),
    ("十二点还是一点再催我睡觉", "ambiguous"),
    ("二十九点催我睡觉", "ambiguous"),
])
def test_no_guessed_or_reported_schedule(night, umo, message, status):
    night.update_from_message(umo, message)
    result = night.take_timed_sleep_result()
    assert (result["status"] if result else None) == status
    assert night._timed_sleep_at(umo) == 0


@pytest.mark.parametrize("message,state", [("别再催我了", None), ("晚安，我睡了", "sleeping")])
def test_cancel_or_sleep_clears_timed_reminder(night, umo, message, state):
    night.update_from_message(umo, "凌晨一点半催我睡觉")
    night.update_from_message(umo, message)
    assert night._timed_sleep_at(umo) == 0
    assert night._sleep_state(umo).get("state") == state


async def test_storage_failure_rolls_back_and_reply_cannot_promise(plugin, night, umo, monkeypatch):
    night.update_from_message(umo, "凌晨一点半催我睡觉")
    original = night._timed_sleep_at(umo)
    def fail(*args):
        raise OSError("write failed")
    monkeypatch.setattr(os, "replace", fail)
    event = fake_event(umo, "改到凌晨两点")
    await plugin.on_private_message(event)
    result = event.get_extra("_minichat_timed_sleep")
    assert result["status"] == "failed"
    assert night._timed_sleep_at(umo) == original
    response = SimpleNamespace(role="assistant", completion_text="好，小星两点会催哥哥睡。")
    await plugin.guard_chat_reply(event, SimpleNamespace(messages=[]), response)
    assert "没存上" in response.completion_text and "会催" not in response.completion_text


async def test_appointment_first_and_timed_sleep_expires_or_delivers(plugin, umo, clock, night, monkeypatch):
    night.update_from_message(umo, "凌晨十二点催我睡觉")
    at = datetime(2026, 11, 24).timestamp()
    night._save_appointments(umo, [{"id": "appt", "at": at, "kind": "event", "label": "交作业", "stage": "pending"}])
    generate = AsyncMock(return_value="时间到了，早点休息喵。晚安呀。")
    monkeypatch.setattr(night, "_generate", generate)
    clock.now = at
    assert (await night._fire(umo))["sent"]
    assert generate.await_args.kwargs["appointment"] == "due"
    assert night._timed_sleep_at(umo) == at
    clock.now += 60
    plugin.context.send_message.side_effect = [True, False]
    result = await night._fire(umo)
    assert result["delivery_status"] == "partial" and generate.await_args.kwargs["reminder"]
    assert night._timed_sleep_at(umo) == 0
    clock.now = datetime(2026, 11, 24, 21).timestamp()
    night.update_from_message(umo, "凌晨十二点催我睡觉")
    clock.now = datetime(2026, 11, 25, 0, 5).timestamp()
    assert not night.should_remind_sleep(umo) and night._timed_sleep_at(umo) == 0


async def test_immediate_history_can_fill_sleep_topic_but_old_or_new_topic_cannot(plugin, umo, clock, night):
    now = datetime.fromtimestamp(clock.now)
    attach(plugin, [turn("user", "几点睡", now - timedelta(minutes=1)),
                    turn("assistant", "先睡吧")], "")
    assert await night.timed_sleep_context(umo, "cid", "十二点再催", clock.now)
    attach(plugin, [turn("user", "几点睡", now - timedelta(minutes=15)),
                    turn("assistant", "先睡吧")], "")
    assert not await night.timed_sleep_context(umo, "cid", "十二点再催", clock.now)
    attach(plugin, [turn("user", "几点睡", now - timedelta(minutes=2)),
                    turn("assistant", "先睡吧"), turn("user", "晚饭吃什么", now - timedelta(minutes=1)),
                    turn("assistant", "汉堡呀")], "")
    assert not await night.timed_sleep_context(umo, "cid", "十二点再催", clock.now)


def test_later_correction_replaces_food_state_and_meal_assignment():
    now = datetime(2026, 11, 24, 18, 10)
    scene = ("unknown", "", "", "")
    assert not quality.check_scene_facts(
        "饭做好了。", scene, (("饭还没做好", now), ("饭做好了", now - timedelta(minutes=1))), now=now)[0]
    assert quality.meal_owner((
        ("我来弄吧", now), ("小星弄吧", now - timedelta(minutes=30)),
        ("晚饭吃什么", now - timedelta(minutes=32)),
    ), now, ("小星",)) == "user"
    assert not quality.meal_owner((
        ("吃完了", now), ("小星弄吧", now - timedelta(minutes=30)),
        ("晚饭吃什么", now - timedelta(minutes=32)),
    ), now, ("小星",))


async def test_context_change_discards_pending_model_output(plugin, umo, clock, night):
    now = datetime.fromtimestamp(clock.now)
    attach(plugin, [turn("user", "我正在洗脸", now - timedelta(minutes=1))], "")
    plugin.context.conversation_manager.get_curr_conversation_id.side_effect = ["cid", "new"]
    model = AsyncMock(return_value=SimpleNamespace(
        completion_text="[[SEND:CONFIRMED_TOPIC]]\n洗完了没？"))
    plugin.context.get_using_provider = lambda **_: SimpleNamespace(text_chat=model)
    result = await night._fire(umo)
    assert model.await_count == 1
    assert "用户正在洗脸" in model.await_args.kwargs["prompt"]
    assert result["reason"] == "stale_generation"
    plugin.context.send_message.assert_not_awaited()


def test_timed_sleep_disabled_preserves_unrelated_appointment(plugin, umo, night):
    plugin.engine_cfg["enable_sleep_reminder"] = False
    night.update_from_message(umo, "凌晨一点半催我睡觉，明天下午三点开会")
    assert night.take_timed_sleep_result()["status"] == "disabled"
    assert night._timed_sleep_at(umo) == 0
    assert night._appointments(umo)[0]["label"] == "开会"
