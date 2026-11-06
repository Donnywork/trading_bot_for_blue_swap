import logging
import os
import threading
import time
from decimal import Decimal

import pytest
from web3.exceptions import (
    BadFunctionCallOutput,
    ContractLogicError,
    TimeExhausted,
    Web3RPCError,
)

import config
import control
import history
import ledger
import main as bot_main
import state
import strategy
import v3pool
from strategy import Position

FLAT = [100.0] * 30
RICH = Position(base=1.0, quote=1.0)


# ---------------------------------------------------------------- strategy


def test_strategy_holds_without_enough_history():
    assert strategy.decide([100.0] * (strategy.WINDOW - 1), RICH) == "hold"


NO_GUARD = strategy.Config(guard_flip_pct=0, guard_drift_pct=0)


def test_strategy_holds_on_a_flat_market():
    assert strategy.decide(FLAT, RICH, NO_GUARD) == "hold"


def test_strategy_buys_a_dip_but_never_sells_a_spike_on_its_own():
    assert strategy.decide(FLAT[:-1] + [90.0], RICH, NO_GUARD) == "buy"
    # base the bot did not buy has no entry price, so there is nothing to take profit on
    assert strategy.decide(FLAT[:-1] + [110.0], RICH, NO_GUARD) == "hold"


def test_strategy_small_moves_stay_inside_the_fee_band():
    assert strategy.decide(FLAT[:-1] + [100.5], RICH, NO_GUARD) == "hold"
    assert strategy.decide(FLAT[:-1] + [99.5], RICH, NO_GUARD) == "hold"


def test_strategy_cannot_buy_without_quote():
    no_quote = Position(base=1.0, quote=0.0)
    assert strategy.decide(FLAT[:-1] + [90.0], no_quote, NO_GUARD) == "hold"


HELD = Position(base=1.0, quote=1.0, entry_price=100.0)


def test_strategy_takes_profit_and_stops_loss_from_the_entry_price():
    assert strategy.decide([102.5], HELD) == "sell"
    assert strategy.decide([102.4], HELD) == "hold"
    assert strategy.decide([96.0], HELD) == "sell"
    assert strategy.decide([96.1], HELD) == "hold"


def test_strategy_exits_work_without_a_full_price_history():
    # right after a restart there are no samples, but a stop loss must still fire
    assert strategy.decide([90.0], HELD) == "sell"


def test_strategy_does_not_buy_more_while_holding():
    assert (
        strategy.decide(FLAT[:-1] + [90.0], HELD) == "sell"
    )  # below the stop loss: exit, not buy
    assert (
        strategy.decide(FLAT[:-1] + [98.0], HELD) == "hold"
    )  # a dip, but inside the band


def test_exit_reason():
    assert strategy.exit_reason(105.0, 100.0) == "take_profit"
    assert strategy.exit_reason(96.0, 100.0) == "stop_loss"
    assert strategy.exit_reason(101.0, 100.0) is None


# ---------------------------------------------------------------- runner


