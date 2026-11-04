import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from web3.exceptions import ContractLogicError, TimeExhausted, Web3RPCError

import bot_contract
import config
import strategy

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
HALT_FILE = LOG_DIR / "HALTED"
HEARTBEAT_FILE = LOG_DIR / "heartbeat"
MAX_BACKOFF_SECONDS = 600

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

    def position(self):
        def balance(token):
            address, decimals, _ = token
            erc20 = bot_contract.token_contract(self.w3, address)
            return erc20.functions.balanceOf(self.bot.address).call() / 10**decimals

        return strategy.Position(base=balance(self.base), quote=balance(self.quote))

    def operator_eth(self):
        return Decimal(self.w3.eth.get_balance(self.account.address)) / 10**18

    def trade(self, signal_name, amount):
        symbol_in = self.token_in[signal_name][2]
        quote = config.get_quote(self.market, symbol_in, amount)
        if quote["too_large"]:
            raise Skip("price impact too large")
        bot_contract.swap_via_contract(
            self.w3,
            self.account,
            self.bot,
            quote,
            self.market["fee"],
            self.settings.dry_run,
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
    ):
        self.io, self.settings, self.stop, self.clock = io, settings, stop, clock
        self.halt_file, self.heartbeat_file = halt_file, heartbeat_file
        self.prices = deque(maxlen=strategy.WINDOW * 5)
        self.last_trade = None
        self.read_failures = 0
        self.trade_failures = 0

    def step(self):
        # ------------------------------------
        # One tick: read -> decide -> (maybe) trade. Raises Halt on anything unrecoverable.
        # ------------------------------------

        try:
            price = self.io.read_price()
            position = self.io.position()
        except READ_ERRORS as e:
            self.read_failures += 1
            log.warning("read failed (%d in a row): %s", self.read_failures, e)
            return
        self.read_failures = 0

        self.prices.append(price)
        decision = strategy.decide(list(self.prices), position)
        log.info(
            "price=%.6f decision=%s base=%.6f quote=%.6f samples=%d",
            price,
            decision,
            position.base,
            position.quote,
            len(self.prices),
        )
        if decision != "hold":
            self.trade(decision, position)

    def trade(self, decision, position):
        s = self.settings
        now = self.clock()
        if self.last_trade is not None and now - self.last_trade < s.cooldown_seconds:
            log.info("%s skipped: cooldown", decision)
            return

        balance = position.quote if decision == "buy" else position.base
        if Decimal(str(balance)) < s.max_trade:
            log.info(
                "%s skipped: vault balance %s < trade size %s",
                decision,
                balance,
                s.max_trade,
            )
            return

        eth = self.io.operator_eth()
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
            self.io.trade(decision, s.max_trade)
        except Skip as e:
            log.info("%s skipped: %s", decision, e)
            return
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

    Runner(io, settings, stop).run(max_steps=ticks)
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
