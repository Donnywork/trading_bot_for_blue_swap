import os
import threading
from decimal import Decimal

import pytest
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

import main as bot_main
import strategy
from strategy import Position

FLAT = [100.0] * 30
RICH = Position(base=1.0, quote=1.0)


# ---------------------------------------------------------------- strategy


def test_strategy_holds_without_enough_history():
    assert strategy.decide([100.0] * (strategy.WINDOW - 1), RICH) == "hold"


def test_strategy_holds_on_a_flat_market():
    assert strategy.decide(FLAT, RICH) == "hold"


def test_strategy_buys_a_dip_and_sells_a_spike():
    assert strategy.decide(FLAT[:-1] + [90.0], RICH) == "buy"
    assert strategy.decide(FLAT[:-1] + [110.0], RICH) == "sell"


def test_strategy_small_moves_stay_inside_the_fee_band():
    assert strategy.decide(FLAT[:-1] + [100.5], RICH) == "hold"
    assert strategy.decide(FLAT[:-1] + [99.5], RICH) == "hold"


def test_strategy_cannot_trade_what_it_does_not_hold():
    assert strategy.decide(FLAT[:-1] + [90.0], Position(base=1.0, quote=0.0)) == "hold"
    assert strategy.decide(FLAT[:-1] + [110.0], Position(base=0.0, quote=1.0)) == "hold"


# ---------------------------------------------------------------- runner


class FakeIO:
    def __init__(self, prices, position=RICH, eth=Decimal(1)):
        self.prices = list(prices)
        self._position = position
        self.eth = eth
        self.trades = []
        self.trade_error = None
        self.read_error = None

    def read_price(self):
        if self.read_error:
            raise self.read_error
        return self.prices.pop(0) if len(self.prices) > 1 else self.prices[0]

    def position(self):
        return self._position

    def operator_eth(self):
        return self.eth

    def trade(self, decision, amount):
        if self.trade_error:
            raise self.trade_error
        self.trades.append((decision, amount))


def settings(**kw):
    base = {
        "poll_seconds": 30,
        "cooldown_seconds": 300,
        "min_operator_eth": Decimal("0.01"),
        "max_trade_failures": 3,
        "max_trade": Decimal("0.01"),
        "dry_run": False,
    }
    return bot_main.Settings(**(base | kw))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path):
    def build(io, **kw):
        clock = Clock()
        runner = bot_main.Runner(
            io,
            settings(**kw),
            threading.Event(),
            clock=clock,
            halt_file=tmp_path / "logs" / "HALTED",
            heartbeat_file=tmp_path / "logs" / "heartbeat",
        )
        runner.test_clock = clock
        return runner

    return build


def warm_up(runner, io):
    # feed enough flat history that the next price is judged against a full window
    for _ in range(strategy.WINDOW):
        runner.step()


def test_holds_do_not_trade(make):
    io = FakeIO([100.0])
    runner = make(io)
    for _ in range(30):
        runner.step()
    assert io.trades == []


def test_trades_on_a_signal_with_the_configured_size(make):
    io = FakeIO([100.0])
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == [("buy", Decimal("0.01"))]


def test_cooldown_blocks_a_second_trade_until_it_expires(make):
    io = FakeIO([100.0])
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    runner.step()
    assert len(io.trades) == 1
    runner.test_clock.now += 301
    runner.step()
    assert len(io.trades) == 2


def test_skips_when_vault_balance_is_below_trade_size(make):
    io = FakeIO([100.0], position=Position(base=1.0, quote=0.005))
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == []


def test_skips_when_operator_eth_is_low(make):
    io = FakeIO([100.0], eth=Decimal("0.001"))
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == []


def test_read_errors_back_off_but_never_halt(make):
    io = FakeIO([100.0])
    io.read_error = ConnectionError("rpc down")
    runner = make(io)
    for _ in range(50):
        runner.step()
    assert runner.read_failures == 50
    assert not runner.halt_file.exists()
    io.read_error = None
    runner.step()
    assert runner.read_failures == 0


def test_repeated_trade_failures_halt(make):
    io = FakeIO([100.0])
    runner = make(io, cooldown_seconds=0)
    warm_up(runner, io)
    io.prices = [90.0]
    io.trade_error = ContractLogicError("STF")
    runner.step()
    runner.step()
    with pytest.raises(bot_main.Halt):
        runner.step()


def test_a_success_resets_the_failure_count(make):
    io = FakeIO([100.0])
    runner = make(io, cooldown_seconds=0)
    warm_up(runner, io)
    io.prices = [90.0]
    io.trade_error = Web3RPCError("boom")
    runner.step()
    runner.step()
    io.trade_error = None
    runner.step()
    assert runner.trade_failures == 0


def test_receipt_timeout_halts_immediately(make):
    io = FakeIO([100.0])
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    io.trade_error = TimeExhausted("no receipt")
    with pytest.raises(bot_main.Halt, match="may still land"):
        runner.step()


