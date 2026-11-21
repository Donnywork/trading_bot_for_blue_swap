"""Watchdog alerting (watchdog.py): watch and alert, never touch anything."""

import pytest

import watchdog


class _Stop(Exception):
    """Raised by the fake sleep to end the run once all states are consumed."""


@pytest.fixture
def settings():
    return {"interval": 1, "stale": 90, "webhook": "http://example.invalid/hook"}


def drive(states, settings, monkeypatch):
    # states: consecutive check results replayed through the run loop.
    # Each entry is a state dict, or None for an unreadable state file.
    # Returns (emitted lines, webhook posts).
    lines, posts, remaining = [], [], list(states)

    def fake_read_state(path, now):
        if not remaining:
            raise _Stop
        state = remaining.pop(0)
        if state is None:
            return None, "state unreadable: OSError"
        return state, None

    def fake_sleep(seconds):
        if not remaining:
            raise _Stop

    monkeypatch.setattr(watchdog, "read_state", fake_read_state)
    try:
        watchdog.run(
            settings,
            now=lambda: 1000,
            sleep=fake_sleep,
            read_state=fake_read_state,
            emit=lines.append,
            post=lambda url, body: posts.append((url, body)),
        )
    except _Stop:
        pass
    return lines, posts


def test_healthy_start_is_quiet(settings, monkeypatch):
    state = {"updated": 1000, "health": "ready", "message": "ok"}
    lines, posts = drive([state, state], settings, monkeypatch)
    assert lines == ["healthy (health=ready)"]
    assert posts == []


def test_halt_alerts_once_and_recovery_alerts(settings, monkeypatch):
    ready = {"updated": 1000, "health": "ready", "message": "ok"}
    halted = {
        "updated": 1000,
        "health": "halted",
        "message": "Execution halted. reconcile",
    }
    lines, posts = drive([ready, halted, halted, ready], settings, monkeypatch)
    assert "[critical] health=halted: Execution halted. reconcile" in lines[1]
    assert lines.count(lines[1]) == 1  # deduplicated while unchanged
    assert "[recovered] health=ready" in lines[2]
    assert len(posts) == 2  # the halt and the recovery; not the repeat


def test_stale_state_alerts_once_despite_changing_age(settings, monkeypatch):
    # The stale age worsens every check; the alert key must not change.
    fresh = {"updated": 1000, "health": "ready"}
    stale_states = [
        {"updated": updated, "health": "ready"} for updated in (900, 850, 800, 700)
    ]
    lines, posts = drive([fresh] + stale_states, settings, monkeypatch)
    stale_lines = [line for line in lines if "stale" in line]
    assert len(stale_lines) == 1
    assert len(posts) == 1


def test_awaiting_scan_is_a_warning(settings, monkeypatch):
    state = {"updated": 1000, "health": "awaiting_scan", "message": "needs scan"}
    lines, posts = drive([state, state], settings, monkeypatch)
    assert any("[warning] awaiting_scan" in line for line in lines)
    assert len(posts) == 1


def test_missing_state_file_is_critical(settings, monkeypatch):
    lines, posts = drive([None, None], settings, monkeypatch)
    assert any("[critical] state unreadable" in line for line in lines)
    assert len(posts) == 1


def test_scanning_is_healthy(settings, monkeypatch):
    state = {"updated": 1000, "health": "scanning", "message": ""}
    lines, posts = drive([state], settings, monkeypatch)
    assert lines == ["healthy (health=scanning)"]
    assert posts == []


def test_webhook_failure_never_stops_the_watchdog(settings, monkeypatch):
    halted = {
        "updated": 1000,
        "health": "halted",
        "message": "Execution halted. reconcile",
    }
    lines = []

    def failing_post(url, body):
        raise OSError("connection refused")

    monkeypatch.setattr(watchdog, "read_state", lambda path, now: (halted, None))
    with pytest.raises(_Stop):
        watchdog.run(
            settings,
            now=lambda: 1000,
            sleep=lambda seconds: (_ for _ in ()).throw(_Stop()),
            read_state=lambda path, now: (halted, None),
            emit=lines.append,
            post=failing_post,
        )
    assert any("webhook delivery failed" in line for line in lines)


def test_config_parses_and_rejects_garbage():
    assert watchdog.config({}) == {"interval": 30, "stale": 90, "webhook": ""}
    tuned = watchdog.config(
        {
            "WATCHDOG_SECONDS": "15",
            "WATCHDOG_STALE_SECONDS": "60",
            "WATCHDOG_WEBHOOK_URL": " https://hook ",
        }
    )
    assert tuned == {"interval": 15, "stale": 60, "webhook": "https://hook"}
    with pytest.raises(SystemExit):
        watchdog.config({"WATCHDOG_SECONDS": "fast"})
    with pytest.raises(SystemExit):
        watchdog.config({"WATCHDOG_SECONDS": "1"})


def test_watchdog_has_no_restart_or_write_surface():
    # The watchdog may only look and speak: nothing in the module writes
    # control files, clears halt markers or spawns processes.
    source = (watchdog.ROOT / "watchdog.py").read_text()
    for forbidden in ("control.save", "HALTED", "subprocess", "os.system", "Popen"):
        assert forbidden not in source
