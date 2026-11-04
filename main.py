import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

import bot_contract
import config
import control
import history
import ledger
import strategy

ROOT = Path(__file__).parent
LOG_DIR = ledger.LOG_DIR
HALT_FILE = LOG_DIR / "HALTED"
HEARTBEAT_FILE = LOG_DIR / "heartbeat"
MAX_BACKOFF_SECONDS = 600
# the vault's float balance can differ from an exact recorded amount in the last digits
BALANCE_TOLERANCE = Decimal("0.999999")
PRELOAD_MAX_AGE = (
    600  # seconds: a price history older than this is not carried over a restart
)

log = logging.getLogger("bot")

# transient read problems: RPC down, rate limited, timeouts. Anything else is a bug: let it crash
# so the supervisor restarts the process and the traceback is in the log.
READ_ERRORS = (OSError, Web3RPCError, ContractLogicError)
# a swap that was refused or reverted: counted toward the kill switch
TRADE_ERRORS = (ContractLogicError, Web3RPCError, RuntimeError, ValueError)


def redact(text):
    # ------------------------------------
    # Errors from a failed request can contain the full RPC URL, and an Infura URL carries the
    # API key. Keep secrets out of logs/bot.log, the journal and logs/HALTED.
    # ------------------------------------

    secrets = []
    rpc_url = os.environ.get("RPC_URL", "")
    if rpc_url:
        secrets += [
            rpc_url,
            rpc_url.rstrip("/").rsplit("/", 1)[-1],
        ]  # URL, then its key segment
    secrets.append(os.environ.get("PRIVATE_KEY", ""))
    for secret in secrets:
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class Halt(Exception):
    """Unrecoverable: stop trading until a human looks (writes logs/HALTED)."""


class Skip(Exception):
    """A trade was not attempted for a benign reason (not a failure)."""


@dataclass(frozen=True)
class Settings:
    poll_seconds: int
    cooldown_seconds: int
    min_operator_eth: Decimal
    max_trade_failures: int
    max_trade: Decimal
    dry_run: bool

    @classmethod
    def from_env(cls):
        return cls(
            poll_seconds=int(os.environ.get("POLL_SECONDS", "30")),
            cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS", "300")),
            min_operator_eth=Decimal(os.environ.get("MIN_OPERATOR_ETH", "0.01")),
            max_trade_failures=int(os.environ.get("MAX_TRADE_FAILURES", "3")),
            max_trade=Decimal(os.environ["MAX_TRADE_AMOUNT"]),
            dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
        )


TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)")


def received_from_receipt(receipt, token_address, vault_address):
    # ------------------------------------
    # How much of a token the vault received in a transaction, read from the receipt's ERC-20
    # Transfer logs. Unlike a before/after balance this cannot be thrown off by a lagging RPC
    # node, or by someone else depositing into the vault at the same moment.
    # ------------------------------------

    token, vault = token_address.lower(), vault_address.lower()
    total = 0
    for entry in receipt["logs"]:
        topics = entry["topics"]
        if (
            entry["address"].lower() == token
            and len(topics) == 3
            and bytes(topics[0]) == TRANSFER_TOPIC
            and "0x" + bytes(topics[2])[-20:].hex() == vault
        ):
            total += int.from_bytes(bytes(entry["data"]), "big")
    return total


