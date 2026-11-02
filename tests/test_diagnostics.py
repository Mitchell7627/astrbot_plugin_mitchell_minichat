import copy
from unittest.mock import AsyncMock


async def test_schedule_trace_priority_and_bounds(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    r.started = True
    monkeypatch.setattr(r, "_sample_minutes", lambda *a, **kw: 120)
    monkeypatch.setattr(r, "_event_remaining_minutes", lambda u: None)
    monkeypatch.setattr(r, "_sleep_reminder_delay_minutes", lambda u: 60)
    monkeypatch.setattr(r, "_appointment_next_delay", lambda u, m: 10)
    r.reschedule(umo)
    trace = r.schedule_diagnostics(umo)[-1]
    assert [s["reason"] for s in trace["steps"]] == [
        "sample",
        "sleep_alignment",
        "appointment",
    ]
    assert trace["next_at"] == clock.now + 600
    assert plugin.store.get(umo, "next_at") == trace["next_at"]
    for i in range(30):
        r.reschedule(umo, delay_minutes=5)
    assert len(r.schedule_diagnostics(umo)) == 20
    before = copy.deepcopy(plugin.store.data)
    monkeypatch.setattr(
        r, "_generate", AsyncMock(side_effect=AssertionError("network call"))
    )
    log = r.schedule_diagnostics(umo)
    log[0]["reason"] = "changed"
    assert r.schedule_diagnostics(umo)[0]["reason"] != "changed"
    assert plugin.panel_service.status()["schedule_diagnostics"][umo]
    assert plugin.panel_service.tasks()["sessions"][0]["schedule_diagnostics"]
    assert plugin.store.data == before
    await r.stop()


async def test_restore_clamps_event_and_stopped_reason(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    r.started = True
    plugin.store.set(umo, "next_at", clock.now + 7200)
    monkeypatch.setattr(r, "_event_remaining_minutes", lambda u: 20)
    monkeypatch.setattr(r, "_appointment_next_delay", lambda *a: None)
    r.reschedule(umo, keep_if_future=True)
    assert r.schedule_diagnostics(umo)[-1]["reason"] == "event_window"
    assert plugin.store.get(umo, "next_at") == clock.now + 19.5 * 60
    await r.stop()
    r.reschedule(umo)
    assert r.schedule_diagnostics(umo)[-1]["reason"] == "stopped"


async def test_unanswered_pause_is_explained(plugin, umo, clock, monkeypatch):
    r = plugin.runner
    plugin.store.set(umo, "unanswered", 3)
    monkeypatch.setattr(r, "should_remind_sleep", lambda u: False)
    monkeypatch.setattr(r, "_next_wake_minutes", lambda u: None)
    result = await r._fire(umo)
    assert result["reason"] == "unanswered_limit" and not result["sent"]
    assert r.schedule_diagnostics(umo)[-1]["reason"] == "unanswered_limit"


async def test_reply_and_event_restore_have_final_target(
    plugin, umo, clock, monkeypatch
):
    r = plugin.runner
    r.started = True
    monkeypatch.setattr(r, "_appointment_next_delay", lambda *a: None)
    r.note_bot_reply(umo)
    assert r.schedule_diagnostics(umo)[-1]["reason"] == "bot_reply_gap"
    plugin.store.set(umo, "pre_event_next_at", clock.now + 7200)
    plugin.store.set(umo, "event_hit", True)
    r._decide_after_event(umo, clock.now)
    last = r.schedule_diagnostics(umo)[-1]
    assert last["reason"] == "event_restore"
    assert last["next_at"] == plugin.store.get(umo, "next_at") == clock.now + 7200
    await r.stop()