class FakeIO:
    def __init__(self, prices, position=RICH, eth=Decimal(1)):
        self.prices = list(prices)
        self._position = position
        self.eth = eth
        self.trades = []
        self.attempts = 0  # every call to trade(), including the ones that fail
        self.trade_error = None
        self.read_error = None
        self.eth_error = None
        self.symbols = ("fETH", "f564")
        self.eth_reads = 0
        self.liquidity = None  # what the pool reports as its liquidity in range
        self.liquidity_error = None
        self.liquidity_reads = 0
        self.depth_scan = None  # what a scan of the whole pool returns
        self.depth_error = None
        self.depth_reads = 0
        # what the vault receives: base for a buy, quote for a sell
        self.received = {"buy": Decimal("0.0001"), "sell": Decimal("0.0105")}

    def read_price(self):
        if self.read_error:
            raise self.read_error
        return self.prices.pop(0) if len(self.prices) > 1 else self.prices[0]

    def position(self):
        return self._position

    def read_depth(self, stop=None):
        self.depth_reads += 1
        self.depth_stop = stop
        if self.depth_error:
            raise self.depth_error
        return self.depth_scan

    def read_liquidity(self):
        self.liquidity_reads += 1
        if self.liquidity_error:
            raise self.liquidity_error
        return self.liquidity

    def operator_eth(self):
        self.eth_reads += 1
        if self.eth_error:
            raise self.eth_error
        return self.eth

    def trade(self, decision, amount):
        self.attempts += 1
        if self.trade_error:
            raise self.trade_error
        self.trades.append((decision, amount))
        return self.received[decision]


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
    def build(io, guard=False, depth=None, depth_file=None, **kw):
        clock = Clock()
        if not guard:
            control.save(
                tmp_path / "logs" / "control.json",
                strategy.parse_config({"guard_flip_pct": 0, "guard_drift_pct": 0}),
            )
        runner = bot_main.Runner(
            io,
            settings(**kw),
            threading.Event(),
            clock=clock,
            halt_file=tmp_path / "logs" / "HALTED",
            heartbeat_file=tmp_path / "logs" / "heartbeat",
            ledger_file=tmp_path / "logs" / "ledger.json",
            control_file=tmp_path / "logs" / "control.json",
            history_file=tmp_path / "logs" / "prices.csv",
            state_file=tmp_path / "logs" / "state.json",
            depth=depth,
            depth_file=depth_file,
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


def test_cooldown_blocks_a_new_entry_until_it_expires(make):
    io = FakeIO([100.0])
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()  # buy at 90
    io.prices = [80.0]
    runner.step()  # stop loss: sells at once, the cooldown does not apply to exits
    assert [t[0] for t in io.trades] == ["buy", "sell"]
    runner.step()  # still a dip, but the cooldown blocks a new entry
    assert len(io.trades) == 2
    runner.test_clock.now += 301
    runner.step()
    assert [t[0] for t in io.trades] == ["buy", "sell", "buy"]


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


# ---------------------------------------------------------------- state file for the dashboard


def test_each_tick_writes_what_it_read_for_the_dashboard(make):
    io = FakeIO([100.0], position=Position(base=0.5, quote=12.5), eth=Decimal("0.05"))
    runner = make(io)
    runner.step()
    chain = state.load(runner.state_file)
    assert (chain["base"], chain["quote"]) == ("fETH", "f564")
    assert chain["price"] == 100.0
    assert chain["vault_base"] == Decimal("0.5")
    assert chain["vault_quote"] == Decimal("12.5")
    assert chain["operator_eth"] == Decimal("0.05")
    assert abs(chain["updated"] - time.time()) < 60


def test_the_operator_gas_is_not_read_every_tick(make):
    io = FakeIO([100.0])
    runner = make(io)
    for _ in range(5):
        runner.step()
    assert io.eth_reads == 1
    runner.test_clock.now += bot_main.OPERATOR_ETH_REFRESH + 1
    runner.step()
    assert io.eth_reads == 2


def test_the_operator_gas_is_re_read_after_a_trade(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io)
    reads = io.eth_reads
    io.eth = Decimal("0.04")  # the swap cost some gas
    runner.step()
    assert io.eth_reads > reads
    assert state.load(runner.state_file)["operator_eth"] == Decimal("0.04")


def test_a_state_file_problem_never_skips_the_tick(make, caplog):
    io = FakeIO([100.0])
    runner = make(io)
    io.eth_error = ConnectionError("429 Too Many Requests")
    with caplog.at_level(logging.WARNING):
        runner.step()
    assert not runner.state_file.exists()
    assert "state file" in caplog.text
    assert list(runner.prices) == [100.0]  # the tick carried on: price recorded
    assert runner.read_failures == 0  # and it is not counted as a failed read

    io.eth_error = None
    runner.state_file.parent.mkdir(exist_ok=True)
    runner.state_file.mkdir()  # a directory where the file goes: the write fails
    runner.step()
    assert list(runner.prices) == [100.0, 100.0]


def test_the_state_file_is_optional_so_other_callers_are_unaffected(tmp_path):
    runner = bot_main.Runner(FakeIO([100.0]), settings(), threading.Event())
    assert runner.state_file is None
    runner.write_state(100.0, RICH)  # does nothing


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


# ---------------------------------------------------------------- positions (TP/SL)


def open_a_position(runner, io, price=90.0):
    warm_up(runner, io)
    io.prices = [price]
    runner.step()


def test_a_buy_opens_a_position_and_saves_it(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io)
    held = ledger.load(runner.ledger_file)["open"]
    assert held["entry_price"] == 90.0
    assert Decimal(held["base"]) == Decimal(
        "0.0001"
    )  # what arrived, not the trade size
    assert Decimal(held["quote_spent"]) == Decimal("0.01")


def test_no_second_buy_while_holding(make):
    io = FakeIO([100.0])
    runner = make(io, cooldown_seconds=0)
    open_a_position(runner, io)
    for _ in range(5):
        runner.step()
    assert len(io.trades) == 1


def test_take_profit_sells_exactly_what_was_bought_and_records_the_profit(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [94.6]  # +5.1% over the 90.0 entry
    runner.step()
    assert io.trades[-1] == ("sell", Decimal("0.0001"))
    saved = ledger.load(runner.ledger_file)
    assert saved["open"] is None
    (trade,) = saved["closed"]
    assert trade["reason"] == "take_profit"
    assert ledger.profit(trade) == Decimal("0.0005")  # 0.0105 back for 0.01 spent


def test_stop_loss_sells_and_records_the_loss(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [85.0]  # -5.6%
    io.received["sell"] = Decimal("0.0096")
    runner.step()
    (trade,) = ledger.load(runner.ledger_file)["closed"]
    assert trade["reason"] == "stop_loss"
    assert ledger.profit(trade) == Decimal("-0.0004")


def test_nothing_happens_inside_the_band(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [92.0]
    runner.step()
    assert len(io.trades) == 1
    assert ledger.load(runner.ledger_file)["open"] is not None


def test_the_position_survives_a_restart_and_exits_without_a_warm_up(make):
    io = FakeIO([100.0])
    first = make(io)
    open_a_position(first, io, 90.0)

    io2 = FakeIO(
        [86.0]
    )  # a fresh process: no price history, price already below the stop
    second = make(io2)
    second.step()
    assert io2.trades == [("sell", Decimal("0.0001"))]


def test_a_failed_exit_waits_for_the_cooldown_before_trying_again(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [80.0]
    io.trade_error = ContractLogicError("STF")
    runner.step()  # the stop loss fails once
    runner.step()  # not hammered: cooldown after a failure
    io.trade_error = None
    assert runner.trade_failures == 1
    assert len(io.trades) == 1
    runner.test_clock.now += 301
    runner.step()
    assert io.trades[-1][0] == "sell"
    assert ledger.load(runner.ledger_file)["open"] is None


def test_an_exit_is_skipped_while_the_vault_lacks_the_base(make):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1.0))
    runner = make(io)
    runner.ledger = ledger.open_position(
        ledger.empty(), 90.0, Decimal("0.0001"), Decimal("0.01"), 0
    )
    io.prices = [80.0]
    runner.step()
    assert io.trades == []


def test_a_float_balance_a_hair_under_the_recorded_amount_still_sells(make):
    io = FakeIO([80.0], position=Position(base=0.0001 - 1e-15, quote=1.0))
    runner = make(io)
    runner.ledger = ledger.open_position(
        ledger.empty(), 90.0, Decimal("0.0001"), Decimal("0.01"), 0
    )
    runner.step()
    assert io.trades == [("sell", Decimal("0.0001"))]


def test_a_buy_that_delivers_nothing_halts(make):
    io = FakeIO([100.0])
    runner = make(io)
    warm_up(runner, io)
    io.prices = [90.0]
    io.received["buy"] = Decimal(0)
    with pytest.raises(bot_main.Halt, match="received nothing"):
        runner.step()


def test_an_unreadable_ledger_halts_instead_of_starting_over(make, tmp_path):
    path = tmp_path / "logs" / "ledger.json"
    path.parent.mkdir()
    path.write_text("{not json")
    runner = make(FakeIO([100.0]))
    runner.run()
    assert "unreadable" in runner.halt_file.read_text()
    assert runner.io.prices == [100.0]  # never ticked


def test_dry_run_and_live_use_different_ledger_files():
    assert ledger.file_for(True) != ledger.file_for(False)


# ---------------------------------------------------------------- ChainIO.trade

VAULT = "0x" + "aa" * 20
POOL = "0x" + "bb" * 20
BASE_ADDRESS = "0x" + "11" * 20
QUOTE_ADDRESS = "0x" + "22" * 20


def transfer_log(token, to, amount, topic=bot_main.TRANSFER_TOPIC):
    pad = lambda address: bytes.fromhex(address[2:]).rjust(32, b"\0")
    return {
        "address": token,
        "topics": [topic, pad(POOL), pad(to)],
        "data": amount.to_bytes(32, "big"),
    }


def receipt_with(*logs):
    return {"status": 1, "logs": list(logs)}


class _Call:
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value


class _FakePool:
    def __init__(self, token0):
        self.functions = self
        self._token0 = token0

    def token0(self):
        return _Call(self._token0)


class _FakeBot:
    address = VAULT

    def __init__(self, cap):
        self.functions = self
        self.cap = cap

    def tokenCap(self, token):
        return _Call(self.cap)


BASE = (BASE_ADDRESS, 18, "fETH")
QUOTE = (QUOTE_ADDRESS, 6, "f564")


def make_chain_io(monkeypatch, dry_run, receipt=None, too_large=False, cap=10**30):
    market = {
        "pool": _FakePool(BASE_ADDRESS),
        "fee": 3000,
        "tokens": {"feth": BASE, "f564": QUOTE},
    }
    io = bot_main.ChainIO(None, None, _FakeBot(cap), market, settings(dry_run=dry_run))
    quote = {"amount_out": 5_000_000, "too_large": too_large}
    monkeypatch.setattr(bot_main.config, "get_quote", lambda *a: quote)
    sent = []

    def fake_swap(*args):
        sent.append(args)
        return None if dry_run else receipt

    monkeypatch.setattr(bot_main.bot_contract, "swap_via_contract", fake_swap)
    return io, sent


def test_received_from_receipt_counts_only_the_vaults_transfers_of_that_token():
    other_topic = bytes(32)
    receipt = receipt_with(
        transfer_log(BASE_ADDRESS, VAULT, 3 * 10**15),
        transfer_log(
            BASE_ADDRESS, VAULT, 10**15
        ),  # a second leg into the vault adds up
        transfer_log(BASE_ADDRESS, "0x" + "cc" * 20, 99),  # someone else's tokens
        transfer_log(QUOTE_ADDRESS, VAULT, 77),  # another token
        transfer_log(BASE_ADDRESS, VAULT, 5, topic=other_topic),  # not a Transfer event
    )
    assert bot_main.received_from_receipt(receipt, BASE_ADDRESS, VAULT) == 4 * 10**15
    assert bot_main.received_from_receipt(receipt, "0x" + "33" * 20, VAULT) == 0
    assert bot_main.received_from_receipt(receipt_with(), BASE_ADDRESS, VAULT) == 0


def test_received_from_receipt_ignores_case_in_addresses():
    receipt = receipt_with(transfer_log(BASE_ADDRESS.upper(), VAULT.upper(), 5))
    assert bot_main.received_from_receipt(receipt, BASE_ADDRESS.lower(), VAULT) == 5


def test_chain_io_live_buy_returns_the_base_that_the_receipt_shows(monkeypatch):
    receipt = receipt_with(transfer_log(BASE_ADDRESS, VAULT, 3 * 10**15))
    io, sent = make_chain_io(monkeypatch, False, receipt)
    assert io.trade("buy", Decimal("0.01")) == Decimal("0.003")
    assert len(sent) == 1


def test_chain_io_live_sell_returns_the_quote_that_the_receipt_shows(monkeypatch):
    receipt = receipt_with(transfer_log(QUOTE_ADDRESS, VAULT, 5_500_000))
    io, _ = make_chain_io(monkeypatch, False, receipt)
    assert io.trade("sell", Decimal("0.001")) == Decimal("5.5")


def test_chain_io_dry_run_uses_the_quoted_output(monkeypatch):
    io, _ = make_chain_io(monkeypatch, True)
    assert io.trade("buy", Decimal("0.01")) == Decimal("5e-12")


def test_chain_io_refuses_a_too_large_impact_before_sending(monkeypatch):
    io, sent = make_chain_io(monkeypatch, False, too_large=True)
    with pytest.raises(bot_main.Skip):
        io.trade("buy", Decimal("0.01"))
    assert sent == []


def test_a_buy_whose_base_could_exceed_the_contract_cap_is_refused(monkeypatch):
    # the sell would have to trade more base than the contract allows per swap: never open it
    io, sent = make_chain_io(
        monkeypatch, False, cap=5_000_000
    )  # quoted out is 5_000_000
    with pytest.raises(bot_main.Skip, match="per-swap cap"):
        io.trade("buy", Decimal("0.01"))
    assert sent == []


def test_the_cap_guard_applies_to_buys_only(monkeypatch):
    receipt = receipt_with(transfer_log(QUOTE_ADDRESS, VAULT, 5_500_000))
    io, _ = make_chain_io(monkeypatch, False, receipt, cap=1)
    assert io.trade("sell", Decimal("0.001")) == Decimal("5.5")


def test_a_connection_error_before_sending_is_not_sent_and_not_a_halt(monkeypatch):
    io, sent = make_chain_io(monkeypatch, False)

    def down(*a):
        raise ConnectionError("rpc down")

    monkeypatch.setattr(bot_main.config, "get_quote", down)
    with pytest.raises(bot_main.bot_contract.NotSent):
        io.trade("buy", Decimal("0.01"))
    assert sent == []


# ---------------------------------------------------------------- failures around the send


def armed_runner(make, io, **kw):
    runner = make(io, **kw)
    warm_up(runner, io)
    io.prices = [90.0]
    return runner


def test_a_swap_that_was_not_sent_is_neither_a_failure_nor_a_halt(make):
    io = FakeIO([100.0])
    runner = armed_runner(make, io)
    io.trade_error = bot_main.bot_contract.NotSent("rpc blip")
    runner.step()
    assert runner.trade_failures == 0
    assert not runner.halt_file.exists()
    assert ledger.load(runner.ledger_file)["open"] is None


def test_a_swap_that_may_be_on_chain_halts_and_is_never_retried(make):
    io = FakeIO([100.0])
    runner = armed_runner(make, io, cooldown_seconds=0)
    io.trade_error = bot_main.bot_contract.SentUnknown("receipt poll failed")
    with pytest.raises(bot_main.Halt, match="may be on chain"):
        runner.step()
    assert len(io.trades) == 0


def test_run_writes_the_halt_file_for_an_unknown_outcome(make):
    io = FakeIO([100.0])
    runner = armed_runner(make, io, cooldown_seconds=0, poll_seconds=0)
    io.trade_error = bot_main.bot_contract.SentUnknown("receipt poll failed")
    runner.run()
    assert "may be on chain" in runner.halt_file.read_text()


def test_a_ledger_that_cannot_be_saved_halts_with_the_numbers_to_fix_it(
    make, monkeypatch
):
    io = FakeIO([100.0])
    runner = armed_runner(make, io)

    def disk_full(*a):
        raise OSError("No space left on device")

    monkeypatch.setattr(ledger, "save", disk_full)
    with pytest.raises(bot_main.Halt) as halted:
        runner.step()
    text = str(halted.value)
    assert "could not be saved" in text and "0.0001" in text and "0.01" in text


def test_an_unreadable_operator_balance_skips_the_trade_instead_of_crashing(make):
    io = FakeIO([100.0])
    runner = armed_runner(make, io)

    def down():
        raise ConnectionError("rpc down")

    io.operator_eth = down
    runner.step()
    assert io.trades == []


def test_a_skipped_exit_is_logged_as_a_warning(make, caplog):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [80.0]
    io.trade_error = bot_main.Skip("price impact too large")
    runner.step()
    skipped = [r for r in caplog.records if "sell skipped" in r.message]
    assert skipped and skipped[0].levelname == "WARNING"


def test_a_skipped_entry_is_only_info(make, caplog):
    caplog.set_level(logging.INFO)
    io = FakeIO([100.0])
    runner = armed_runner(make, io)
    io.trade_error = bot_main.Skip("price impact too large")
    runner.step()
    skipped = [r for r in caplog.records if "buy skipped" in r.message]
    assert skipped and skipped[0].levelname == "INFO"


# ---------------------------------------------------------------- settings changed while running


def write_settings(runner, **raw):
    # the guard is off unless a test asks for it: most tests are about trading, not the guard
    raw = {"guard_flip_pct": 0, "guard_drift_pct": 0, **raw}
    control.save(runner.control_file, strategy.parse_config(raw))


def test_new_settings_are_picked_up_on_the_next_tick(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [91.5]  # +1.7%: inside the default band
    runner.step()
    assert len(io.trades) == 1
    write_settings(runner, take_profit_pct=1.5)
    runner.step()
    assert io.trades[-1][0] == "sell"
    assert runner.cfg.take_profit_pct == 1.5


def test_switching_strategy_changes_what_triggers_a_buy(make):
    io = FakeIO([100.0])
    runner = make(io, cooldown_seconds=0)
    warm_up(runner, io)
    write_settings(runner, strategy="breakout")
    io.prices = [90.0]  # a dip: mean reversion would buy, a breakout strategy must not
    runner.step()
    assert io.trades == []
    io.prices = [105.0]
    runner.step()
    assert io.trades == [("buy", Decimal("0.01"))]


def test_an_invalid_settings_file_keeps_the_current_settings(make, caplog):
    io = FakeIO([100.0])
    runner = make(io)
    write_settings(runner, strategy="rsi")
    runner.step()
    assert runner.cfg.strategy == "rsi"
    runner.control_file.write_text('{"strategy": "yolo"}')
    runner.step()
    assert runner.cfg.strategy == "rsi"  # unchanged, and the bot kept running
    assert any(
        "ignoring the settings file, keeping the current ones" in r.message
        for r in caplog.records
    )
    assert not runner.halt_file.exists()


def test_no_settings_file_means_defaults(make):
    runner = make(
        FakeIO([100.0]), guard=True
    )  # guard=True: no settings file is written
    runner.step()
    assert runner.cfg == strategy.DEFAULT


def test_settings_are_loaded_when_the_runner_starts(make):
    first = make(FakeIO([100.0]), guard=True)
    write_settings(first, strategy="bollinger", stop_loss_pct=2.0)
    second = make(FakeIO([100.0]), guard=True)  # keeps the file the first one wrote
    assert (second.cfg.strategy, second.cfg.stop_loss_pct) == ("bollinger", 2.0)


def test_saving_the_same_settings_again_is_not_logged_as_a_change(make, caplog):
    caplog.set_level(logging.INFO)
    runner = make(FakeIO([100.0]), guard=True)
    write_settings(runner, strategy="rsi")
    runner.step()
    write_settings(runner, strategy="rsi")  # a new file, identical settings
    runner.step()
    assert sum("settings changed" in r.message for r in caplog.records) == 1


def test_each_tick_appends_a_price_to_the_history(make):
    io = FakeIO([100.0, 101.0, 102.5])
    runner = make(io)
    for _ in range(3):
        runner.step()
    prices = [p for _, p in history.tail(runner.history_file, 10)]
    assert prices == [100.0, 101.0, 102.5]


def test_a_failed_read_records_no_price(make):
    io = FakeIO([100.0])
    io.read_error = ConnectionError("down")
    runner = make(io)
    runner.step()
    assert history.tail(runner.history_file, 10) == []


def test_a_history_write_failure_does_not_stop_the_bot(make, monkeypatch):
    def fail(*a):
        raise OSError("disk full")

    monkeypatch.setattr(history, "append", fail)
    runner = make(FakeIO([100.0]))
    runner.step()
    assert len(runner.prices) == 1


# ---------------------------------------------------------------- settings edge cases


def test_an_invalid_settings_file_at_start_says_the_defaults_are_used(
    make, tmp_path, caplog
):
    path = tmp_path / "logs" / "control.json"
    path.parent.mkdir()
    path.write_text('{"strategy": "yolo"}')
    runner = make(FakeIO([100.0]), guard=True)
    assert runner.cfg == strategy.DEFAULT
    assert any(
        "using the defaults until it is fixed" in r.message for r in caplog.records
    )


def test_a_settings_file_that_cannot_be_read_keeps_the_current_settings(make, caplog):
    io = FakeIO([100.0])
    runner = make(io)
    write_settings(runner, strategy="rsi")
    runner.step()
    runner.control_file.unlink()
    runner.control_file.mkdir()  # reading a directory raises an OSError, not a ValueError
    runner.step()  # must not crash
    assert runner.cfg.strategy == "rsi"


def test_the_runner_uses_the_dry_run_ledger_file_in_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LOG_DIR", tmp_path)
    dry = bot_main.Runner(FakeIO([100.0]), settings(dry_run=True), threading.Event())
    live = bot_main.Runner(FakeIO([100.0]), settings(dry_run=False), threading.Event())
    assert dry.ledger_file.name == "ledger_dryrun.json"
    assert live.ledger_file.name == "ledger.json"


# ---------------------------------------------------------------- promises the docs make


def test_a_failed_buy_waits_for_the_cooldown_before_the_next_attempt(make):
    io = FakeIO([100.0])
    runner = armed_runner(make, io)
    io.trade_error = ContractLogicError("STF")
    runner.step()
    runner.step()
    assert io.attempts == 1  # the broken swap is not hammered
    runner.test_clock.now += 301
    runner.step()
    assert io.attempts == 2


def test_a_skipped_buy_also_starts_the_cooldown(make):
    io = FakeIO([100.0])
    runner = armed_runner(make, io)
    io.trade_error = bot_main.Skip("price impact too large")
    runner.step()
    runner.step()
    assert io.attempts == 1
    runner.test_clock.now += 301
    runner.step()
    assert io.attempts == 2


def test_the_log_lines_the_docs_point_to_exist(make, caplog):
    caplog.set_level(logging.INFO)
    io = FakeIO([100.0])
    runner = make(io)
    write_settings(runner, strategy="mean_reversion", stop_loss_pct=2.0)
    open_a_position(runner, io, 90.0)
    io.prices = [80.0]
    runner.step()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "settings changed" in text
    assert "opened position:" in text
    assert "closed position (stop_loss)" in text
    assert "strategy=mean_reversion price=" in text and "decision=" in text


@pytest.mark.parametrize(
    ("value", "dry"),
    [
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("true", True),
        ("", True),
        ("no", True),
        ("0", True),
        ("fasle", True),
    ],
)
def test_only_the_word_false_turns_dry_run_off(monkeypatch, value, dry):
    monkeypatch.setenv("MAX_TRADE_AMOUNT", "0.01")
    monkeypatch.setenv("DRY_RUN", value)
    assert bot_main.Settings.from_env().dry_run is dry


def test_the_bot_refuses_to_start_while_the_halt_file_exists(
    tmp_path, monkeypatch, caplog
):
    halt = tmp_path / "HALTED"
    halt.write_text("2026-11-04 10:00:00 3 failed trades in a row\n")
    monkeypatch.setattr(bot_main, "HALT_FILE", halt)
    monkeypatch.setattr(bot_main, "load_dotenv", lambda: None)
    monkeypatch.setattr(bot_main, "setup_logging", lambda: None)

    def must_not_connect():
        raise AssertionError("connected although the bot is halted")

    monkeypatch.setattr(bot_main.bot_contract, "connect", must_not_connect)
    bot_main.main()
    assert any("Not starting" in r.getMessage() for r in caplog.records)
    assert halt.exists()  # only a person removes it


# ---------------------------------------------------------------- the pattern guard and restarts


def test_the_runner_says_when_buying_pauses_and_resumes(make, caplog):
    caplog.set_level(logging.INFO)
    io = FakeIO([100.0])
    runner = make(io, guard=True)
    runner.step()
    runner.step()
    assert (
        sum("new buys are paused" in r.message for r in caplog.records) == 1
    )  # not every tick
    # an alternating pattern: 4% steps up and down, a few quiet samples between
    pattern = []
    price = 100.0
    for d in [1, -1] * 5:
        price *= 1.04 if d > 0 else 1 / 1.04
        pattern += [price] * 3
    for p in pattern:
        io.prices = [p]
        runner.step()
    assert any("new buys are allowed again" in r.message for r in caplog.records)


def test_a_fresh_price_history_is_restored_after_a_restart(make):
    first = make(FakeIO([100.0]))
    for _ in range(3):
        first.step()
    second = make(FakeIO([100.0]))
    assert list(second.prices) == [100.0, 100.0, 100.0]


def test_a_stale_price_history_is_not_carried_over(make, monkeypatch):
    first = make(FakeIO([100.0]))
    first.step()
    real = time.time()
    monkeypatch.setattr(
        bot_main.time, "time", lambda: real + bot_main.PRELOAD_MAX_AGE + 60
    )
    second = make(FakeIO([100.0]))
    assert list(second.prices) == []


def alternating(steps=10, quiet=3):
    # 4% jumps up and down with a few quiet samples between: the pool's pattern
    prices, price = [], 100.0
    for d in [1, -1] * (steps // 2):
        price *= 1.04 if d > 0 else 1 / 1.04
        prices += [price] * quiet
    return prices


def feed(runner, io, prices):
    for p in prices:
        io.prices = [p]
        runner.step()


def test_with_the_guard_on_a_healthy_pattern_still_buys_a_dip(make):
    io = FakeIO([100.0])
    runner = make(io, guard=True)
    feed(runner, io, alternating())
    assert io.trades == []
    io.prices = [runner.prices[-1] * 0.9]  # a real dip
    runner.step()
    assert [side for side, _ in io.trades] == ["buy"]


def test_with_the_guard_on_a_downtrend_blocks_the_buy_and_says_so(make, caplog):
    caplog.set_level(logging.INFO)
    io = FakeIO([100.0])
    runner = make(io, guard=True)
    prices, price = [], 100.0
    for _ in range(10):  # ten big drops in a row
        price /= 1.04
        prices += [price] * 3
    feed(runner, io, prices)
    io.prices = [price * 0.9]  # the entry rule would buy this dip
    runner.step()
    assert io.trades == []
    assert any("new buys are paused" in r.message for r in caplog.records)


def hold(runner, price=100.0):
    # a position the bot already holds, without going through a buy (the guard would block it)
    runner.ledger = ledger.open_position(
        ledger.empty(), price, Decimal("0.0001"), Decimal("0.01"), 0
    )


def test_a_stop_loss_is_never_held_back_by_the_guard(make):
    io = FakeIO([100.0])
    runner = make(io, guard=True)
    hold(runner)
    io.prices = [80.0]  # far below the stop loss, and the guard has seen nothing
    runner.step()
    assert io.trades[-1][0] == "sell"


def test_a_slow_slide_logs_the_pause_once_not_every_tick(make, caplog):
    caplog.set_level(logging.INFO)
    io = FakeIO([100.0])
    runner = make(io, guard=True)
    feed(runner, io, alternating())
    price = runner.prices[-1]
    for _ in range(120):  # a slide of 0.3% per tick: the reason text changes every tick
        price *= 0.997
        feed(runner, io, [price])
    paused = [r for r in caplog.records if "new buys are paused" in r.message]
    assert len(paused) <= 3  # once per check that pauses, never once per tick


def test_nothing_is_logged_about_the_guard_while_holding_a_position(make, caplog):
    caplog.set_level(logging.INFO)
    io = FakeIO([100.0])
    runner = make(io, guard=True)
    hold(runner)
    caplog.clear()
    io.prices = [100.5]
    runner.step()
    assert not any("new buys are" in r.message for r in caplog.records)


def test_prices_are_restored_only_from_an_unbroken_recent_stretch(make, tmp_path):
    path = tmp_path / "logs" / "prices.csv"
    path.parent.mkdir(exist_ok=True)
    now = time.time()
    # an old burst, a two day gap, then a recent run
    old = [(now - 200_000 + 30 * i, 50.0) for i in range(5)]
    recent = [(now - 150 + 30 * i, 100.0 + i) for i in range(5)]
    for ts, price in old + recent:
        history.append(path, ts, price)
    runner = make(FakeIO([100.0]))
    assert list(runner.prices) == [100.0, 101.0, 102.0, 103.0, 104.0]


# ---------------------------------------------------------------- time stop and risk sizing


def test_the_time_stop_sells_an_old_position_and_records_why(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    write_settings(runner, time_stop_on=True, time_stop_min=15)
    io.prices = [90.5]  # inside the band: only the clock can end it
    runner.step()
    assert [t[0] for t in io.trades] == ["buy"]  # 0 minutes held: nothing yet
    held = runner.ledger["open"]
    runner.ledger = {**runner.ledger, "open": {**held, "opened": time.time() - 16 * 60}}
    runner.step()
    assert io.trades[-1] == ("sell", Decimal("0.0001"))
    (trade,) = runner.ledger["closed"]
    assert trade["reason"] == "time_stop"


def test_the_time_stop_does_nothing_unless_it_is_ticked(make):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    held = runner.ledger["open"]
    runner.ledger = {**runner.ledger, "open": {**held, "opened": time.time() - 3600}}
    io.prices = [90.5]
    runner.step()
    assert [t[0] for t in io.trades] == ["buy"]


def test_a_buy_is_sized_by_risk_when_asked(make):
    # vault quote 1000, stop loss 4% (+0.75% costs), risking 1% = 10 -> 210.526315 quote
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(500), risk_pct=Decimal(1))
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == [("buy", Decimal("210.526315"))]
    assert runner.ledger["open"]["quote_spent"] == "210.526315"


def in_range(price, scale=1.0):
    # the liquidity a pool as deep as the saved layout (times `scale`) reports at `price`
    return int(v3pool.V3Pool(v3pool.load_profile(), price).L * 10**18 * scale)


def test_a_buy_is_sized_on_the_pools_real_costs_when_it_has_the_depth(make, caplog):
    # a 40000 vault risking 2% on a 4% stop: the fixed 0.75% cost would ask for 16,842, but a buy
    # that big pays far more than that to the pool, so the stop loss would cost more than 2%
    io = FakeIO([1043.0], position=Position(base=0.0, quote=40000.0))
    io.liquidity = in_range(1000.0)
    runner = make(
        io,
        max_trade=Decimal(40000),
        risk_pct=Decimal(2),
        depth=v3pool.load_profile(),
    )
    warm_up(runner, io)
    io.prices = [1000.0]  # a 4% dip under the average: a buy
    with caplog.at_level(logging.INFO):
        runner.step()
    ((decision, amount),) = io.trades
    assert decision == "buy"
    assert amount < Decimal(16842)

    def loss(quote):
        return v3pool.loss_at_stop(v3pool.load_profile(), 1000.0, 4.0, quote)

    assert loss(float(amount)) <= 800 < loss(float(amount) + 1)
    assert "RISK_PER_TRADE_PCT=2" in caplog.text


def buy_amount(make, tmp_path, liquidity, **kw):
    # the amount a 40000 vault spends on a dip at 1000 when the pool reports `liquidity`
    for name in ("ledger.json", "prices.csv"):  # a fresh start every time
        (tmp_path / "logs" / name).unlink(missing_ok=True)
    io = FakeIO([1043.0], position=Position(base=0.0, quote=40000.0))
    io.liquidity = liquidity
    runner = make(
        io,
        max_trade=Decimal(40000),
        risk_pct=Decimal(2),
        depth=v3pool.load_profile(),
        **kw,
    )
    warm_up(runner, io)
    io.prices = [1000.0]
    runner.step()
    ((decision, amount),) = io.trades
    assert decision == "buy"
    return amount


def test_a_thinner_pool_gets_a_smaller_buy_and_a_deeper_one_a_bigger(make, tmp_path):
    as_saved = buy_amount(make, tmp_path, in_range(1000.0))
    assert buy_amount(make, tmp_path, in_range(1000.0, 0.5)) < as_saved
    assert buy_amount(make, tmp_path, in_range(1000.0, 2.0)) > as_saved


def test_a_pool_with_almost_no_liquidity_limits_the_buy_to_about_the_budget_itself(
    make, tmp_path
):
    # the pool eats about all of any buy, so a stop loss costing 2% of the vault (800) only
    # allows a buy of about 800
    amount = buy_amount(make, tmp_path, in_range(1000.0, 1e-6))
    assert Decimal(800) <= amount < Decimal(1000)


def test_the_pools_liquidity_is_read_once_when_a_buy_is_sized_and_not_again_for_a_while(
    make,
):
    io = FakeIO([1043.0], position=Position(base=0.0, quote=40000.0))
    io.liquidity = in_range(1000.0)
    runner = make(
        io, max_trade=Decimal(40000), risk_pct=Decimal(2), depth=v3pool.load_profile()
    )
    warm_up(runner, io)
    assert io.liquidity_reads == 0  # holding nothing and not buying: no read at all
    runner.buy_size(Position(base=0.0, quote=40000.0), 1000.0)
    runner.buy_size(Position(base=0.0, quote=40000.0), 1000.0)
    assert io.liquidity_reads == 1
    runner.test_clock.now += bot_main.DEPTH_MAX_AGE + 1
    runner.buy_size(Position(base=0.0, quote=40000.0), 1000.0)
    assert io.liquidity_reads == 2


def test_no_liquidity_is_read_when_risk_sizing_is_off(make):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(500), depth=v3pool.load_profile())
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == [("buy", Decimal(500))] and io.liquidity_reads == 0


def test_a_price_outside_the_saved_layout_skips_the_buy_and_says_why(make, caplog):
    io = FakeIO([2086.0], position=Position(base=0.0, quote=40000.0))
    io.liquidity = in_range(1000.0)  # the read works; the layout holds nothing up here
    profile = v3pool.load_profile()
    # (only the positions with a range: the full-range one would hold liquidity at any price)
    ranged = {
        **profile,
        "ticks": [t for t in profile["ticks"] if abs(t["tick"]) < 800_000],
    }
    runner = make(io, max_trade=Decimal(40000), risk_pct=Decimal(2), depth=ranged)
    warm_up(runner, io)
    io.prices = [2000.0]
    with caplog.at_level(logging.WARNING):
        runner.step()
    assert io.trades == []
    assert "buy skipped: the pool's depth cannot be sized" in caplog.text
    assert "could not read" not in caplog.text


def test_a_failed_liquidity_read_skips_the_buy_and_nothing_more(make, caplog):
    io = FakeIO([1043.0], position=Position(base=0.0, quote=40000.0))
    io.liquidity_error = OSError("connection reset")
    runner = make(
        io, max_trade=Decimal(40000), risk_pct=Decimal(2), depth=v3pool.load_profile()
    )
    warm_up(runner, io)
    io.prices = [1000.0]
    with caplog.at_level(logging.WARNING):
        runner.step()
    assert io.trades == []  # skipped: not a halt, not a crash
    assert "buy skipped: could not read the pool's liquidity" in caplog.text
    # the next dip, once the read works, buys
    io.liquidity_error, io.liquidity = None, in_range(1000.0)
    runner.test_clock.now += 600
    runner.step()
    assert [t[0] for t in io.trades] == ["buy"]


def test_the_state_file_tells_the_dashboard_how_deep_the_pool_was(make, tmp_path):
    io = FakeIO([1043.0], position=Position(base=0.0, quote=40000.0))
    io.liquidity = in_range(1000.0, 0.5)
    runner = make(
        io, max_trade=Decimal(40000), risk_pct=Decimal(2), depth=v3pool.load_profile()
    )
    warm_up(runner, io)
    # nothing has been read yet, so the state file says nothing about depth (the dashboard then
    # takes the layout as saved)
    assert state.load(tmp_path / "logs" / "state.json")["depth_scale"] == 1.0
    io.prices = [1000.0]
    runner.step()  # sizes a buy: reads the liquidity
    runner.step()  # the next tick writes what it found
    assert state.load(tmp_path / "logs" / "state.json")["depth_scale"] == pytest.approx(
        0.5, rel=1e-3
    )


def control_with(tmp_path, **extra):
    # the settings file as the dashboard leaves it (the pattern guard off, as `make` does)
    control.save(
        tmp_path / "logs" / "control.json",
        strategy.parse_config({"guard_flip_pct": 0, "guard_drift_pct": 0, **extra}),
    )


# ---------------------------------------------------------------- the pause button


def test_paused_buys_nothing_and_resuming_buys_again(make, tmp_path, caplog):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(500))
    warm_up(runner, io)
    control_with(tmp_path, paused=True)
    io.prices = [90.0]  # a dip: without the pause this is a buy
    with caplog.at_level(logging.INFO):
        runner.step()
    assert io.trades == []
    assert "new buys are PAUSED" in caplog.text and "decision=hold" in caplog.text
    assert "PAUSED" in caplog.text.splitlines()[-1] or "PAUSED" in caplog.text
    control_with(tmp_path, paused=False)
    with caplog.at_level(logging.INFO):
        runner.step()
    assert [t[0] for t in io.trades] == ["buy"]
    assert "new buys are resumed" in caplog.text


def test_a_sale_is_never_held_back_by_the_pause(make, tmp_path):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, paused=True)
    io.prices = [95.0]  # +5.6%: take profit
    runner.step()
    assert [t[0] for t in io.trades] == ["buy", "sell"]


def test_a_stop_loss_still_sells_while_paused(make, tmp_path):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, paused=True)
    io.prices = [80.0]  # -11%: stop loss
    runner.step()
    assert [t[0] for t in io.trades] == ["buy", "sell"]


# ---------------------------------------------------------------- the scan button


def scan_result(block):
    # what a scan of the pool returns: the shipped layout, as of another block
    return {**v3pool.load_profile(), "block": block}


def test_a_scan_request_reads_the_pool_saves_it_and_answers_the_dashboard(
    make, tmp_path
):
    io = FakeIO([100.0])
    io.depth_scan = scan_result(777)
    saved = tmp_path / "logs" / "pool_profile.json"
    runner = make(io, depth_file=saved)
    runner.step()
    assert io.depth_reads == 0  # nobody asked
    control_with(tmp_path, scan_request=5)
    runner.step()
    assert io.depth_reads == 1 and runner.depth["block"] == 777
    assert v3pool.current_profile(saved)["request"] == 5
    runner.step()  # the tick after writes the answer to the state file
    answer = state.load(tmp_path / "logs" / "state.json")["scan"]
    assert (answer["done"], answer["block"], answer["error"]) == (5, 777, None)
    assert answer["at"] > 0
    assert io.depth_reads == 1  # served once, not on every tick


def test_the_layout_in_use_is_measured_afresh_after_a_scan(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_scan = scan_result(1)
    runner = make(
        io, depth=v3pool.load_profile(), depth_file=tmp_path / "logs" / "p.json"
    )
    runner.scale, runner.scale_at = 0.3, 123.0  # measured against the old layout
    control_with(tmp_path, scan_request=1)
    runner.step()
    assert (runner.scale, runner.scale_at) == (1.0, None)


def test_a_failed_scan_is_reported_without_secrets_and_not_retried(
    make, tmp_path, monkeypatch, caplog
):
    url = "https://sepolia.infura.io/v3/0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("RPC_URL", url)
    io = FakeIO([100.0])
    io.depth_error = OSError(f"429 Too Many Requests for url: {url}")
    saved = tmp_path / "logs" / "pool_profile.json"
    before = v3pool.load_profile()
    runner = make(io, depth=before, depth_file=saved)
    control_with(tmp_path, scan_request=3)
    with caplog.at_level(logging.WARNING):
        runner.step()
        runner.step()
        runner.step()
    assert io.depth_reads == 1  # the button asks again, the bot does not
    assert runner.depth is before and not saved.exists()
    answer = state.load(tmp_path / "logs" / "state.json")["scan"]
    assert answer["done"] == 3 and "429" in answer["error"]
    assert "0123456789abcdef" not in (tmp_path / "logs" / "state.json").read_text()
    assert "0123456789abcdef" not in caplog.text
    # a second click is a new request
    io.depth_error, io.depth_scan = None, scan_result(9)
    control_with(tmp_path, scan_request=4)
    runner.step()
    assert runner.depth["block"] == 9 and saved.exists()


def test_a_scan_that_does_not_fit_this_pool_is_refused(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_scan = {
        **scan_result(9),
        "fee": 500,
    }  # another fee tier: the maths is not for it
    saved = tmp_path / "logs" / "pool_profile.json"
    runner = make(io, depth=v3pool.load_profile(), depth_file=saved)
    control_with(tmp_path, scan_request=1)
    runner.step()
    runner.step()
    assert not saved.exists() and runner.depth["block"] != 9
    assert (
        "does not match"
        in state.load(tmp_path / "logs" / "state.json")["scan"]["error"]
    )


def test_a_scan_with_nowhere_to_save_it_says_so(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_scan = scan_result(9)
    runner = make(io)  # no depth_file
    control_with(tmp_path, scan_request=1)
    runner.step()
    runner.step()
    assert io.depth_reads == 0
    assert (
        "nowhere to save"
        in state.load(tmp_path / "logs" / "state.json")["scan"]["error"]
    )


def test_a_restart_does_not_scan_again_for_a_request_it_already_served(make, tmp_path):
    served = {
        **scan_result(5),
        "request": 5,
    }  # what a scan saves, with the request it answered
    io = FakeIO([100.0])
    io.depth_scan = scan_result(6)
    runner = make(io, depth=served, depth_file=tmp_path / "logs" / "pool_profile.json")
    control_with(tmp_path, scan_request=5)
    runner.step()
    assert io.depth_reads == 0
    control_with(tmp_path, scan_request=6)
    runner.step()
    assert io.depth_reads == 1


def test_the_dashboard_still_gets_the_answer_to_its_scan_after_a_restart(
    make, tmp_path
):
    saved = tmp_path / "logs" / "pool_profile.json"
    io = FakeIO([100.0])
    io.depth_scan = scan_result(777)
    first = make(io, depth_file=saved)
    control_with(tmp_path, scan_request=5)
    first.step()
    first.step()  # the tick after writes the answer to the state file
    io2 = FakeIO([100.0])
    io2.depth_scan = scan_result(778)
    second = make(io2, depth=v3pool.current_profile(saved), depth_file=saved)
    control_with(tmp_path, scan_request=5)
    second.step()
    answer = state.load(tmp_path / "logs" / "state.json")["scan"]
    # without it the page would show the button as waiting for ever (5 is above "no answer")
    assert (answer["done"], answer["block"], answer["error"]) == (5, 777, None)
    assert io2.depth_reads == 0


def test_a_newer_saved_layout_beats_an_older_answer_in_the_state_file(make, tmp_path):
    # scan 5 answered; scan 6 saved its layout, then the bot stopped before the state file was
    # written: the restarted bot must not keep saying 5 (the button would wait for ever)
    state.save(
        tmp_path / "logs" / "state.json",
        time.time(),
        {
            "base": "fETH",
            "quote": "f564",
            "price": 100.0,
            "vault_base": Decimal(0),
            "vault_quote": Decimal(1),
            "operator_eth": Decimal(1),
            "scan": {"done": 5, "block": 1, "at": 1.0, "error": None},
        },
    )
    newer = {**scan_result(777), "request": 6}
    runner = make(FakeIO([100.0]), depth=newer)
    assert runner.scan_info["done"] == 6 and runner.scan_info["block"] == 777
    assert runner.scan_done == 6


def test_a_scan_is_told_to_the_state_file_in_the_same_tick(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_scan = scan_result(777)
    runner = make(io, depth_file=tmp_path / "logs" / "pool_profile.json")
    control_with(tmp_path, scan_request=5)
    runner.step()  # no second tick: a stop right here must not lose the answer
    answer = state.load(tmp_path / "logs" / "state.json")["scan"]
    assert (answer["done"], answer["block"]) == (5, 777)


def test_a_state_file_that_cannot_be_read_does_not_stop_the_bot_starting(
    make, tmp_path
):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "state.json").write_text("{not json")
    assert make(FakeIO([100.0])).scan_info is None
    damaged = {**scan_result(1), "request": "five"}  # no number: no answer from it
    assert make(FakeIO([100.0]), depth=damaged).scan_info is None


def test_the_answer_comes_from_the_saved_layout_when_there_is_no_state_file(
    make, tmp_path
):
    served = {**scan_result(5), "request": 5}
    runner = make(FakeIO([100.0]), depth=served)
    assert runner.scan_info["done"] == 5 and runner.scan_info["block"] == 5
    assert make(FakeIO([100.0])).scan_info is None  # never asked: no answer


def test_a_failed_scan_is_not_retried_after_a_restart(make, tmp_path):
    saved = tmp_path / "logs" / "pool_profile.json"
    io = FakeIO([100.0])
    io.depth_error = OSError("429 Too Many Requests")
    first = make(io, depth=v3pool.load_profile(), depth_file=saved)
    control_with(tmp_path, scan_request=3)
    first.step()
    first.step()  # writes the failure to the state file
    io2 = FakeIO([100.0])
    io2.depth_scan = scan_result(9)
    second = make(io2, depth=v3pool.load_profile(), depth_file=saved)
    control_with(tmp_path, scan_request=3)  # the same request, still in the settings
    second.step()
    assert io2.depth_reads == 0
    assert "429" in state.load(tmp_path / "logs" / "state.json")["scan"]["error"]


def test_a_scan_that_fails_in_an_unexpected_way_never_takes_the_bot_down(
    make, tmp_path
):
    io = FakeIO([100.0])
    io.depth_error = KeyError("ticks")  # not one of the read errors
    runner = make(io, depth_file=tmp_path / "logs" / "pool_profile.json")
    control_with(tmp_path, scan_request=1)
    runner.step()
    runner.step()
    assert "ticks" in state.load(tmp_path / "logs" / "state.json")["scan"]["error"]


class ScanPool:
    # a pool that answers the scan's calls and remembers the block each was made at
    def __init__(self):
        self.blocks, self.calls = [], 0
        self.functions = self

    def _fn(self, value):
        pool = self

        class Call:
            def call(self, block_identifier="latest"):
                pool.calls += 1
                pool.blocks.append(block_identifier)
                return value

        return Call()

    def tickSpacing(self):
        return self._fn(60)

    def slot0(self):
        return self._fn((2**96, 0, 0, 0, 0, 0, True))

    def liquidity(self):
        return self._fn(0)

    def tickBitmap(self, word):
        return self._fn(0)

    def fee(self):
        return self._fn(3000)


def test_every_call_of_a_scan_is_made_at_the_same_block():
    pool = ScanPool()
    layout = config.read_depth(pool, 18, 18, 12345)
    assert layout["block"] == 12345 and pool.calls > 100
    assert set(pool.blocks) == {12345}


def test_a_scan_gives_up_at_a_stop_signal_instead_of_finishing():
    pool = ScanPool()
    with pytest.raises(InterruptedError):
        config.read_depth(pool, 18, 18, 1, stop=lambda: True)
    assert pool.calls == 0
    stops = iter([False, False, True])
    with pytest.raises(InterruptedError):
        config.read_depth(pool, 18, 18, 1, stop=lambda: next(stops))
    assert pool.calls == 2


def test_the_runner_hands_its_stop_signal_to_the_scan(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_scan = scan_result(2)
    runner = make(io, depth_file=tmp_path / "logs" / "pool_profile.json")
    control_with(tmp_path, scan_request=1)
    runner.step()
    assert io.depth_stop == runner.stop.is_set


def test_a_scan_stopped_midway_is_reported_not_retried(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_error = InterruptedError("stopped while reading the pool's layout")
    runner = make(io, depth_file=tmp_path / "logs" / "pool_profile.json")
    control_with(tmp_path, scan_request=1)
    runner.step()
    answer = state.load(tmp_path / "logs" / "state.json")["scan"]
    assert answer["done"] == 1 and "stopped" in answer["error"]
    runner.step()
    assert io.depth_reads == 1


def test_a_call_that_returns_nothing_is_a_read_error_not_a_crash(make, caplog):
    io = FakeIO([100.0])
    io.read_error = BadFunctionCallOutput("Could not transact with/call contract")
    runner = make(io)
    with caplog.at_level(logging.WARNING):
        runner.step()  # backs off and retries like any read error
    assert "read failed" in caplog.text


def test_a_scan_does_not_hold_up_the_sale_decided_in_the_same_tick(make, tmp_path):
    io = FakeIO([100.0])
    io.depth_scan = scan_result(2)
    runner = make(io, depth_file=tmp_path / "logs" / "pool_profile.json")
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, scan_request=1)
    io.prices = [95.0]  # take profit
    runner.step()
    assert [t[0] for t in io.trades] == ["buy", "sell"] and io.depth_reads == 1


def test_the_buy_size_is_the_ceiling_when_risk_sizing_is_off(make):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(500))
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == [("buy", Decimal(500))]


def test_risk_sizing_never_spends_more_than_the_ceiling(make):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(100), risk_pct=Decimal(5))
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    assert io.trades == [("buy", Decimal(100))]


def test_risk_pct_comes_from_the_environment_and_is_checked(monkeypatch):
    monkeypatch.setenv("MAX_TRADE_AMOUNT", "1000")
    monkeypatch.delenv("RISK_PER_TRADE_PCT", raising=False)
    assert bot_main.Settings.from_env().risk_pct == 0
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "2.5")
    assert bot_main.Settings.from_env().risk_pct == Decimal("2.5")
    for bad in ("-1", "101", "nan", "inf", "abc", ""):
        monkeypatch.setenv("RISK_PER_TRADE_PCT", bad)
        with pytest.raises(SystemExit):
            bot_main.Settings.from_env()


def old_position_ledger(tmp_path, minutes):
    # a position the bot bought `minutes` ago, as a restarted bot finds it in its ledger file
    data = ledger.open_position(
        ledger.empty(),
        90.0,
        Decimal("0.0001"),
        Decimal("0.01"),
        time.time() - minutes * 60,
    )
    ledger.save(tmp_path / "logs" / "ledger.json", data)


def test_a_restarted_bot_still_knows_how_old_its_position_is(make, tmp_path):
    old_position_ledger(tmp_path, 16)
    control.save(
        tmp_path / "logs" / "control.json",
        strategy.parse_config(
            {
                "guard_flip_pct": 0,
                "guard_drift_pct": 0,
                "time_stop_on": True,
                "time_stop_min": 15,
            }
        ),
    )
    io = FakeIO([90.5])  # inside the band: only the clock can end it
    runner = make(io, guard=True)
    runner.step()
    assert io.trades == [("sell", Decimal("0.0001"))]
    assert runner.ledger["closed"][0]["reason"] == "time_stop"


def test_an_old_position_is_left_alone_when_the_time_stop_is_off(make, tmp_path):
    old_position_ledger(tmp_path, 600)
    io = FakeIO([90.5])
    runner = make(io)  # the guard is off and the time stop is not ticked
    runner.step()
    assert io.trades == []


def test_the_reason_of_a_sale_is_the_one_the_decision_was_made_for(make):
    # a sale decided by the time stop keeps that reason even if, by the time the swap is recorded,
    # the age worked out again no longer reaches the limit (a clock that moved back)
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    runner.record("sell", 90.5, Decimal("0.0001"), Decimal("0.0100"), "time_stop")
    assert runner.ledger["closed"][0]["reason"] == "time_stop"


def test_a_risk_sized_buy_is_closed_by_selling_exactly_what_it_brought(make):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(500), risk_pct=Decimal(1))
    warm_up(runner, io)
    io.prices = [90.0]
    runner.step()
    held = runner.ledger["open"]
    # what the vault holds after the buy: the base it brought, and the rest of the quote
    io._position = Position(base=float(held["base"]), quote=789.0)
    io.prices = [95.0]  # +5.6%: take profit
    runner.step()
    assert io.trades[0] == ("buy", Decimal("210.526315"))
    assert io.trades[1] == ("sell", Decimal(held["base"]))


def test_a_risk_sized_buy_too_small_to_matter_is_skipped_and_says_why(make, caplog):
    io = FakeIO([100.0], position=Position(base=0.0, quote=0.01))
    runner = make(io, max_trade=Decimal(500), risk_pct=Decimal(1))
    warm_up(runner, io)
    io.prices = [90.0]
    with caplog.at_level(logging.INFO):
        runner.step()
    assert io.trades == []
    assert "below 1.00% of MAX_TRADE_AMOUNT" in caplog.text


def test_a_ledger_with_an_unreadable_opened_time_is_refused(tmp_path):
    for opened in ("yesterday", None, float("nan"), True):
        data = ledger.open_position(ledger.empty(), 90.0, "0.1", "10", 1)
        data["open"]["opened"] = opened
        with pytest.raises(ValueError, match="invalid opened time"):
            ledger.check(data)


# ---------------------------------------------------------------- the sell everything button


def test_sell_everything_sells_the_position_at_any_price_and_answers_in_the_same_tick(
    make, tmp_path
):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io.prices = [91.0]  # +1.1%: no target is near
    runner.step()
    assert [t[0] for t in io.trades] == ["buy"]
    control_with(tmp_path, liquidate_request=5, paused=True)
    runner.step()  # no second tick: a stop right here must not lose the answer
    assert [t[0] for t in io.trades] == ["buy", "sell"]
    assert io.trades[1][1] == Decimal(
        ledger.load(runner.ledger_file)["closed"][0]["base"]
    )
    closed = ledger.load(runner.ledger_file)["closed"][0]
    assert closed["reason"] == "liquidate"
    answer = state.load(tmp_path / "logs" / "state.json")["liquidate"]
    assert answer["done"] == 5 and "sold the open position" in answer["note"]
    runner.step()
    assert len(io.trades) == 2  # served once


def test_a_price_target_hit_at_the_same_moment_is_the_recorded_reason(make, tmp_path):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, liquidate_request=1)
    io.prices = [95.0]  # +5.6%: take profit
    runner.step()
    assert ledger.load(runner.ledger_file)["closed"][0]["reason"] == "take_profit"
    assert runner.liquidate_done == 1


