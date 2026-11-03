"""Cancellation intent, full-clause targets, scope and undo contracts."""
import copy

import pytest


def register(plugin, umo):
    plugin.runner.update_from_message(umo, "我下午三点开会")
    return copy.deepcopy(plugin.runner._appointments(umo))


@pytest.mark.parametrize("item", ["理发预约", "牙医预约", "体检预约"])
@pytest.mark.parametrize("pattern", ["{item}下午三点取消了", "下午三点的{item}取消了", "取消下午三点的{item}"])
def test_unknown_item_order_preserves_meeting(plugin, umo, clock, item, pattern):
    before = register(plugin, umo)
    plugin.runner.update_from_message(umo, pattern.format(item=item))
    assert plugin.runner._appointments(umo) == before


@pytest.mark.parametrize("text", [
    "没有要取消下午三点的会议", "不是说要取消下午三点的会议",
    "下午三点的会议取消失败了", "下午三点的会议取消不了",
    "下午三点的会议没取消成功", "下午三点的会议取消没有成功",
    "下午三点的会议取消了但没成功", "不要取消所有约定",
    "我可能取消下午三点的会议", "我考虑取消下午三点的会议",
    "我准备取消下午三点的会议", "如果下午三点的会议取消了",
    "他说下午三点的会议取消了", "我妈下午三点的会议取消了",
    "下午三点的会议取消了吗", "不是不取消下午三点的会议",
    "不是没取消下午三点的会议", "没有要取消下午三点的所有约定",
])
def test_unconfirmed_action_preserves_state_and_has_no_success(plugin, umo, clock, text):
    before = register(plugin, umo)
    plugin.runner.update_from_message(umo, text)
    assert plugin.runner._appointments(umo) == before
    assert not any(x["rule"] == "CANCEL" for x in plugin.runner.decisions.items(umo))


@pytest.mark.parametrize("text", [
    "下午三点的这个约定取消了", "下午三点的那个安排取消了",
    "下午三点的这条提醒取消了", "下午三点的所有约定取消了",
    "下午三点的会议取消成功了", "我决定取消下午三点的会议",
    "取消下午三点的会议", "会议下午三点取消了",
    "这个约定取消了", "那个安排取消了", "这条提醒取消了",
    "我不去开会了", "我下午三点不开会了",
])
def test_unique_target_cancel_positive(plugin, umo, clock, text):
    register(plugin, umo)
    plugin.runner.update_from_message(umo, text)
    assert not plugin.runner._appointments(umo)
    record = next(x for x in plugin.runner.decisions.items(umo) if x["rule"] == "CANCEL")
    assert record["action"] == "applied" and record["undo_state"] == "available"


def seed_multiple(plugin, umo, clock, same_clock=False):
    r = plugin.runner
    for text in ["我下午三点开会", "我下午四点下课", "我下午五点开会"]:
        r.update_from_message(umo, text)
    items = r._appointments(umo)
    if same_clock:
        items[1].update(at=items[0]["at"], time_text="15:00")
        r._save_appointments(umo, items)
    return copy.deepcopy(r._appointments(umo))


@pytest.mark.parametrize("text,expected", [
    ("下午三点的那个安排取消了", ["16:00", "17:00"]),
    ("下午三点的所有约定取消了", ["16:00", "17:00"]),
    ("所有约定取消了", []),
    ("所有会议取消了", ["16:00"]),
    ("会议取消了", ["15:00", "16:00", "17:00"]),
    ("那个安排取消了", ["15:00", "16:00", "17:00"]),
])
def test_scope_multiple_items(plugin, umo, clock, text, expected):
    seed_multiple(plugin, umo, clock)
    plugin.runner.update_from_message(umo, text)
    assert [x["time_text"] for x in plugin.runner._appointments(umo)] == expected


@pytest.mark.parametrize("text,remaining", [
    ("下午三点的那个安排取消了", 3),
    ("下午三点的所有约定取消了", 1),
    ("下午三点的会议取消了", 2),
])
def test_shared_clock_requires_unique_or_explicit_all(plugin, umo, clock, text, remaining):
    seed_multiple(plugin, umo, clock, same_clock=True)
    plugin.runner.update_from_message(umo, text)
    assert len(plugin.runner._appointments(umo)) == remaining


@pytest.mark.parametrize("stale", [False, True])
def test_cancel_snapshot_undo_and_stale_rejection(plugin, umo, clock, stale):
    r = plugin.runner
    before = seed_multiple(plugin, umo, clock)
    r.update_from_message(umo, "下午三点的会议取消了")
    rec = next(x for x in r.decisions.items(umo) if x["rule"] == "CANCEL")
    assert rec["action"] == "applied" and rec["touched"]
    if stale:
        clock.now += 10
        r.update_from_message(umo, "我下午六点下课")
        newer = copy.deepcopy(r._appointments(umo))
        with pytest.raises(ValueError):
            r.undo_decision(rec["id"])
        assert r._appointments(umo) == newer
    else:
        r.undo_decision(rec["id"])
        assert r._appointments(umo) == before


def test_completed_records_are_preserved(plugin, umo, clock):
    r = plugin.runner
    seed_multiple(plugin, umo, clock)
    items = r._appointments(umo)
    items[0]["stage"] = "done"
    r._save_appointments(umo, items)
    r.update_from_message(umo, "所有约定取消了")
    assert r._appointments(umo) == [items[0]]


def test_unrelated_negation_and_independent_clause(plugin, umo, clock):
    register(plugin, umo)
    plugin.runner.update_from_message(umo, "我不去理发，下午三点的会议取消了，我正在吃饭")
    assert not plugin.runner._appointments(umo)
    assert plugin.store.get(umo, "active_event")["name"] == "eating"
