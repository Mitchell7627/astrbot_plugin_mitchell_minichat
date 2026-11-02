import copy
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_mitchell_minichat.core.event_detector import EventRuleStore
from astrbot_plugin_mitchell_minichat.core.sleep_reminder import in_remind_window
from astrbot_plugin_mitchell_minichat.core.store import Store
from astrbot_plugin_mitchell_minichat.core.runner import Runner


@pytest.mark.parametrize(
    "fallback,count", [(False, 16), (True, 8)], ids=["configured-16", "fallback-8"]
)
def test_rule_sources(plugin, tmp_path, fallback, count):
    cfg = {} if fallback else plugin.engine_cfg
    engine = EventRuleStore(tmp_path, cfg)
    assert len(engine.rules_snapshot()) == count
    assert engine.match("我正在洗澡")["name"] == "shower"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我正在洗澡", "shower"),
        ("我没有在洗澡", None),
        ("你洗澡了吗", None),
        ("他说“我正在洗澡”", None),
        ("明天我去洗澡", None),
        ("如果下雨，我就去洗澡", None),
        ("我妈正在洗澡", None),
        ("听了朋友的话，我现在正在洗澡", "shower"),
        ("我现在不得不去坐地铁", "commute"),
    ],
)
def test_keyword_boundaries(plugin, text, expected):
    hit = plugin.engine.match(text)
    assert (hit["name"] if hit else None) == expected


@pytest.mark.parametrize("text", ["我到家了，我正在洗澡", "吃饱了，我正在洗澡"])
def test_compound_message(plugin, umo, clock, text):
    r = plugin.runner
    r.update_from_message(umo, "我正在吃饭")
    r.update_from_message(umo, text)
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


def test_away_end_return(plugin, umo, clock):
    r = plugin.runner
    for text in ["我去上课了", "都已经下课了"]:
        r.update_from_message(umo, text)
    assert plugin.store.get(umo, "away")["active"] is True
    r.update_from_message(umo, "我到家了")
    assert plugin.store.get(umo, "away")["active"] is False


def test_together_away_conflict(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我们一起牵手出门")
    assert r._together_alive(umo, clock.now)
    r.update_from_message(umo, "我去公司上班了")
    assert not r._together_alive(umo, clock.now)
    assert plugin.store.get(umo, "away")["active"]


def test_sleep_wake_and_questions(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "晚安，我睡了")
    assert r.is_sleeping(umo)
    for text in ["我妈醒了", "你醒了吗", "我还没起床"]:
        r.update_from_message(umo, text)
        assert r.is_sleeping(umo)
    r.update_from_message(umo, "我已经醒了")
    assert not r.is_sleeping(umo)


@pytest.mark.parametrize(
    "hour,minute,expected", [(23, 30, True), (0, 10, True), (1, 0, False)]
)
def test_midnight_window(hour, minute, expected):
    assert (
        in_remind_window("23:00", "00:20", datetime(2026, 11, 2, hour, minute))
        == expected
    )


def test_multiple_appointments_and_legacy_store(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我十一点下课")
    r.update_from_message(umo, "我十二点到家")
    items = r._appointments(umo)
    assert len(items) == 2
    assert len({a["id"] for a in items}) == 2
    plugin.store.save()
    restored = Store(plugin.data_dir)
    assert restored.get(umo, "appointments") == plugin.store.get(umo, "appointments")
    plugin.store = restored
    r = Runner(plugin)
    assert len(r._appointments(umo)) == 2
    legacy = r._appointments(umo)
    for item in legacy:
        item.pop("id", None)
    r._save_appointments(umo, legacy)
    plugin.store.save()
    assert all("id" not in x for x in r._appointments(umo))
    clock.now = legacy[0]["at"]
    selected = r.appointment_shot_item(umo)
    assert selected["id"]
    assert r._appointments(umo)[0]["id"] == selected["id"]


async def test_stale_llm_result(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我正在吃饭")
    seq = plugin.store.get(umo, "turn_seq")
    r.update_from_message(umo, "我正在洗澡")
    r.classify_text = AsyncMock(return_value={"name": "eating", "done": False})
    await r.classify_with_llm(umo, "我在吃饭", seq)
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


def test_undo_rejects_later_changes(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我正在吃饭")
    record = next(
        x for x in r.decisions.items(umo) if x.get("undo_state") == "available"
    )
    r.update_from_message(umo, "我正在洗澡")
    with pytest.raises(ValueError):
        r.undo_decision(record["id"])
    assert plugin.store.get(umo, "active_event")["name"] == "shower"


def test_tool_history_repair(plugin):
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "user", "content": "hi"},
    ]
    fixed = plugin.runner._repair_tool_calls(copy.deepcopy(history))
    assert fixed[1]["role"] == "tool" and fixed[1]["tool_call_id"] == "a"
    assert fixed[2]["role"] == "user"


async def test_sleep_defers_due_appointment(plugin, umo, clock):
    r = plugin.runner
    r.update_from_message(umo, "我十一点下课")
    r.update_from_message(umo, "我睡了")
    clock.now = datetime(2026, 11, 2, 11, 1).timestamp()
    wake = (r._sleep_state(umo) or {})["wake_at"]
    result = await r._fire(umo)
    assert result["reason"] == "sleeping"
    item = r._appointments(umo)[0]
    assert item["deferred"] and item["at"] == wake + 60
    assert any(
        x["reason"] == "appointment_deferred" for x in r.schedule_diagnostics(umo)
    )


@pytest.mark.parametrize(
    "text,name",
    [
        ("我正在约会", "date"),
        ("我正在吃饭", "eating"),
        ("我正在做饭", "cooking"),
        ("我正在喝奶茶", "drinking"),
        ("我正在坐地铁", "commute"),
        ("我正在开会", "meeting"),
        ("我正在写代码", "working"),
        ("我正在洗澡", "shower"),
        ("我正在打游戏", "gaming"),
        ("我正在洗衣服", "errand"),
        ("我正在学习", "study"),
        ("我正在看电影", "movie"),
        ("我正在逛街", "shopping"),
        ("我正在看病", "hosp"),
        ("我正在运动", "exercise"),
        ("我正在午睡", "nap"),
    ],
)
def test_all_configured_event_categories(plugin, text, name):
    assert plugin.engine.match(text)["name"] == name