def test_sell_everything_with_no_position_is_answered_and_buys_nothing(
    make, tmp_path, caplog
):
    io = FakeIO([100.0], position=Position(base=0.0, quote=1000.0))
    runner = make(io, max_trade=Decimal(500))
    warm_up(runner, io)
    control_with(
        tmp_path, liquidate_request=7
    )  # not paused: the bot still buys nothing now
    io.prices = [90.0]  # a dip
    with caplog.at_level(logging.INFO):
        runner.step()
    assert io.trades == []
    answer = state.load(tmp_path / "logs" / "state.json")["liquidate"]
    assert answer["done"] == 7 and "nothing to sell" in answer["note"]
    runner.step()  # served: buying works again
    assert [t[0] for t in io.trades] == ["buy"]


def test_a_failed_sale_stays_asked_for_and_is_not_hammered(make, tmp_path):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, liquidate_request=2, paused=True)
    io.trade_error = ValueError("reverted")
    io.prices = [91.0]
    runner.step()
    assert runner.liquidate_done == 0 and runner.ledger["open"] is not None
    attempts = io.attempts
    runner.step()  # inside the cooldown after a failure
    assert io.attempts == attempts
    io.trade_error = None
    runner.test_clock.now += 301
    runner.step()
    assert runner.liquidate_done == 2 and runner.ledger["open"] is None


