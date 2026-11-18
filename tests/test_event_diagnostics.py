import pytest

from event_diagnostics import entry_status

NOW = 1000


def ready(**changes):
    return {
        "updated": NOW,
        "snapshot_time": NOW - 12,
        "health": "ready",
        "connected": True,
        "mode": "live",
        "mode_done": 10,
        "start_floor": 10,
        "controls": {
            "event_paused": False,
            "event_mode_request": 10,
            "event_start_request": 11,
            "event_backrun": True,
        },
        **changes,
    }


def codes(value):
    return {b["code"] for b in entry_status(value, NOW)["blockers"]}


def test_mode_change_requires_new_start_even_when_feed_and_snapshot_ready():
    value = ready()
    value["controls"].update(event_paused=True, event_start_request=9)
    result = entry_status(value, NOW)
    assert codes(value) == {"paused"}
    assert "Start Trading" in result["summary"]
    # Old runtime state has no start_floor: do not claim its older Start was applied.
    del value["start_floor"]
    value["controls"]["event_paused"] = False
    assert "paused" in codes(value)
    value["controls"]["event_start_request"] = 11
    assert not codes(value)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"updated": NOW - 31}, "stale_status"),
        ({"snapshot_time": NOW - 31}, "snapshot"),
        ({"connected": False}, "feed"),
        ({"feed": {"gap": True}}, "feed"),
        ({"health": "awaiting_scan"}, "awaiting_scan"),
        ({"position": {"base_raw": 1}}, "position"),
        ({"intent_pending": True}, "intent"),
        ({"cooldown_until": NOW + 10}, "cooldown"),
        ({"mode_block": "Vault limits conflict"}, "mode_block"),
    ],
)
def test_distinct_blockers(changes, code):
    assert code in codes(ready(**changes))


def test_modes_describe_execution_and_no_opportunity_is_not_a_fault():
    decision = {
        "kind": "mined",
        "status": "no_opportunity",
        "reason": "profit floor not met",
    }
    for mode in ("paper", "simulate", "live"):
        result = entry_status(ready(mode=mode, decisions=[decision]), NOW)
        assert result["blockers"] == []
        assert result["last_evaluation"] == decision
        assert result["mode_note"]


def test_invalid_controls_and_startup_failure_are_explainable():
    result = entry_status(
        {
            "updated": NOW,
            "health": "failed",
            "mode_done": None,
            "start_floor": None,
            "control_error": "Invalid controls",
        },
        NOW,
    )
    assert {"failed", "control_error", "paused"} <= {
        b["code"] for b in result["blockers"]
    }
