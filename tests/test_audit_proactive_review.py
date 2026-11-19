from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_mitchell_minichat.core import proactive_quality as q
from astrbot_plugin_mitchell_minichat.core.proactive_review import ProactiveReview, read_draft, read_review


def stamp(role, text, when):
    return {"role": role, "content": f"{text}\n<system_reminder>Current datetime: {when:%Y-%m-%d %H:%M:%S}</system_reminder>"}


def attach(plugin, messages, output):
    mgr = SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid"),
                          get_conversation=AsyncMock(return_value=SimpleNamespace(history=messages)),
                          update_conversation=AsyncMock())
    plugin.context.conversation_manager = mgr
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=output)))
    plugin.context.get_using_provider = lambda **_: provider
    return mgr, provider


@pytest.mark.parametrize("draft,body,reason", [
    ("[[SEND:FOLLOW_UP]]\n过来歇一歇呀", "过来歇一歇呀", "model_follow_up"),
    ("[[NO_SEND:ANSWERED]]\n不应外发", "", "model_answered"),
    ("[[SEND:CONFIRMED_TOPIC]]同一行", "", "model_bad_label"),
    ("[[SEND:PRIVATE_THOUGHT]]\n不应外发", "", "model_bad_label"),
    ("先说一句\n[[NO_SEND]]", "", "model_bad_label"),
    ("[[REVIEW:不该外发]]\n嗨", "", "model_bad_label"),
    ("[[SEND:GREETING]]\n你好\n自检：不该外发", "", "model_bad_label"),
    ("没有标签的正常答复", "没有标签的正常答复", "model_unreported"),
])
def test_review_protocol_does_not_leak_labels(draft, body, reason):
    assert read_draft(draft) == (body, reason)


async def test_replied_and_unsupported_meal_are_pruned_before_sending(plugin, umo, clock):
    r = plugin.runner
    now = datetime.fromtimestamp(clock.now)
    n = now - timedelta(minutes=3)
    r.store.set(umo, "proactive_recent", [q.fingerprint("我来啦", clock.now - 300, ["unknown", "", "", ""])])
    mgr, provider = attach(plugin, [stamp("user", "今天午休看了小说", n), stamp("assistant", "看到哪啦", n)],
        "[[SEND:FOLLOW_UP]]\n你一直没有回我。\n不吃的话那我就自己吃。\n你读的那本书叫什么呀？")
    result = await r._fire(umo)
    assert result["sent"] and result["delivered_segments"] == 1
    provider.text_chat.assert_awaited_once()
    assert "对方在上次主动消息之后回过" in provider.text_chat.await_args.kwargs["prompt"]
    assert "那本书" in plugin.context.send_message.await_args.args[1].get_plain_text()
    saved = mgr.update_conversation.await_args.kwargs["history"][-1]["content"]
    assert "那本书" in saved and "SEND" not in saved and "没有回我" not in saved
    items = plugin.panel_service.proactive_review()["items"]
    assert items[0]["model"] == "model_follow_up" and items[0]["delivered_segments"] == 1
    assert set(items[0]["checks"]) == {"reply_received", "unconfirmed_meal_pressure"}
    disk = r.proactive_review.path.read_text(encoding="utf-8")
    assert not any(text in disk for text in ("那本书", "没有回我", "自己吃", "[[SEND"))
    assert ProactiveReview(r.proactive_review.path).recent() == items


async def test_answered_social_word_can_still_use_model_and_report_skip(plugin, umo, clock):
    r = plugin.runner
    now = datetime.fromtimestamp(clock.now)
    messages = [stamp("user", "小笨蛋", now - timedelta(minutes=2)), stamp("assistant", "谁笨啦", now)]
    mgr, provider = attach(plugin, messages, "[[NO_SEND:NO_NATURAL_LINE]]\n[[REVIEW:打趣已经接完，这会儿先不打扰。]]\n不应外发")
    assert (await r._fire(umo))["reason"] == "proactive_self_skip"
    provider.text_chat.assert_awaited_once()
    messages.append(stamp("user", "小笨蛋，今天窗外有只鸽子", now))
    assert (await r._fire(umo))["reason"] == "proactive_self_skip"
    assert provider.text_chat.await_count == 2
    plugin.context.send_message.assert_not_awaited()
    mgr.update_conversation.assert_not_awaited()
    assert plugin.store.get(umo,"unanswered",0) == 0
    records = r.proactive_review.recent()
    assert [(x["route"], x["model"]) for x in records] == [
        ("topic", "model_no_natural_line"), ("persona_greeting", "model_no_natural_line"),
    ]
    assert all(x["summary"] == "打趣已经接完，这会儿先不打扰。" for x in records)


@pytest.mark.parametrize("summary", ["<b>草稿</b>", "手机号 123456789", "甲" * 73, "https://some.example"])
def test_unsuitable_summary_never_leaks_to_body_or_saved_summary(summary):
    assert read_review(f"[[SEND:GREETING]]\n[[REVIEW:{summary}]]\n来打声招呼") == (
        "来打声招呼", "model_greeting", "", "",
    )


