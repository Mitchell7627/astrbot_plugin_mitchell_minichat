import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_mitchell_minichat.core.reply_guard import FALLBACK, clean_history, inspect_text
from astrbot_plugin_mitchell_minichat.core.migrations import (
    LEGACY_PROACTIVE_PROMPT_V034, migrate_config,
)


INCIDENT = (
    '今天辛苦啦\n坐下来歇一会儿吧\n慢慢来喵\n\n'
    'Hmm "well" opener — he said don\'t start with that phrase. '
    'Maybe avoid it. Let\'s do:\n\n'
    '"先歇一会儿吧"\n"慢慢来喵"\n先歇一会儿吧\n慢慢来喵'
)


@pytest.mark.parametrize("text", [
    "Hello! How was your day?", "Analysis: Your proposal needs a budget.",
    '她说「Analysis: test」', "`<think>hint</think>`",
    'Hmm, that sounds nice!', "头发别滴到肩膀", "摸摸头喵",
])
def test_clean_reply_is_unchanged(text):
    assert inspect_text(text) == (text, None)


def test_incident_is_removed_from_visible_and_loaded_history():
    history = [{"role": "assistant", "content": INCIDENT}]
    expected = INCIDENT.split("\n\n")[0]
    assert inspect_text(INCIDENT) == (expected, "draft_removed")
    assert clean_history(history)[0]["content"] == expected
    assert history[0]["content"] == INCIDENT


async def test_old_draft_is_hidden_during_generation_but_kept_on_disk(plugin, umo):
    from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
    from astrbot.core.provider.entities import LLMResponse

    extras = {}
    event = SimpleNamespace(unified_msg_origin=umo, set_extra=extras.__setitem__, get_extra=extras.get)
    old = Message(role="assistant", content=[TextPart(text=INCIDENT)])
    current = Message(role="assistant", content=[TextPart(text="新回复")])
    wrapper = SimpleNamespace(messages=[old])
    await plugin.guard_chat_history(event, wrapper)
    assert old.content[0].text == INCIDENT.split("\n\n")[0]
    wrapper.messages.append(current)
    await plugin.guard_chat_reply(event, wrapper, LLMResponse(role="assistant", completion_text="新回复"))
    saved = dump_messages_with_checkpoints(wrapper.messages)
    assert saved[0]["content"][0]["text"] == INCIDENT
    assert saved[1]["content"][0]["text"] == "新回复"


@pytest.mark.parametrize("text,expected", [
    ("<think>secret</think>你好喵", "你好喵"),
    ("Analysis: internal\nFinal answer: 你好喵", "你好喵"),
    ('Hmm "唔" opener', FALLBACK),
    ("<think>no closing tag", FALLBACK),
    (INCIDENT, INCIDENT.split("\n\n")[0]),
])
async def test_real_astrbot_final_hook_cleans_send_and_saved_message(plugin, umo, text, expected, monkeypatch):
    from astrbot.core.agent.message import Message, TextPart
    from astrbot.core.astr_agent_hooks import MainAgentHooks
    from astrbot.core import astr_agent_hooks
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.message.components import Image, Plain
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.provider.entities import LLMResponse

    event = SimpleNamespace(unified_msg_origin=umo)
    image = Image.fromURL("https://example.invalid/image.png")
    response = LLMResponse(role="assistant", result_chain=MessageChain([Plain(text), image]))
    messages = []
    wrapper = SimpleNamespace(messages=messages, context=SimpleNamespace(event=event))

    async def dispatch(event, event_type, *args):
        if event_type.name == "OnAgentDoneEvent":
            await plugin.guard_chat_reply(event, *args)

    monkeypatch.setattr(astr_agent_hooks, "call_event_hook", dispatch)

    runner = SimpleNamespace(run_context=wrapper, stats=SimpleNamespace(), _transition_state=lambda state: None,
                             agent_hooks=MainAgentHooks(), _resolve_unconsumed_follow_ups=lambda: None)
    await ToolLoopAgentRunner._complete_with_assistant_response(runner, response)
    assert response.result_chain.chain == [Plain(expected), image]
    assert isinstance(messages[0], Message)
    assert [p.text for p in messages[0].content if isinstance(p, TextPart)] == [expected]


def test_known_default_is_migrated_without_overwriting_custom_text(plugin):
    friend = plugin.friend_cfg
    friend["proactive_prompt"] = LEGACY_PROACTIVE_PROMPT_V034
    assert "proactive_prompt" in migrate_config(plugin.config)
    assert "肩膀借我" not in friend["proactive_prompt"]
    friend["proactive_prompt"] = "我自己的主动提示词"
    assert "proactive_prompt" not in migrate_config(plugin.config)
    assert friend["proactive_prompt"] == "我自己的主动提示词"


