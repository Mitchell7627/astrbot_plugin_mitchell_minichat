import copy
import importlib.util
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "minichat_replay", ROOT / "tools/replay.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "fallback,count", [(False, 16), (True, 8)], ids=["configured-16", "fallback-8"]
)
async def test_replay_is_deterministic_and_never_connects(
    plugin, tmp_path, monkeypatch, fallback, count
):
    def blocked(*args, **kw):
        raise AssertionError("Replay must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    scenario = {
        "events": [
            {"at": "2026-11-02T10:00:00+00:00", "text": "我正在吃饭，十一点下课"}
        ],
        "until": "2026-11-02T12:00:00+00:00",
    }
    # Separate working directories guarantee no retained state between replays.
    one = tmp_path / "one"
    one.mkdir()
    monkeypatch.chdir(one)
    first = await module.replay(copy.deepcopy(scenario), fallback=fallback)
    two = tmp_path / "two"
    two.mkdir()
    monkeypatch.chdir(two)
    second = await module.replay(copy.deepcopy(scenario), fallback=fallback)
    assert first == second
    assert first["rules"] == count
    timers = [row for row in first["timeline"] if row["kind"] == "timer"]
    assert timers and any(row["prompts"] for row in timers)
    assert any(row["delivery"]["sent"] for row in timers)


@pytest.mark.parametrize(
    "document",
    [
        {"events": []},
        {"events": [{"at": "2026-11-02T10:00:00", "text": "hi"}]},
        {"events": [{"at": 200, "text": "hi"}, {"at": 100, "text": "hi"}]},
        {"events": [{"at": 200, "text": "hi"}], "until": 100},
    ],
)
async def test_replay_rejects_ambiguous_or_reversed_times(plugin, document):
    with pytest.raises(ValueError):
        await module.replay(document)