def test_a_served_sell_everything_request_is_not_served_again_after_a_restart(
    make, tmp_path
):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, liquidate_request=5, paused=True)
    io.prices = [91.0]
    runner.step()
    assert runner.liquidate_done == 5
    # restarted, resumed on the dashboard, and a new position is open: it must not be sold
    io2 = FakeIO([100.0])
    runner2 = make(io2, guard=True)  # (keeps the control file the test wrote)
    control_with(tmp_path, liquidate_request=5)
    assert runner2.liquidate_done == 5
    open_a_position(runner2, io2, 90.0)
    io2.prices = [91.0]
    runner2.step()
    assert [t[0] for t in io2.trades] == ["buy"] and runner2.ledger["open"] is not None


def test_base_in_the_vault_that_the_bot_did_not_buy_is_left_alone_and_said(
    make, tmp_path, caplog
):
    io = FakeIO([100.0])  # the fake vault holds 1.0 base, the position 0.0001
    runner = make(io)
    open_a_position(runner, io, 90.0)
    control_with(tmp_path, liquidate_request=1, paused=True)
    io.prices = [91.0]
    with caplog.at_level(logging.INFO):
        runner.step()
    assert "was not bought by the bot and is left alone" in caplog.text
    assert io.trades[1][1] == Decimal("0.0001")  # only the recorded base was sold