@pytest.mark.parametrize("text", [
    "要不然磨到了", "你这样好像在汇报工作喵", "想吃面呢",
    "加上对方在睡觉时", "两点半哥哥就去上课了",
])
def test_real_log_noncurrent_actions_do_not_change_scene(plugin, umo, clock, text):
    clock.now = datetime(2026, 11, 9, 12, 45).timestamp()
    runner = plugin.runner
    runner.update_from_message(umo, text)
    assert runner._proactive_scene(umo) == ["unknown", "", "", ""]
    assert not runner.is_sleeping(umo)


@pytest.mark.parametrize("start,end", [("我在吃饭", "吃完咯"), ("我在洗澡", "洗完咯")])
def test_end_signal_survives_affectionate_followup(plugin, umo, clock, start, end):
    r = plugin.runner
    r.update_from_message(umo, start)
    assert plugin.store.get(umo, "active_event")
    r.update_from_message(umo, end)
    r.update_from_message(umo, "抱抱")
    assert plugin.store.get(umo, "active_event") is None


def test_ordering_moves_out_of_commute_without_claiming_food_is_served(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "准备出门吃咯")
    assert r._proactive_scene(umo)[1] == "commute"
    r.update_from_message(umo, "点好咯")
    r.update_from_message(umo, "等着吧喵")
    assert r._proactive_scene(umo)[3] == "ordered"
    assert "何时开始吃还不确定" in r._proactive_hint(umo)
    r.update_from_message(umo, "好好吃")
    assert r._proactive_scene(umo)[3] == "active"


@pytest.mark.parametrize("first,second,third", [
    ("肩膀借我一会儿", "那就给我搭一下肩膀", "往这边靠点，让我挨着"),
    ("被子跑到你那边啦\n再眯一会儿", "被子被我卷过来了，往中间挪挪",
     "挪一点，别把被子都卷走啦"),
])
async def test_recent_opening_does_not_block_checked_message(plugin, umo, clock, monkeypatch, first, second, third):
    r = plugin.runner
    generate = AsyncMock(side_effect=[first, second, third, "头发别滴到肩膀"])
    monkeypatch.setattr(r, "_generate", generate)
    assert (await r._fire(umo))["sent"]
    plugin.friend_cfg["schedule_settings"]["max_unanswered_times"] = 0
    for _ in range(2):
        clock.now += 3600
        assert (await r._fire(umo))["sent"]
    assert plugin.store.get(umo, "unanswered") == 3
    assert (await r._fire(umo))["sent"]
    assert generate.await_count == 4


async def test_original_conversation_keeps_only_successful_part(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    current = "original"
    h = []
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(side_effect=lambda _: current),
        get_conversation=AsyncMock(return_value=SimpleNamespace(history=h)),
        update_conversation=AsyncMock(),
    )
    plugin.context.conversation_manager = manager
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value="第一段。第二段。"))

    async def send(*args):
        nonlocal current
        current = "different"
        return True

    plugin.context.send_message.side_effect = send
    report = await r._fire(umo)
    assert report["delivery_status"] == "partial"
    assert plugin.context.send_message.await_count == 1
    assert manager.update_conversation.await_args.args[1] == "original"
    saved = manager.update_conversation.await_args.kwargs["history"][-1]["content"]
    assert saved.startswith("第一段。\n<system_reminder>Current datetime:")
    assert "第二段" not in saved


async def test_changed_scene_while_generating_is_rejected(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    entered, resume = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await resume.wait()
        return "旧场景"

    monkeypatch.setattr(r, "_generate", generate)
    fire = asyncio.create_task(r._fire(umo))
    await asyncio.wait_for(entered.wait(), 2)
    r._set_event(umo, r.engine.get_rule("eating"), "吃饭")
    resume.set()
    assert (await asyncio.wait_for(fire, 2))["reason"] == "stale_generation"
    plugin.context.send_message.assert_not_awaited()

@pytest.mark.parametrize("text,limit,short,expected", [
    ("汤刚盛好，小心烫\n先尝一小口\n闻起来真香", 1, 0,
     ["汤刚盛好，小心烫，先尝一小口，闻起来真香"]),
    ("出门啦\n说好去买菜的，我等了好久", 7, 8, ["出门啦，说好去买菜的，我等了好久"]),
    ("Hello\nHow are you?", 1, 8, ["Hello How are you?"]),
    ("先试一下。\n—\n慢慢来就好喵", 7, 0, ["先试一下。—", "慢慢来就好喵"]),
])
def test_split_merges_preserve_pause_and_order(text, limit, short, expected):
    from astrbot_plugin_mitchell_minichat.core.splitter import split_text

    assert split_text(text, max_segments=limit, min_length=short, balanced=False) == expected


@pytest.mark.parametrize("enabled", [True, False])
async def test_preview_matches_actual_segments(plugin, umo, monkeypatch, enabled):
    r = plugin.runner
    r.started = True
    text = "先休息一下\n再聊聊最近的事"
    r.cfg["segmented_reply_settings"]["enable"] = enabled
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value=text))
    preview = await plugin.panel_service.preview({"umo": umo})
    await plugin.panel_service.send({"umo": umo, "text": text})
    delivered = [call.args[1].get_plain_text() for call in plugin.context.send_message.await_args_list]
    assert preview["segments"] == delivered