async def test_older_topic_is_recalled_without_promoting_it_to_current_scene(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    now = datetime.fromtimestamp(clock.now)
    thought = "之前聊过水彩，想问下次试什么颜色。"
    mgr, provider = attach(plugin, [
        stamp("user", "昨天说了这个", now - timedelta(hours=5)),
        stamp("user", "第一次画水彩，感觉挺有意思", now - timedelta(hours=3)),
        stamp("assistant", "那本画册也挺好看", now - timedelta(hours=3)),
    ], f"[[SEND:FOLLOW_UP]]\n[[REVIEW:RECALL/QUESTION|{thought}]]\n你还在床上看画册吗？\n想起之前聊的水彩，下次想试什么颜色呀？")
    monkeypatch.setattr(r, "_load_persona", AsyncMock(return_value="TEST_PERSONA"))
    dialogue = await r._proactive_input(umo, "cid", now)
    assert dialogue.outreach == "catch_up"
    assert dialogue.earlier_topics == ("第一次画水彩，感觉挺有意思",) and not dialogue.user_turns
    report = await r._fire(umo)
    assert report["sent"] and report["delivered_segments"] == 1
    provider.text_chat.assert_awaited_once()
    args = provider.text_chat.await_args.kwargs
    assert args["system_prompt"] == "TEST_PERSONA"
    assert "第一次画水彩" in args["prompt"]
    assert "昨天说了这个" not in args["prompt"] and not args["contexts"]
    sent = plugin.context.send_message.await_args.args[1].get_plain_text()
    saved = mgr.update_conversation.await_args.kwargs["history"][-1]["content"]
    assert "试什么颜色" in sent and "试什么颜色" in saved
    assert all(word not in sent and word not in saved for word in (thought, "REVIEW", "床上"))
    row = r.proactive_review.recent()[0]
    assert row["summary"] == thought and row["outline"] == "RECALL/QUESTION"
    assert row["checks"] == ["unconfirmed_catch_up"]
    assert ProactiveReview(r.proactive_review.path).recent()[0] == row


@pytest.mark.parametrize("user,registered,drop", [
    ("还没到时间，先歇会儿吧", False, True),
    ("中午十二点一起吃饭", False, False),
    ("明天中午十二点一起吃饭", False, True),
    ("晚饭还没定", False, True),
    ("还没到时间，先歇会儿吧", True, False),
    ("如果中午十二点一起吃饭", False, True),
])
def test_countdown_needs_actual_schedule(user, registered, drop):
    now = datetime(2026, 11, 2, 11, 55)
    draft = "先看会儿书。离饭点还差五分钟。忙完再聊也行。"
    appointments = [{"label":"吃饭","at":now.timestamp()+300,"stage":"pending"}] if registered else []
    text, reason = q.check_chat_timing(draft, [(user, now)], appointments, now)
    assert ("还差五分钟" not in text) is drop
    assert bool(reason) is drop
    assert "先看会儿书" in text and "忙完再聊也行" in text


@pytest.mark.parametrize("text", [
    "要是还差几分钟，先看看窗外呀。", "这题我还差几分钟能做完。", "她说『饭点快到了』。",
    "要不要一起吃饭呀？", "你还有三分钟到站吗？",
    "午饭一起吃吗？我做这题还差几分钟。",
])
def test_timing_guard_preserves_hypothetical_questions_and_unrelated_work(text):
    assert q.check_chat_timing(text, [("抱抱", None)], [], datetime(2026, 11, 2, 11, 55)) == (text, None)


async def test_ordinary_guard_cleans_visible_reply_and_saved_agent_message(plugin, umo, clock):
    now = datetime.fromtimestamp(clock.now)
    attach(plugin, [stamp("assistant", "我说好一会去吃饭", now)], "")
    bad = "先看会儿书。离饭点还差五分钟。"
    last = SimpleNamespace(role="assistant", tool_calls=[], content=[])
    reply = SimpleNamespace(role="assistant", completion_text=bad, result_chain=None)
    event = SimpleNamespace(unified_msg_origin=umo, message_str="还没到时间，先歇会儿吧")
    await plugin.guard_chat_reply(event, SimpleNamespace(messages=[last]), reply)
    assert "先看会儿书" in reply.completion_text
    assert "五分钟" not in reply.completion_text
    assert last.content[0].text == reply.completion_text


def test_review_does_not_persist_unexpected_model_or_message_fields(tmp_path):
    repo = ProactiveReview(tmp_path / "review.json")
    for i in range(53):
        repo.add({"at":i, "umo":"private", "model":"secret", "message":"private body", "checks":["draft_blocked", "secret"]})
    assert len(repo.recent(100)) == 50
    assert repo.recent()[0]["at"] == 52 and repo.recent()[0]["checks"] == ["draft_blocked"]
    assert "private body" not in repo.path.read_text(encoding="utf-8") and "secret" not in repo.path.read_text(encoding="utf-8")