def test_an_old_request_is_not_served_to_a_position_bought_after_it(
    make, tmp_path, caplog
):
    # the state file is gone (or damaged), so the bot cannot know request 5 was served long ago
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    opened = runner.ledger["open"]["opened"]
    (tmp_path / "logs" / "state.json").write_text("{not json")
    control_with(tmp_path, liquidate_request=int(opened) - 1000)
    with caplog.at_level(logging.INFO):
        runner2 = make(FakeIO([91.0]), guard=True)
    assert runner2.liquidate_done == int(opened) - 1000
    assert "older than the open position" in caplog.text
    runner2.step()
    assert runner2.io.trades == [] and runner2.ledger["open"] is not None


def test_a_request_made_after_the_position_was_bought_is_served_after_a_restart(
    make, tmp_path
):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    opened = runner.ledger["open"]["opened"]
    (tmp_path / "logs" / "state.json").unlink()
    control_with(tmp_path, liquidate_request=int(opened) + 1000, paused=True)
    runner2 = make(FakeIO([91.0]), guard=True)
    assert runner2.liquidate_done == 0
    runner2.step()
    assert [t[0] for t in runner2.io.trades] == ["sell"]


def test_a_sale_skipped_for_a_low_vault_balance_is_a_warning(make, tmp_path, caplog):
    io = FakeIO([100.0])
    runner = make(io)
    open_a_position(runner, io, 90.0)
    io._position = Position(base=0.0, quote=1.0)  # the base is not in the vault
    control_with(tmp_path, liquidate_request=1, paused=True)
    with caplog.at_level(logging.INFO):
        runner.step()
    records = [r for r in caplog.records if "sell skipped: vault balance" in r.message]
    assert records and records[-1].levelno == logging.WARNING
    assert runner.ledger["open"] is not None and runner.liquidate_done == 0