@pytest.mark.parametrize("reply,expected", [
    ("还没到十点呢，先休息一下", True),
    ("还没到十一点呢，先休息一下", False),
    ("现在十点整了", True),
    ("现在十点了", False),
    ("已经十点啦", False),
    ("已经十一点啦", True),
    ("现在 10:06", False),
    ("十点下课的话记得回来", False),
    ("「还没到十点」是昨天说的", False),
    ("现在十点半？", False),
])
def test_current_time_claim(reply, expected):
    from astrbot_plugin_mitchell_minichat.core.proactive_quality import contradictory_clock

    assert contradictory_clock(reply, datetime(2026, 11, 17, 10, 6)) == expected


async def test_wrong_clock_never_reaches_send(plugin, umo, clock, monkeypatch):
    clock.now = datetime(2026, 11, 17, 10, 6).timestamp()
    r = plugin.runner
    generate = AsyncMock(return_value="还没到十点呢，先休息一下")
    monkeypatch.setattr(r, "_generate", generate)
    result = await r._fire(umo)
    assert result["reason"] == "clock_conflict"
    generate.assert_awaited_once()
    plugin.context.send_message.assert_not_awaited()
    assert not plugin.store.get(umo, "unanswered")


@pytest.mark.parametrize("text", ["甜点放桌上了", "十一点半下课啦", "甜点买好了\n十二点下课吧",
                                  "上午十点怎么样，十二点下课喔"])
async def test_due_reply_keeps_registered_subject_and_clock(plugin, umo, clock, monkeypatch, text):
    r = plugin.runner
    r.update_from_message(umo, "十二点下课哦")
    clock.now = datetime(2026, 11, 2, 12).timestamp()
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value=text))
    assert (await r._fire(umo))["sent"]
    sent = plugin.context.send_message.await_args_list[0].args[1].get_plain_text()
    assert "12:00" in sent and "下课" in sent
    assert "甜点" not in sent and "十一点" not in sent


async def test_recent_dessert_topic_does_not_block_a_grounded_new_message(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    monkeypatch.setattr(r, "_proactive_dialogue", AsyncMock(return_value=[
        {"role": "user", "content": "你已经买好了布丁，冰箱里还有布丁，布丁给我留着"},
    ]))
    replies = AsyncMock(side_effect=["布丁我已经买好了喵", "学累了歇一下\n布丁还在冰箱里凉着",
                                    "冰箱里那份布丁给你留着喵"])
    monkeypatch.setattr(r, "_generate", replies)
    assert (await r._fire(umo))["sent"]
    clock.now += 15 * 60
    r.update_from_message(umo, "我正在上课")
    assert (await r._fire(umo))["sent"]
    clock.now += 15 * 60
    r.update_from_message(umo, "布丁刚买好了，我放冰箱啦")
    assert (await r._fire(umo))["sent"]


@pytest.mark.parametrize("signal,hint", [("洗完澡咯", "已经洗完澡"), ("吃完咯", "已经吃完"), ("到咯", "地点未确认")])
def test_bare_completion_with_no_live_event_never_offers_activity_model(plugin, umo, clock, signal, hint):
    r = plugin.runner
    r.update_from_message(umo, signal)
    assert r.take_llm_candidate(umo) is None
    assert not plugin.store.get(umo, "active_event")
    assert hint in r._proactive_hint(umo)


def test_almost_finished_lesson_preserves_active_event(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我正在上课")
    r.update_from_message(umo, "快下课了")
    assert r._proactive_scene(umo)[1] == "study"
    assert r.take_llm_candidate(umo) is None
    r.update_from_message(umo, "下课了")
    assert not plugin.store.get(umo, "active_event")
