import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_mitchell_minichat.core.proactive_quality import polish_reply


UNKNOWN = ("unknown", "", "", "")
BATH = ("together", "shower", "self", "active")
BATH_REPLY = ("浴缸的水快凉了，还不出来呀？\n再泡一会儿就太久了，起来吧。\n"
              "出来换件衣服，头发记得擦干喵。")
ARRIVAL = "八点多了，还没到吗\n早上风挺大的，记得系好围巾\n到了告诉我一声喔"
CONCERT = "还没到午休，先喝口水吧\n下周三有个音乐会，票都买好了\n届时你记得在后台等一下"


@pytest.mark.parametrize("body,scene,expected,reason", [
    (BATH_REPLY, UNKNOWN, "", "unconfirmed_bath"),
    (BATH_REPLY, ("together", "shower", "her", "active"), "", "unconfirmed_bath"),
    (BATH_REPLY, BATH, BATH_REPLY.splitlines()[0] + "\n" + BATH_REPLY.splitlines()[-1], "same_reply"),
    (ARRIVAL, UNKNOWN, "\n".join(ARRIVAL.splitlines()[:2]), "same_reply"),
    (CONCERT, UNKNOWN, CONCERT.splitlines()[0], "unsupported_plan"),
    ("晚安喵。\n被子盖好啦。\n晚安喵！", UNKNOWN, "晚安喵。\n被子盖好啦。", "same_reply"),
    ("今天风好大\n先找个暖和的地方\n我等你把故事讲完喵", UNKNOWN, None, None),
    ("好笨哦。\n下次还敢。\n嘿嘿", UNKNOWN, None, None),
    ("她念台词说『明天有个演唱会，票订好了』。\n这句读得真有气势！", UNKNOWN, None, None),
    ("洗澡的话别把水开太热喵\n刚回来就先歇一会", UNKNOWN, None, None),
    ("到学校了没？\n到家了记得说一声喵", UNKNOWN, None, None),
    ("走到哪啦？\n到了告诉我，东门今天锁着", UNKNOWN, None, None),
])
def test_reply_sentence_filter(body, scene, expected, reason):
    assert polish_reply(body, scene) == (body if expected is None else expected, {reason} if reason else set())


@pytest.mark.parametrize("confirmed,keep", [
    ("明天下午三点有个演唱会，演唱会的票买好了", True),
    ("明天下午三点去看演唱会", False),
    ("明天下午三点有个演唱会，票还没买", False),
    ("明天下午三点有个音乐会，票买好了", False),
    ("后天下午三点有个演唱会，票买好了", False),
    ("明天上午三点有个演唱会，票买好了", False),
    ("「明天下午三点有个演唱会，票买好了」", False),
])
def test_booking_requires_same_confirmed_booking(confirmed, keep):
    now = datetime(2026, 11, 17, 12)
    text = "明天下午三点有个演唱会，票都定好了"
    out, _ = polish_reply(text, UNKNOWN, [(confirmed, now)], now=now)
    assert out == (text if keep else "")


def test_registered_plan_grounds_date_and_time_but_not_purchased_ticket():
    now = datetime(2026, 11, 17, 12)
    plan = ({"at": (now + timedelta(days=1, hours=3)).timestamp(), "label": "演唱会"},)
    legitimate = "明天下午三点有个演唱会"
    assert polish_reply(legitimate, UNKNOWN, appointments=plan, now=now) == (legitimate, set())
    for text in ("后天下午三点有个演唱会", "明天下午四点有个演唱会",
                 "明天下午三点有个演唱会，票都定好了"):
        assert polish_reply(text, UNKNOWN, appointments=plan, now=now)[0] == ""
    assert polish_reply("到时候你要在后台等着喵", UNKNOWN, appointments=plan, now=now)[0] == ""


def test_relative_confirmation_uses_its_own_day():
    now = datetime(2026, 11, 17, 12)
    previous = now - timedelta(days=1)
    user = ("明天下午三点有个演唱会，演唱会的票买好了", previous)
    assert not polish_reply("明天下午三点有个演唱会，演唱会的票买好了", UNKNOWN, [user], now=now)[0]
    assert polish_reply("今天下午三点有个演唱会，票都定好了", UNKNOWN, [user], now=now)[0]


