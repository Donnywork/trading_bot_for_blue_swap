import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import partial
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
import state
import strategy
import v3pool

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
# the operator's gas only changes when a trade is sent, so the dashboard's copy of it is re-read
# now and then (and after a trade), not every tick
OPERATOR_ETH_REFRESH = 300
# a buy sized by risk below this share of MAX_TRADE_AMOUNT is not worth its gas: skip it
MIN_BUY_SHARE = Decimal("0.01")
# How deep the pool is (what a buy of a given size costs) is ONE read of its liquidity, made when a
# buy is being sized and at most this often (seconds)
DEPTH_MAX_AGE = 300
# A whole scan of the pool (the dashboard's button) is about 120 reads: this many seconds apart, as
# the research's own scan did, so a rate-limited RPC (HTTP 429) is not hit with a burst
DEPTH_CALL_PAUSE = 0.2

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
    # share of the vault's quote one stop loss may cost; 0 = every buy is `max_trade`
    risk_pct: Decimal = Decimal(0)

    @classmethod
    def from_env(cls):
        try:
            risk_pct = Decimal(os.environ.get("RISK_PER_TRADE_PCT", "0"))
        except InvalidOperation:
            risk_pct = Decimal("nan")
        if not risk_pct.is_finite() or not 0 <= risk_pct <= 100:
            raise SystemExit(
                "RISK_PER_TRADE_PCT must be between 0 and 100 (0 = every buy is MAX_TRADE_AMOUNT)"
            )
        return cls(
            poll_seconds=int(os.environ.get("POLL_SECONDS", "30")),
            cooldown_seconds=int(os.environ.get("COOLDOWN_SECONDS", "300")),
            min_operator_eth=Decimal(os.environ.get("MIN_OPERATOR_ETH", "0.01")),
            max_trade_failures=int(os.environ.get("MAX_TRADE_FAILURES", "3")),
            max_trade=Decimal(os.environ["MAX_TRADE_AMOUNT"]),
            dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
            risk_pct=risk_pct,
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
        self.symbols = (self.base[2], self.quote[2])

    def read_price(self):
        sqrt_price_x96 = self.pool.functions.slot0().call()[0]
        decimals0, decimals1 = self.base[1], self.quote[1]
        return (sqrt_price_x96 / 2**96) ** 2 * 10 ** (decimals0 - decimals1)

    def read_depth(self):
        # the pool's whole liquidity layout (about 120 read-only calls, a pause between them)
        return config.read_depth(
            self.pool,
            self.base[1],
            self.quote[1],
            self.w3.eth.block_number,
            DEPTH_CALL_PAUSE,
        )

    def read_liquidity(self):
        # how much liquidity the pool has in range right now: one read-only call, like the price
        return self.pool.functions.liquidity().call()

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
        state_file=None,
        depth=None,
        depth_file=None,
    ):
        self.io, self.settings, self.stop, self.clock = io, settings, stop, clock
        # the pool's saved liquidity layout (v3pool), to size a buy by its real cost; None sizes by
        # the fixed strategy.FRICTION_PCT. The layout is scaled to how deep the pool is now (one
        # read of its liquidity, when a buy is sized, at most every DEPTH_MAX_AGE): `scale` and
        # when it was read. The dashboard gets the scale through the state file.
        self.depth = depth
        self.scale, self.scale_at = 1.0, None
        # The dashboard's "scan the pool" button: a new layout is read when its request number
        # (in the settings file) is above the last one served. A scan saves the layout in
        # `depth_file`, together with the request it answered, so a restart does not scan again.
        self.depth_file = depth_file
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
        # what the dashboard shows of the chain: it reads this file instead of asking the RPC
        self.state_file = state_file
        # the last answer to the scan button, for the dashboard (the state file); kept over a restart
        self.scan_info = self.restored_scan()
        self.scan_done = max(
            (depth or {}).get("request", 0), (self.scan_info or {}).get("done", 0)
        )
        self.eth, self.eth_at = None, None
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
        if new.paused != self.cfg.paused:
            if new.paused:
                log.warning(
                    "new buys are PAUSED from the dashboard (a sale still runs)"
                )
            else:
                log.info("new buys are resumed from the dashboard")
        if new != self.cfg:
            log.info("settings changed: %s", strategy.to_dict(new))
            self.cfg = new

    def restored_scan(self):
        # What the last run answered to the scan button. The state file has it, a failed scan
        # included (so a restart does not retry one by itself); without it, the saved layout
        # still knows the request it answered. Without either the button is simply unanswered.
        try:
            saved = state.load(self.state_file) if self.state_file else None
        except ValueError:
            saved = None
        if saved and saved["scan"]:
            return saved["scan"]
        request = (self.depth or {}).get("request", 0)
        if request:
            return {
                "done": request,
                "block": self.depth.get("block"),
                "at": None,
                "error": None,
            }
        return None

    def serve_scan_request(self):
        # ------------------------------------
        # The dashboard's "scan the pool's liquidity" button: read the pool's whole layout (about
        # 120 read-only calls, 30 to 40 s) and keep it from now on. Done at the end of a tick, so a
        # sale decided in this tick is not held up by it. A failed scan is reported to the dashboard
        # and not retried by itself (the button asks again); it never halts: it is a read.
        # ------------------------------------

        request = self.cfg.scan_request
        if request <= self.scan_done:
            return
        self.scan_done = request
        log.info(
            "scanning the pool's liquidity (asked on the dashboard): about 120 reads"
        )
        try:
            if self.depth_file is None:
                raise ValueError("nowhere to save the layout")
            profile = v3pool.check_profile(self.io.read_depth(), "the scanned pool")
            profile = {**profile, "request": request}
            v3pool.save_profile(self.depth_file, profile)
        except Exception as e:  # noqa: BLE001
            # (a scan is only a read: whatever goes wrong must not take the bot down; and an
            # error from a failed request can carry the RPC URL and its key)
            self.scan_info = {"done": request, "error": redact(str(e))[:300]}
            log.warning("the pool scan failed: %s", redact(str(e)))
            return
        self.depth = profile
        self.scale, self.scale_at = (
            1.0,
            None,
        )  # measured against the layout in use: read afresh
        self.scan_info = {
            "done": request,
            "block": profile["block"],
            "at": time.time(),
            "error": None,
        }
        log.info(
            "pool scanned at block %s: %d ticks",
            profile["block"],
            len(profile["ticks"]),
        )

    def pool_now(self, price):
        # ------------------------------------
        # The saved liquidity layout, scaled to how deep the pool is now. Liquidity providers add
        # and pull liquidity, which changes what a buy costs. One read of the pool's liquidity,
        # only when a buy is being sized and at most every DEPTH_MAX_AGE; a read error propagates
        # (the caller skips the buy: a read, never a halt).
        # ------------------------------------

        now = self.clock()
        if self.scale_at is None or now - self.scale_at >= DEPTH_MAX_AGE:
            live = self.io.read_liquidity()
            self.scale = v3pool.depth_scale(self.depth, price, live)
            self.scale_at = now
            log.info(
                "pool depth: %.0f%% of the saved layout (liquidity %s in range)",
                self.scale * 100,
                live,
            )
        return v3pool.scaled(self.depth, self.scale)

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
        # (the ledger is stamped with wall-clock time, so the age is too)
        held_seconds = time.time() - held["opened"] if held else None
        position = replace(position, entry_price=entry_price, held_seconds=held_seconds)

        self.prices.append(price)
        if self.history_file is not None:
            try:
                history.append(self.history_file, time.time(), price)
            except OSError as e:
                log.warning("could not write the price history: %s", e)
        self.write_state(price, position)
        prices = list(self.prices)
        decision = strategy.decide(prices, position, self.cfg)
        if decision == "buy" and self.cfg.paused:
            decision = "hold"  # paused on the dashboard: only a new buy waits, a sale never does
        self.note_guard(prices, position)
        log.info(
            "strategy=%s price=%.6f decision=%s base=%.6f quote=%.6f samples=%d%s",
            self.cfg.strategy,
            price,
            decision,
            position.base,
            position.quote,
            len(self.prices),
            (f" entry={entry_price:.6f}" if held else "")
            + (" PAUSED" if self.cfg.paused else ""),
        )
        if decision != "hold":
            # why a sale is made is settled now, from the same price and age the decision used: the
            # ledger keeps this reason, not one worked out again after the swap (a clock that moved
            # in between could no longer match it)
            reason = (
                strategy.exit_reason(
                    price, entry_price, self.cfg, position.held_seconds
                )
                if decision == "sell"
                else None
            )
            self.trade(decision, position, price, reason)
        self.serve_scan_request()

    def write_state(self, price, position):
        # Only for the dashboard: a failure here is a warning, never a reason to skip the tick.
        if self.state_file is None:
            return
        now = self.clock()
        try:
            if self.eth_at is None or now - self.eth_at >= OPERATOR_ETH_REFRESH:
                self.eth, self.eth_at = self.io.operator_eth(), now
            base, quote = self.io.symbols
            chain = {
                "base": base,
                "quote": quote,
                "price": price,
                "vault_base": Decimal(str(position.base)),
                "vault_quote": Decimal(str(position.quote)),
                "operator_eth": self.eth,
            }
            if self.scale_at is not None:
                # how deep the pool was when a buy was last sized, for the dashboard's estimates
                chain["depth_scale"] = self.scale
            if self.scan_info is not None:
                chain["scan"] = self.scan_info
            state.save(self.state_file, time.time(), chain)
        except (*READ_ERRORS, OSError) as e:
            log.warning("could not update the dashboard's state file: %s", e)

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

    def trade(self, decision, position, price, reason=None):
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
            try:
                amount, balance = self.buy_size(position, price), position.quote
            except READ_ERRORS as e:
                log.warning("buy skipped: could not read the pool's liquidity: %s", e)
                return
            except ValueError as e:
                # (the read worked; the saved layout holds nothing at this price, or the pool is dry)
                log.warning("buy skipped: the pool's depth cannot be sized: %s", e)
                return
            if s.risk_pct > 0 and amount < s.max_trade * MIN_BUY_SHARE:
                log.info(
                    "buy skipped: the risk-sized buy %s is below %s%% of MAX_TRADE_AMOUNT (%s), "
                    "too small to be worth the gas",
                    amount,
                    MIN_BUY_SHARE * 100,
                    s.max_trade,
                )
                return
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
        self.eth_at = None  # gas changed: re-read it
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
        self.record(decision, price, amount, received, reason)

    def buy_size(self, position, price):
        # MAX_TRADE_AMOUNT is the ceiling (the contract's cap is the same number or higher); with
        # RISK_PER_TRADE_PCT set, a buy is smaller when the vault is small or the pool is thin at
        # this size, so a stop loss costs about that share of the vault
        s = self.settings
        loss_at_stop = None
        if self.depth is not None and s.risk_pct > 0:
            loss_at_stop = partial(
                v3pool.loss_at_stop,
                self.pool_now(price),
                price,
                self.cfg.stop_loss_pct,
            )
        size = strategy.trade_size(
            Decimal(str(position.quote)),
            s.max_trade,
            s.risk_pct,
            self.cfg.stop_loss_pct,
            loss_at_stop,
        )
        if size < s.max_trade:
            log.info(
                "buy size %s (ceiling %s): RISK_PER_TRADE_PCT=%s%% of the vault's %s quote, "
                "with a %g%% stop loss",
                size,
                s.max_trade,
                s.risk_pct,
                position.quote,
                self.cfg.stop_loss_pct,
            )
        return size

    def record(self, decision, price, amount, received, reason=None):
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
            opened = self.ledger["open"]
            reason = reason or strategy.exit_reason(
                price, opened["entry_price"], self.cfg, time.time() - opened["opened"]
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
    # the layout the dashboard's scan button last saved, else the one that ships with the code
    try:
        depth = v3pool.current_profile()
    except (OSError, ValueError) as e:
        depth = None
        if settings.risk_pct > 0:
            # a risk-sized buy needs the pool's real costs: without them, refuse rather than size
            # with a number that is only right for small buys
            raise SystemExit(
                f"RISK_PER_TRADE_PCT needs {v3pool.PROFILE_FILE.name}: {e}"
            ) from e
    if settings.risk_pct > 0:
        log.info(
            "buys are sized by risk: a stop loss should cost %s%% of the vault's quote, never more "
            "than %s per buy (pool layout saved at block %s, scaled to the pool's liquidity "
            "when a buy is sized)",
            settings.risk_pct,
            settings.max_trade,
            depth["block"],
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
        state_file=state.STATE_FILE,
        depth=depth,
        depth_file=v3pool.LIVE_PROFILE_FILE,
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