class ChainIO:
    # ------------------------------------
    # Everything that touches the chain, so Runner can be tested with a fake.
    # "base" is the pool's token0 and "quote" is token1; price = quote per base.
    # ------------------------------------

    def __init__(self, w3, account, bot, market, settings):
        self.w3, self.account, self.bot = w3, account, bot
        self.market, self.settings = market, settings
        self.pool = market["pool"]

        token0 = self.pool.functions.token0().call().lower()
        by_address = {t[0].lower(): t for t in market["tokens"].values()}
        self.base = by_address[token0]
        self.quote = next(t for a, t in by_address.items() if a != token0)
        self.token_in = {"buy": self.quote, "sell": self.base}

    def read_price(self):
        sqrt_price_x96 = self.pool.functions.slot0().call()[0]
        decimals0, decimals1 = self.base[1], self.quote[1]
        return (sqrt_price_x96 / 2**96) ** 2 * 10 ** (decimals0 - decimals1)

    def raw_balance(self, token):
        erc20 = bot_contract.token_contract(self.w3, token[0])
        return erc20.functions.balanceOf(self.bot.address).call()

    def position(self):
        return strategy.Position(
            base=self.raw_balance(self.base) / 10 ** self.base[1],
            quote=self.raw_balance(self.quote) / 10 ** self.quote[1],
        )

    def operator_eth(self):
        return Decimal(self.w3.eth.get_balance(self.account.address)) / 10**18

    def trade(self, signal_name, amount):
        # Returns how much of the OTHER token arrived, in human units (Decimal). The runner
        # records it: it is the size of the position a later sell must close.
        symbol_in = self.token_in[signal_name][2]
        token_out = self.base if signal_name == "buy" else self.quote
        try:
            quote = config.get_quote(self.market, symbol_in, amount)
            if quote["too_large"]:
                raise Skip("price impact too large")
            if signal_name == "buy":
                self.check_exit_fits(quote, token_out)
        except OSError as e:  # nothing sent yet, so a connection blip is not a halt
            raise bot_contract.NotSent(f"could not read the chain: {e}") from e
        receipt = bot_contract.swap_via_contract(
            self.w3,
            self.account,
            self.bot,
            quote,
            self.market["fee"],
            self.settings.dry_run,
        )
        if self.settings.dry_run:
            received = quote["amount_out"]  # nothing moved: the quoted output stands in
        else:
            received = received_from_receipt(receipt, token_out[0], self.bot.address)
        return Decimal(received) / 10 ** token_out[1]

    def check_exit_fits(self, quote, token_out):
        # The later sell trades exactly what this buy returns, and the contract refuses any swap
        # above its per-swap cap. If the base returned could exceed it (a pool where base is
        # cheaper than quote), the position could never be closed: so do not open it.
        cap = self.bot.functions.tokenCap(Web3.to_checksum_address(token_out[0])).call()
        expected = quote["amount_out"] * (10_000 + config.SLIPPAGE_BPS) // 10_000
        if expected > cap:
            raise Skip(
                "this buy would return more base than the contract's per-swap cap, "
                "so the sell could not close it"
            )