@pytest.mark.parametrize("text", ["来了来了", "要沉进去了喵", "快沉下去了！"])
async def test_context_only_turn_never_classifies_as_bath(plugin, umo, monkeypatch, text):
    r = plugin.runner
    recent = AsyncMock(return_value="她：水已经放好了")
    monkeypatch.setattr(r, "_recent_dialogue", recent)
    r.update_from_message(umo, text)
    assert r.take_llm_candidate(umo) is None
    await r.classify_with_llm(umo, text)
    recent.assert_not_awaited()
    assert plugin.store.get(umo, "active_event") is None


@pytest.mark.parametrize("newer", ["", "我正在洗澡", "我在吃饭"])
@pytest.mark.parametrize("split", [True, False])
async def test_clarification_only_clears_the_unconfirmed_bath(plugin, umo, clock, monkeypatch, newer, split):
    r = plugin.runner
    # Recreate already applied semantic state so the clarification path is
    # exercised independently of the new prefilter.
    monkeypatch.setattr(r, "classify_text", AsyncMock(return_value={"name": "shower", "done": False}))
    await r.classify_with_llm(umo, "要往里面去了")
    assert plugin.store.get(umo, "active_event")["unconfirmed_source"]
    if newer:
        r.update_from_message(umo, newer)
    if split:
        r.update_from_message(umo, "我的意思是")
        clock.now += 12
        r.update_from_message(umo, "要陷进抱抱里了")
    else:
        r.update_from_message(umo, "我的意思是，要陷进抱抱里了")
    event = plugin.store.get(umo, "active_event")
    assert (event or {}).get("name") == {"": None, "我正在洗澡": "shower", "我在吃饭": "eating"}[newer]
    assert r.take_llm_candidate(umo) is None


async def test_delayed_inference_cannot_cross_clarification(plugin, umo, monkeypatch):
    r = plugin.runner
    entered, finish = asyncio.Event(), asyncio.Event()

    async def classify(*args):
        entered.set()
        await finish.wait()
        return {"name": "shower", "done": False}

    monkeypatch.setattr(r, "classify_text", classify)
    pending = asyncio.create_task(r.classify_with_llm(umo, "要往里面去了"))
    await asyncio.wait_for(entered.wait(), 2)
    r.update_from_message(umo, "我说的是抱抱")
    finish.set()
    await asyncio.wait_for(pending, 2)
    assert r._proactive_scene(umo)[1] == ""


async def test_wrong_scene_skips_once_and_keeps_next_cycle(plugin, umo, monkeypatch):
    r = plugin.runner
    r.started = True
    generate = AsyncMock(return_value=BATH_REPLY)
    monkeypatch.setattr(r, "_generate", generate)
    finish = Mock()
    monkeypatch.setattr(r, "_finish_cycle", finish)
    result = await r._fire(umo)
    assert result["reason"] == "proactive_filtered"
    generate.assert_awaited_once()
    plugin.context.send_message.assert_not_awaited()
    assert not plugin.store.get(umo, "unanswered")
    finish.assert_called_once_with(umo)


@pytest.mark.parametrize("history_role", ["assistant", "user"])
async def test_filters_before_split_and_history(plugin, umo, clock, monkeypatch, history_role):
    r = plugin.runner
    clock.now = datetime(2026, 11, 17, 12).timestamp()
    r.cfg["segmented_reply_settings"].update(max_segments=3, min_length=0)
    text = "今天吃饭了没？\n明天下午三点有个演唱会，票都定好了"
    confirmed = "明天下午三点有个演唱会，演唱会的票买好了"
    monkeypatch.setattr(r, "_generate", AsyncMock(return_value=text))
    monkeypatch.setattr(r, "_load_history", AsyncMock(return_value=[{"role": history_role, "content": confirmed}]))
    saved = AsyncMock()
    monkeypatch.setattr(r, "_append_assistant_history", saved)
    result = await r._fire(umo)
    delivered = [call.args[1].get_plain_text() for call in plugin.context.send_message.await_args_list]
    expected = text.splitlines() if history_role == "user" else text.splitlines()[:1]
    assert result["sent"]
    assert delivered == expected
    assert saved.await_args.args[1] == "\n".join(expected)
    assert plugin.store.get(umo, "unanswered") == 1
