import json
from types import SimpleNamespace

import pytest

import bot_contract
import ledger
import legacy_main as main
import operator_lock


@pytest.mark.parametrize("service", ["legacy", "events", "manual"])
@pytest.mark.parametrize("marker", ["HALTED", "events/HALTED"])
def test_every_live_sender_respects_both_halt_markers(tmp_path, service, marker):
    path = tmp_path / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("unresolved send")
    with (
        pytest.raises(operator_lock.SendBlocked, match="Reconcile"),
        operator_lock.sender(tmp_path, service),
    ):
        pytest.fail("sender started despite halt")
    # Rejection releases the lock, without clearing the marker.
    with operator_lock.acquire(tmp_path / "operator.lock"):
        assert path.read_text() == "unresolved send"


@pytest.mark.parametrize(
    "path,data,allowed",
    [
        ("ledger.json", {"open": {"base": "1"}}, "legacy"),
        ("events/execution_live.json", {"position": {}, "intent": None}, "events"),
        ("events/execution_live.json", {"position": None, "intent": {}}, "events"),
    ],
)
def test_service_can_resume_own_state_but_other_senders_are_blocked(
    tmp_path, path, data, allowed
):
    record = tmp_path / path
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(data))
    for service in ("legacy", "events", "manual"):
        if service == allowed:
            # The service's own ledger loader still validates/reconciles this state.
            with operator_lock.sender(tmp_path, service):
                pass
        else:
            with (
                pytest.raises(operator_lock.SendBlocked, match="inventory"),
                operator_lock.sender(tmp_path, service),
            ):
                pytest.fail("occupied ledger accepted")


@pytest.mark.parametrize("data", ["broken", "{}", "null", "[]"])
def test_unreadable_other_ledger_blocks_sender(tmp_path, data):
    (tmp_path / "ledger.json").write_text(data)
    with (
        pytest.raises(operator_lock.SendBlocked, match="cannot verify"),
        operator_lock.sender(tmp_path, "events"),
    ):
        pytest.fail("unreadable ledger accepted")


def test_sender_excludes_manual_commands_and_releases_after_failure(tmp_path):
    with pytest.raises(OSError), operator_lock.sender(tmp_path, "events"):
        with (
            pytest.raises(operator_lock.SendBlocked, match="lock"),
            operator_lock.sender(tmp_path, "manual"),
        ):
            pytest.fail("concurrent sender")
        raise OSError("test failure")
    with operator_lock.sender(tmp_path, "manual"):
        pass


def test_non_live_bypasses_live_lock_and_ledgers(tmp_path):
    (tmp_path / "HALTED").write_text("unresolved")
    (tmp_path / "ledger.json").write_text("broken")
    with (
        operator_lock.acquire(tmp_path / "operator.lock"),
        operator_lock.sender(tmp_path, "events", live=False),
    ):
        pass


@pytest.mark.parametrize("command", ["deploy", "swap"])
def test_manual_entrypoints_check_sender_before_execution(
    tmp_path, monkeypatch, command
):
    monkeypatch.setattr(ledger, "LOG_DIR", tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(
        bot_contract, "connect", lambda: pytest.fail("connected before sender check")
    )
    (tmp_path / "HALTED").write_text("unresolved")
    with pytest.raises(operator_lock.SendBlocked):
        if command == "deploy":
            bot_contract.cmd_deploy()
        else:
            bot_contract.cmd_swap("f564", "1")


def test_legacy_loads_execution_state_with_lock_held(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOG_DIR", tmp_path)
    monkeypatch.setattr(main, "HALT_FILE", tmp_path / "HALTED")
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(
        main.Settings, "from_env", lambda: SimpleNamespace(dry_run=False)
    )
    calls = []

    def run(settings, ticks):
        with (
            pytest.raises(operator_lock.SendBlocked, match="lock"),
            operator_lock.sender(tmp_path, "manual"),
        ):
            pytest.fail("state loaded before lock")
        calls.append(ticks)

    monkeypatch.setattr(main, "run_configured", run)
    main.main(ticks=1)
    assert calls == [1]