# ---------------------------------------------------------------- the fast poll window


def test_poll_delay_is_the_normal_pace_when_the_fast_poll_is_off():
    assert bot_main.poll_delay(10, 0, 1_700_000_040) == 10
    assert bot_main.poll_delay(10, 10, 1_700_000_040) == 10  # not faster: off


def test_poll_delay_speeds_up_inside_the_window_and_lands_on_its_start():
    start, end = bot_main.FAST_POLL_WINDOW
    minute = 1_700_000_040 - 1_700_000_040 % 60
    assert bot_main.poll_delay(10, 3, minute + start) == 3
    assert bot_main.poll_delay(10, 3, minute + end - 1) == 3
    assert bot_main.poll_delay(10, 3, minute + end) == 10  # after the window
    assert bot_main.poll_delay(10, 3, minute + start - 4) == 4  # wake at its start
    assert bot_main.poll_delay(10, 3, minute + 5) == 10  # far from it
    # just before the minute turns the next window is still 30+ s away: the normal pace
    assert bot_main.poll_delay(10, 3, minute + 59) == 10


def test_settings_read_the_fast_poll_and_the_shorter_cooldown_from_the_environment(
    monkeypatch,
):
    monkeypatch.setenv("MAX_TRADE_AMOUNT", "1000")
    monkeypatch.delenv("COOLDOWN_SECONDS", raising=False)
    monkeypatch.delenv("FAST_POLL_SECONDS", raising=False)
    s = bot_main.Settings.from_env()
    assert s.cooldown_seconds == 60 and s.fast_poll_seconds == 0
    monkeypatch.setenv("FAST_POLL_SECONDS", "3")
    assert bot_main.Settings.from_env().fast_poll_seconds == 3
    for bad in ("-1", "61"):
        monkeypatch.setenv("FAST_POLL_SECONDS", bad)
        with pytest.raises(SystemExit, match="FAST_POLL_SECONDS"):
            bot_main.Settings.from_env()


def test_the_run_loop_waits_by_the_fast_poll_window(make, monkeypatch):
    io = FakeIO([100.0])
    runner = make(io, poll_seconds=10, fast_poll_seconds=3)
    waits = []
    monkeypatch.setattr(runner.stop, "wait", lambda delay: waits.append(delay))
    start, _ = bot_main.FAST_POLL_WINDOW
    monkeypatch.setattr(
        bot_main.time, "time", lambda: 1_700_000_040 - 1_700_000_040 % 60 + start
    )
    runner.run(max_steps=2)
    assert waits == [3]
