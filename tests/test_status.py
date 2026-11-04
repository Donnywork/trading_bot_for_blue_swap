import time
from decimal import Decimal

import pytest

import ledger
import status

NOW = 1_700_000_000
CHAIN = {
    "base": "fETH",
    "quote": "f564",
    "price": 1050.0,
    "vault_base": Decimal("0.5"),
    "vault_quote": Decimal("12.5"),
    "operator_eth": Decimal("0.05"),
}
RUNNING = ("running", "last tick 12s ago")


def held(entry=1000.0, base="0.00001", spent="0.01", opened=NOW - 600):
    return ledger.open_position(
        ledger.empty(), entry, Decimal(base), Decimal(spent), opened
    )


def text(*args):
    return "\n".join(status.render(*args))


def test_no_position_and_no_history():
    out = text(ledger.empty(), CHAIN, RUNNING, False, NOW)
    assert "LIVE" in out
    assert "position:  none" in out
    assert "no closed trades yet" in out
    assert "1050.000000 f564 per fETH" in out
    assert "0.5 fETH, 12.5 f564" in out


def test_open_position_shows_targets_and_unrealized_profit():
    out = text(held(), CHAIN, RUNNING, False, NOW)
    assert "OPEN for 10m" in out
    assert "take profit at 1030.000000 (+3%)" in out
    assert "stop loss at 960.000000 (-4%)" in out
    assert "(+5.00% from entry)" in out
    # 0.00001 fETH at 1050 is worth 0.0105 f564 for a cost of 0.01
    assert "unrealized +0.000500 f564 (+5.00%)" in out


def test_open_position_with_a_loss():
    chain = CHAIN | {"price": 900.0}
    out = text(held(), chain, RUNNING, False, NOW)
    assert "(-10.00% from entry)" in out
    assert "unrealized -0.001000 f564 (-10.00%)" in out


def test_realized_summary_and_recent_trades_newest_first():
    data = ledger.empty()
    for received, reason, exit_price in [
        ("0.0105", "take_profit", 1050.0),
        ("0.0097", "stop_loss", 970.0),
    ]:
        data = ledger.open_position(
            data, 1000.0, Decimal("0.00001"), Decimal("0.01"), 1
        )
        data = ledger.close_position(data, exit_price, Decimal(received), reason, NOW)
    out = text(data, CHAIN, RUNNING, False, NOW)
    assert "2 closed trades: 1 won, 1 lost (1 take profit, 1 stop loss)" in out
    assert "P/L +0.000200 f564 on 0.02 f564 spent (+1.00%)" in out
    recent = out[out.index("recent:") :].splitlines()
    assert "stop_loss" in recent[1] and "take_profit" in recent[2]


def test_only_the_latest_trades_are_listed():
    data = ledger.empty()
    for i in range(status.RECENT_TRADES + 3):
        data = ledger.open_position(
            data, 1000.0, Decimal("0.00001"), Decimal("0.01"), 1
        )
        data = ledger.close_position(
            data, 1000.0 + i, Decimal("0.01"), "take_profit", NOW
        )
    out = text(data, CHAIN, RUNNING, False, NOW)
    assert out.count("take_profit ") == status.RECENT_TRADES


def test_dry_run_is_labelled():
    assert "DRY RUN" in text(ledger.empty(), CHAIN, RUNNING, True, NOW)


def test_the_ledger_part_still_shows_when_the_chain_is_unreadable():
    out = text(held(), None, RUNNING, False, NOW)
    assert "unavailable" in out
    assert "OPEN for 10m" in out and "unrealized" not in out


# ---------------------------------------------------------------- bot health


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "HALT_FILE", tmp_path / "HALTED")
    monkeypatch.setattr(status, "HEARTBEAT_FILE", tmp_path / "heartbeat")
    return tmp_path


def test_health_halted_wins_and_shows_the_reason(files):
    (files / "HALTED").write_text("2026-11-04 10:00:00 3 failed trades in a row\n")
    (files / "heartbeat").write_text("x")
    kind, text_ = status.read_health(time.time())
    assert kind == "halted" and "3 failed trades" in text_


def test_health_running_stale_and_unknown(files):
    assert status.read_health(time.time())[0] == "unknown"
    (files / "heartbeat").write_text("x")
    now = time.time()
    assert status.read_health(now + 10)[0] == "running"
    assert status.read_health(now + status.STALE_AFTER_SECONDS + 60)[0] == "stale"


def test_cli_takes_no_arguments(monkeypatch):
    monkeypatch.setattr("sys.argv", ["status.py", "--live"])
    with pytest.raises(SystemExit, match="usage"):
        status.cli()