def test_connection_error_while_sending_halts_but_not_in_dry_run(make):
    live = FakeIO([100.0])
    runner = make(live)
    warm_up(runner, live)
    live.prices = [90.0]
    live.trade_error = ConnectionError("dropped")
    with pytest.raises(bot_main.Halt, match="may have been sent"):
        runner.step()

    dry = FakeIO([100.0])
    runner = make(dry, dry_run=True)
    warm_up(runner, dry)
    dry.prices = [90.0]
    dry.trade_error = ConnectionError("dropped")
    runner.step()  # counted as a failure, no halt
    assert runner.trade_failures == 1


def test_skip_is_not_a_failure(make):
    io = FakeIO([100.0])
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    io.trade_error = bot_main.Skip("price impact too large")
    runner.step()
    assert runner.trade_failures == 0


def test_run_writes_halt_file_and_stops(make):
    io = FakeIO([100.0])
    runner = make(io, cooldown_seconds=0, max_trade_failures=1, poll_seconds=0)
    warm_up(runner, io)
    io.prices = [90.0]
    io.trade_error = ContractLogicError("STF")
    runner.run()
    assert "failed trades" in runner.halt_file.read_text()


def test_run_writes_heartbeat_and_respects_stop(make):
    io = FakeIO([100.0])
    runner = make(io, poll_seconds=0)
    runner.run(max_steps=2)
    assert runner.heartbeat_file.exists()

    runner.stop.set()
    runner.run()  # returns immediately
    assert io.prices == [100.0]


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MAX_TRADE_AMOUNT", "0.5")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("POLL_SECONDS", "45")
    s = bot_main.Settings.from_env()
    assert (s.max_trade, s.dry_run, s.poll_seconds) == (Decimal("0.5"), False, 45)
    monkeypatch.setenv("DRY_RUN", "maybe")
    assert (
        bot_main.Settings.from_env().dry_run is True
    )  # anything but "false" is a dry run


def test_redact_hides_the_rpc_key_and_private_key(monkeypatch):
    monkeypatch.setenv(
        "RPC_URL", "https://sepolia.infura.io/v3/0123456789abcdef0123456789abcdef"
    )
    monkeypatch.setenv("PRIVATE_KEY", "ab" * 32)
    err = (
        "429 Client Error for url: https://sepolia.infura.io/v3/0123456789abcdef0123456789abcdef"
        f" key={'ab' * 32}"
    )
    out = bot_main.redact(err)
    assert "0123456789abcdef" not in out and "abab" not in out
    assert "429 Client Error" in out  # the useful part survives


def test_redact_leaves_text_alone_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("RPC_URL", raising=False)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    assert bot_main.redact("plain message") == "plain message"


def test_logging_and_halt_file_are_redacted(make, monkeypatch, caplog):
    monkeypatch.setenv(
        "RPC_URL", "https://sepolia.infura.io/v3/0123456789abcdef0123456789abcdef"
    )
    secret_error = ConnectionError("failed for url: " + os.environ["RPC_URL"])

    formatter = bot_main.RedactingFormatter("%(message)s")
    record = bot_main.log.makeRecord(
        "bot", 40, __file__, 1, "read failed: %s", (secret_error,), None
    )
    assert "0123456789abcdef" not in formatter.format(record)

    runner = make(FakeIO([100.0]))
    runner.halt(f"boom {secret_error}")
    assert "0123456789abcdef" not in runner.halt_file.read_text()


def test_cli_logs_a_redacted_crash_and_exits_nonzero(monkeypatch, caplog):
    monkeypatch.setenv(
        "RPC_URL", "https://sepolia.infura.io/v3/0123456789abcdef0123456789abcdef"
    )

    def boom():
        raise RuntimeError("failed for " + os.environ["RPC_URL"])

    monkeypatch.setattr(bot_main, "main", lambda ticks=None: boom())
    monkeypatch.setattr("sys.argv", ["main.py"])
    with pytest.raises(SystemExit) as exit_info:
        bot_main.cli()
    assert exit_info.value.code == 1
    assert any(
        r.levelname == "ERROR" and "crashed" in r.message for r in caplog.records
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [([], None), (["--ticks", "3"], 3)],
)
def test_cli_passes_the_tick_limit(monkeypatch, argv, expected):
    seen = []
    monkeypatch.setattr(bot_main, "main", lambda ticks=None: seen.append(ticks))
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    bot_main.cli()
    assert seen == [expected]


@pytest.mark.parametrize(
    "argv", [["--ticks"], ["--ticks", "0"], ["--ticks", "x"], ["--bogus"]]
)
def test_cli_rejects_bad_arguments(monkeypatch, argv):
    monkeypatch.setattr(bot_main, "main", lambda ticks=None: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    with pytest.raises(SystemExit, match="usage"):
        bot_main.cli()