class Runner:
    def __init__(
        self,
        io,
        settings,
        stop,
        clock=time.monotonic,
        halt_file=HALT_FILE,
        heartbeat_file=HEARTBEAT_FILE,
        ledger_file=None,
        control_file=None,
        history_file=None,
    ):
        self.io, self.settings, self.stop, self.clock = io, settings, stop, clock
        self.halt_file, self.heartbeat_file = halt_file, heartbeat_file
        self.ledger_file = ledger_file or ledger.file_for(settings.dry_run)
        try:
            self.ledger = ledger.load(self.ledger_file)
            self.ledger_error = None
        except ValueError as e:
            self.ledger, self.ledger_error = ledger.empty(), str(e)
        self.prices = deque(maxlen=strategy.HISTORY)
        self.guard_note = None  # why buying is paused right now, for the log
        # settings a person can change while the bot runs (the dashboard writes this file)
        self.control_file, self.history_file = control_file, history_file
        self.cfg = strategy.DEFAULT
        self.control_stamp = None
        self.reload_config(starting=True)
        self.preload_prices()
        self.last_trade = None
        self.read_failures = 0
        self.trade_failures = 0

    def preload_prices(self):
        # After a restart, take the price history from the file so the pattern guard (which needs
        # hours of prices) does not start from nothing. Only if the file is fresh: after a longer
        # gap the old prices are a different market.
        if self.history_file is None:
            return
        points = history.tail(self.history_file, strategy.HISTORY)
        # (the history file is stamped with wall-clock time, not the runner's monotonic clock)
        limit = max(PRELOAD_MAX_AGE, 5 * self.settings.poll_seconds)
        restored = history.trailing_run(points, time.time(), limit)
        if restored:
            self.prices.extend(restored)
            log.info("restored %d price samples from the history file", len(restored))

    def reload_config(self, starting=False):
        # Picked up on the next tick, no restart. A file that is invalid keeps the settings in
        # force (and says so in the log) instead of stopping the bot or reverting to defaults.
        # At start-up there are no earlier settings, so the defaults are what is in force.
        if self.control_file is None:
            return
        try:
            stat = self.control_file.stat()
        except OSError:
            return  # no file yet: the defaults stand
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self.control_stamp:
            return
        self.control_stamp = stamp
        try:
            new = control.load(self.control_file)
        except (ValueError, OSError) as e:
            log.error(
                "ignoring the settings file, %s: %s",
                "using the defaults until it is fixed"
                if starting
                else "keeping the current ones",
                e,
            )
            return
        if new != self.cfg:
            log.info("settings changed: %s", strategy.to_dict(new))
            self.cfg = new

    def step(self):
        # ------------------------------------
        # One tick: read -> decide -> (maybe) trade. Raises Halt on anything unrecoverable.
        # ------------------------------------

        self.reload_config()
        try:
            price = self.io.read_price()
            position = self.io.position()
        except READ_ERRORS as e:
            self.read_failures += 1
            log.warning("read failed (%d in a row): %s", self.read_failures, e)
            return
        self.read_failures = 0

        held = self.ledger["open"]
        entry_price = held["entry_price"] if held else None
        position = replace(position, entry_price=entry_price)

        self.prices.append(price)
        if self.history_file is not None:
            try:
                history.append(self.history_file, time.time(), price)
            except OSError as e:
                log.warning("could not write the price history: %s", e)
        prices = list(self.prices)
        decision = strategy.decide(prices, position, self.cfg)
        self.note_guard(prices, position)
        log.info(
            "strategy=%s price=%.6f decision=%s base=%.6f quote=%.6f samples=%d%s",
            self.cfg.strategy,
            price,
            decision,
            position.base,
            position.quote,
            len(self.prices),
            f" entry={entry_price:.6f}" if held else "",
        )
        if decision != "hold":
            self.trade(decision, position, price)

    def note_guard(self, prices, position):
        # Log only when buying is paused or allowed again, and when the CHECK that pauses it
        # changes. The reason text carries live numbers, so comparing it would log every tick.
        if position.entry_price is not None or position.quote <= 0:
            return  # holding something, or nothing to buy with: a pause would mean nothing
        status = strategy.guard_status(prices, self.cfg)
        if status["kind"] != self.guard_note:
            if status["reason"]:
                log.warning("new buys are paused: %s", status["reason"])
            else:
                log.info("new buys are allowed again")
            self.guard_note = status["kind"]

    def trade(self, decision, position, price):
        s = self.settings
        now = self.clock()
        cooling = (
            self.last_trade is not None and now - self.last_trade < s.cooldown_seconds
        )
        # An exit may follow a successful buy at once (a stop loss must not sit out a cooldown),
        # but after a failed attempt it waits like any trade, so a broken swap is not hammered.
        if cooling and (decision == "buy" or self.trade_failures):
            log.info("%s skipped: cooldown", decision)
            return

        if decision == "buy":
            amount, balance = s.max_trade, position.quote
        else:
            # close exactly what the buy brought in, not the (much larger) base-token trade size
            amount, balance = Decimal(self.ledger["open"]["base"]), position.base
        if Decimal(str(balance)) < amount * BALANCE_TOLERANCE:
            log.info(
                "%s skipped: vault balance %s < trade size %s",
                decision,
                balance,
                amount,
            )
            return

        try:
            eth = self.io.operator_eth()
        except READ_ERRORS as e:
            log.warning(
                "%s skipped: could not read the operator's ETH: %s", decision, e
            )
            return
        if eth < s.min_operator_eth:
            log.warning(
                "%s skipped: operator ETH %s below %s, refill it",
                decision,
                eth,
                s.min_operator_eth,
            )
            return

        self.last_trade = now  # also after a failure, so a broken trade is not hammered
        try:
            received = self.io.trade(decision, amount)
        except Skip as e:
            # a sell that keeps being skipped is a position that cannot close: not a quiet line
            level = logging.WARNING if decision == "sell" else logging.INFO
            log.log(level, "%s skipped: %s", decision, e)
            return
        except bot_contract.NotSent as e:
            log.warning("%s not sent, nothing happened on chain: %s", decision, e)
            return
        except bot_contract.SentUnknown as e:
            raise Halt(
                "the swap may be on chain but its outcome is unknown: check the operator "
                f"wallet's transactions on Etherscan before restarting ({e})"
            ) from e
        except TimeExhausted as e:
            raise Halt(
                "no receipt before the timeout, so the swap may still land: check the operator "
                f"wallet's pending transactions on Etherscan before restarting ({e})"
            ) from e
        except OSError as e:
            if s.dry_run:
                self.trade_failed(decision, e)
                return
            # the connection may have dropped after the transaction was sent
            raise Halt(
                f"connection error while trading, the swap may have been sent: {e}"
            ) from e
        except TRADE_ERRORS as e:
            self.trade_failed(decision, e)
            return

        self.trade_failures = 0
        log.info("%s done%s", decision, " (dry run, nothing sent)" if s.dry_run else "")
        self.record(decision, price, amount, received)

    def record(self, decision, price, amount, received):
        # ------------------------------------
        # Remember the position, so TP/SL survive a restart. Saved at once: a trade that landed
        # but was not written down would leave base in the vault that nothing ever sells.
        # ------------------------------------

        if received <= 0:
            raise Halt(
                f"{decision} went through but the vault received nothing: check the vault and "
                "the operator wallet on Etherscan before restarting"
            )
        if decision == "buy":
            self.ledger = ledger.open_position(
                self.ledger, price, received, amount, time.time()
            )
            log.info(
                "opened position: %s base for %s quote at %.6f", received, amount, price
            )
        else:
            reason = strategy.exit_reason(
                price, self.ledger["open"]["entry_price"], self.cfg
            )
            self.ledger = ledger.close_position(
                self.ledger, price, received, reason, time.time()
            )
            trade = self.ledger["closed"][-1]
            log.info(
                "closed position (%s) at %.6f: P/L %s quote",
                reason,
                price,
                ledger.profit(trade),
            )
        try:
            ledger.save(self.ledger_file, self.ledger)
        except OSError as e:
            raise Halt(
                f"the {decision} landed but the ledger could not be saved ({e}). By hand: "
                f"{decision} at {price}, received {received}, spent {amount}. Free up the disk, "
                f"then fix {self.ledger_file.name}, or sell/withdraw the tokens"
            ) from e

    def trade_failed(self, decision, error):
        self.trade_failures += 1
        log.error("%s failed (%d in a row): %s", decision, self.trade_failures, error)
        if self.trade_failures >= self.settings.max_trade_failures:
            raise Halt(f"{self.trade_failures} failed trades in a row, last: {error}")

    def halt(self, reason):
        log.critical("HALTING: %s", reason)
        self.halt_file.parent.mkdir(exist_ok=True)
        self.halt_file.write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} {redact(reason)}\n"
        )

    def run(self, max_steps=None):
        if self.ledger_error:
            self.halt(self.ledger_error)
            return
        steps = 0
        while not self.stop.is_set():
            try:
                self.step()
            except Halt as h:
                self.halt(str(h))
                return
            self.heartbeat_file.parent.mkdir(exist_ok=True)
            self.heartbeat_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))

            steps += 1
            if max_steps is not None and steps >= max_steps:
                return
            # back off while the RPC is failing, otherwise poll at the normal pace
            delay = self.settings.poll_seconds
            if self.read_failures:
                delay = min(delay * 2**self.read_failures, MAX_BACKOFF_SECONDS)
            self.stop.wait(delay)


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=3),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def main(ticks=None):
    load_dotenv()
    setup_logging()

    if HALT_FILE.exists():
        log.error(
            "Not starting: %s exists.\n%s", HALT_FILE, HALT_FILE.read_text().strip()
        )
        log.error("Fix the cause, then delete the file to resume.")
        return

    settings = Settings.from_env()
    w3, account = bot_contract.connect()
    bot = bot_contract.load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])
    if bot.functions.operator().call() != account.address:
        raise SystemExit("PRIVATE_KEY is not the bot contract's operator")

    market = config.load_market()
    io = ChainIO(w3, account, bot, market, settings)

    log.info(
        "Started. contract=%s operator=%s dry_run=%s",
        bot.address,
        account.address,
        settings.dry_run,
    )
    log.info(
        "poll=%ss cooldown=%ss trade size=%s min ETH=%s",
        settings.poll_seconds,
        settings.cooldown_seconds,
        settings.max_trade,
        settings.min_operator_eth,
    )

    stop = threading.Event()
    # a signal only sets the flag: the current tick (including a swap waiting for its
    # receipt) finishes first, then the loop exits
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    runner = Runner(
        io,
        settings,
        stop,
        control_file=control.CONTROL_FILE,
        history_file=history.HISTORY_FILE,
    )
    cfg = runner.cfg
    log.info(
        "strategy=%s take profit +%g%% stop loss -%g%% (change it on the dashboard or in %s)",
        cfg.strategy,
        cfg.take_profit_pct,
        cfg.stop_loss_pct,
        control.CONTROL_FILE.name,
    )
    held = runner.ledger["open"]
    if held:
        log.info(
            "open position from %s: %s base bought at %s",
            runner.ledger_file.name,
            held["base"],
            held["entry_price"],
        )
    runner.run(max_steps=ticks)
    log.info("Stopped.")


def cli():
    # usage: python main.py [--ticks N]   (N ticks then exit: a smoke test against the real chain)
    args = sys.argv[1:]
    ticks = None
    if (
        len(args) == 2
        and args[0] == "--ticks"
        and args[1].isdigit()
        and int(args[1]) > 0
    ):
        ticks = int(args[1])
    elif args:
        raise SystemExit("usage: main.py [--ticks N]")

    # an unhandled error is logged through the redacting formatter (a raw traceback could print
    # the RPC URL), then exits non-zero so the supervisor restarts the process
    try:
        main(ticks)
    except Exception:
        log.exception("crashed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
