"""Phase 2 bot. Start paused and operate it from the loopback dashboard."""

import argparse
import json
import logging
import queue
import signal
import threading
import time
from contextlib import closing
from dataclasses import replace

from web3 import Web3
from web3.exceptions import TransactionNotFound, Web3Exception

import backrun
import config
import control
import event_fees
import event_history
import event_market as market
import ledger
import operator_lock
import rpc_metrics as rpc
import strategy
from event_engine import PaperEngine
from event_feed import Feed, endpoint
from event_modes import Modes
from event_observer import Observer
from event_settings import EventSettings
from event_snapshot import Snapshots, SnapshotUnavailable, hexstr, markets
from event_store import DIRECTORY
from legacy_main import Halt, RedactingFormatter
from runtime import ConfigurationError, error_summary, required
from runtime import load_environment as load_dotenv

log = logging.getLogger("events")


def receipt_dict(receipt):
    return {
        "transactionHash": hexstr(receipt["transactionHash"]),
        "from": receipt["from"],
        "status": receipt["status"],
        "blockNumber": receipt["blockNumber"],
        "blockHash": hexstr(receipt["blockHash"]),
    }


class Service:
    def __init__(
        self,
        settings,
        store,
        snapshots,
        executor,
        feed,
        control_file=control.CONTROL_FILE,
        clock=time.time,
        modes=None,
        metrics=None,
        observer=None,
    ):
        self.settings, self.store, self.snapshots, self.executor, self.feed = (
            settings,
            store,
            snapshots,
            executor,
            feed,
        )
        self.control_file, self.clock = control_file, clock
        self.engine = PaperEngine(
            settings.limits, settings.market_bots, settings.competitors
        )
        self.controls = strategy.DEFAULT
        self.modes, self.metrics = modes, metrics
        self.observer = observer
        self.fees = getattr(executor, "fees", None) or event_fees.FeeCache(
            executor.w3, settings, clock
        )
        self.executor.fees = self.fees
        self.fee_gap = False
        self.mode_done = self.start_floor = None
        self.mode_block = (
            "Select Live after recovery before starting entries"
            if settings.mode == "live"
            else ""
        )
        self.control_error = ""
        self.nonce_hash, self.nonces = None, {}
        self.recent = []
        self.started = self.clock()
        self.price_history = event_history.Recorder(store.directory / "prices.jsonl")
        self.history_error = ""
        self.last_evaluation = None
        self.last_execution = None
        self.decision_counts = {}
        self.discarded_events = {"stale_pending": 0, "feed_changed": 0}
        self.health, self.message = "starting", "Updating saved pool snapshots"
        self.next_attempt, self.last_refresh, self.generation = 0, 0, -1
        self.receipt_generation = None
        self.scan_done = store.scan_request_done()
        self.scan_status = store.scan_status()
        self.arbitrage_block = None
        self.executor.entry_guard = self.entry_guard
        self.read_controls()  # Adopt old request numbers, never old permission to trade.

    def record(self, decision):
        decision = {"mode": self.settings.mode, "now": self.clock(), **decision}
        if decision.get("kind") in ("pending", "mined", "arbitrage"):
            if decision.get("status") == "prepared":
                decision["execution_note"] = (
                    "Backrun requires successful target inclusion and a profitable inclusion-block quote."
                    if decision.get("backrun")
                    else "No modeled backrun meets the configured caps and profit floor."
                )
                if decision.get("frontrun") and not self.controls.event_frontrun:
                    decision["execution_note"] += (
                        " A modeled front-run exists, but front-runs are disabled."
                    )
            self.last_evaluation = decision
        if decision.get("status") in (
            "execution_rejected",
            "simulated",
            "confirmed",
            "not_sent",
            "reverted",
        ) and decision.get("kind") not in ("pending", "mined"):
            self.last_execution = decision
        status = decision.get("status", "unknown")
        self.decision_counts[status] = self.decision_counts.get(status, 0) + 1
        self.store.decision(decision)
        self.recent.append(decision)
        self.recent = self.recent[-30:]

    def read_controls(self):
        try:
            cfg = control.load(self.control_file)
        except (ValueError, OSError):
            self.controls = replace(strategy.DEFAULT, event_paused=True)
            self.control_error = (
                "Invalid control file. Entries paused until repaired and started again."
            )
            self.start_floor = self.mode_done = None
            return
        self.control_error = ""
        if self.mode_done is None:
            self.mode_done, self.start_floor = (
                cfg.event_mode_request,
                cfg.event_start_request,
            )
        if cfg.event_paused:
            self.start_floor = max(self.start_floor, cfg.event_start_request)
        paused = (
            cfg.event_paused
            or cfg.event_start_request <= self.start_floor
            or cfg.event_mode_request != self.mode_done
            or bool(self.mode_block)
        )
        self.controls = replace(cfg, event_paused=paused)

    def process_mode(self, *, busy=False):
        cfg = self.controls
        if self.control_error or cfg.event_mode_request <= self.mode_done:
            return
        self.mode_done = cfg.event_mode_request
        self.start_floor = max(self.start_floor, cfg.event_start_request)
        try:
            if busy:
                raise ConfigurationError(
                    "Execution was in flight. Request the mode again."
                )
            ex = self.modes.activate(cfg.event_mode)
        except (ValueError, OSError, Web3Exception) as exc:
            self.mode_block = error_summary(exc)
        else:
            self.executor, self.settings, self.store = ex, ex.settings, ex.store
            ex.entry_guard = self.entry_guard
            ex.fees = self.fees
            self.engine.reset("execution mode changed")
            self.arbitrage_block, self.last_refresh = None, 0
            self.mode_block = ""
        self.read_controls()
        if not self.control_error:
            self.start_floor = max(self.start_floor, self.controls.event_start_request)
        self.controls = replace(self.controls, event_paused=True)

    def entry_guard(self, action=None):
        self.read_controls()
        if (
            self.controls.event_paused
            or not self.feed.ready.is_set()
            or self.feed.gap.is_set()
            or self.generation != self.feed.generation
        ):
            raise ValueError("event entries paused or feed changed")
        if action and (
            not self.feed.queue.empty()
            or (action == "entry" and not self.controls.event_frontrun)
            or (action == "backrun" and not self.controls.event_backrun)
            or (action == "arbitrage" and not self.controls.event_arbitrage)
        ):
            raise ValueError(
                "pending work or module control changed during preparation"
            )

    def refresh(self, scan=False):
        if self.store.data["position"] is not None:
            self.health = "managing_position"
            return  # Snapshot catch-up must never monopolize the loop while an exit is due.
        try:
            with rpc.purpose(rpc.SCAN if scan else rpc.SNAPSHOT):
                value = self.snapshots.refresh(scan=scan)
            # A paced scan can take several blocks. Catch its completed layout up
            # before handing it to the engine's ordinary freshness checks.
            if scan and self.clock() - value["timestamp"] > self.engine.max_age:
                with rpc.purpose(rpc.SNAPSHOT):
                    value = self.snapshots.refresh()
        except Exception as exc:
            self.fees.invalidate()
            self.price_history.verified = False
            if isinstance(exc, SnapshotUnavailable):
                self.invalidate_prices()
            if self.observer:
                self.observer.invalidate()
            self.engine.handle({"kind": "reorg", "now": self.clock()})
            raise
        decision = self.engine.handle(
            {"kind": "snapshot", "now": self.clock(), "snapshot": value}
        )
        if decision["status"] != "snapshot_loaded":
            raise ValueError("fresh snapshot unavailable")
        try:
            with rpc.purpose(rpc.DIAGNOSTICS):
                self.price_history.revalidate(self.executor.w3.eth.get_block)
            self.price_history.append(
                event_history.observation(
                    value, self.settings.mode, self.started, self.clock()
                )
            )
            self.history_error = ""
        except (OSError, Web3Exception, ValueError, KeyError, TypeError) as exc:
            # Chart storage must not block execution recovery or inventory exits.
            self.history_error = f"Price history unavailable: {error_summary(exc)}"
        self.last_refresh = self.clock()
        if self.observer:
            self.observer.accept(
                value,
                self.snapshots.swaps,
                self.settings.mode,
                self.snapshots.swaps_dropped,
            )
        with rpc.purpose(rpc.RECEIPTS):
            if self.nonce_hash != value["block_hash"]:
                self.nonces.clear()
                self.nonce_hash = value["block_hash"]
            for sender in self.settings.market_bots:
                if sender not in self.nonces:
                    self.nonces[sender] = self.executor.w3.eth.get_transaction_count(
                        Web3.to_checksum_address(sender), value["block"]
                    )
                self.engine.settle_nonces(sender, self.nonces[sender])
        self.health = "ready" if self.feed.ready.is_set() else "disconnected"

    def invalidate_prices(self):
        try:
            self.price_history.invalidate()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.history_error = f"Price history unavailable: {error_summary(exc)}"

    def maybe_execute(self, decision):
        if (
            self.settings.mode == "paper"
            or self.controls.event_paused
            or self.store.data["position"]
            or self.clock() < self.next_attempt
        ):
            return
        action = None
        if (
            decision.get("status") == "arbitrage_prepared"
            and decision.get("backrun")
            and self.controls.event_arbitrage
        ):
            action = "arbitrage"
        elif (
            decision.get("status") == "paper_revalidated"
            and decision.get("backrun")
            and self.controls.event_backrun
        ):
            action = "backrun"
        elif (
            decision.get("status") == "prepared"
            and decision.get("frontrun")
            and self.controls.event_frontrun
        ):
            action = "entry"
        if (
            action is None
            or f"{action}:{decision['hash']}" in self.store.data["attempts"]
        ):
            return
        self.entry_guard(action)
        self.next_attempt = self.clock() + self.settings.cooldown
        try:
            with rpc.purpose(rpc.QUOTES):
                result = (
                    self.executor.backrun(decision, action=action)
                    if action in ("backrun", "arbitrage")
                    else self.executor.enter(decision, self.engine.snapshot)
                )
        except (OSError, Web3Exception, ValueError) as exc:
            self.record(
                {
                    "kind": "execution",
                    "status": "execution_rejected",
                    "action": action,
                    "hash": decision["hash"],
                    "reason": error_summary(exc),
                    "diagnostics": getattr(self.executor, "attempt", {}),
                }
            )
            raise
        self.read_controls()
        self.process_mode(busy=True)
        self.record(result)
        # Our own transaction changes canonical state and every prediction based on it.
        if result["status"] == "confirmed":
            if action == "entry":
                self.next_attempt = 0  # A successful entry never delays its exit.
            self.engine.handle({"kind": "reorg", "now": self.clock()})
            self.refresh()

    def cross_pool(self):
        self.read_controls()
        snapshot = self.engine.snapshot
        if (
            not self.controls.event_arbitrage
            or self.controls.event_paused
            or not self.feed.ready.is_set()
            or self.feed.gap.is_set()
            or self.generation != self.feed.generation
            or not self.feed.queue.empty()
            or self.clock() < self.next_attempt
            or snapshot is None
            or self.store.data["position"]
            or snapshot["block_hash"] == self.arbitrage_block
        ):
            return
        self.arbitrage_block = snapshot["block_hash"]
        plan = backrun.best_cycle(
            market.models(snapshot, self.clock()), self.settings.limits
        )
        decision = {
            "kind": "arbitrage",
            "status": "arbitrage_prepared" if plan else "no_opportunity",
            "now": self.clock(),
            "hash": snapshot["block_hash"],
            "snapshot_block": snapshot["block"],
            "snapshot_hash": snapshot["block_hash"],
            "backrun": plan,
            "reason": "current cross-pool spread; independent of a target transaction",
        }
        self.record(decision)
        self.maybe_execute(decision)

    def exits(self):
        position = self.store.data["position"]
        request = self.controls.event_exit_request
        if position is None:
            if request > self.store.data["exit_done"]:
                self.store.data["exit_done"] = request
                self.store.save()
            return
        reason = None
        if request > self.store.data["exit_done"] and request >= int(
            position["opened"]
        ):
            reason = "requested"
        elif self.clock() >= position["opened"] + self.settings.exit_seconds:
            reason = "time_limit"
        else:
            nonce = self.executor.w3.eth.get_transaction_count(
                Web3.to_checksum_address(position["target_sender"]), "latest"
            )
            if nonce > position["target_nonce"]:
                reason = "target_nonce_mined"
        if reason and self.clock() >= self.next_attempt:
            self.next_attempt = self.clock() + self.settings.cooldown
            try:
                with rpc.purpose(rpc.QUOTES):
                    result = self.executor.exit(reason)
            except (OSError, Web3Exception, ValueError) as exc:
                self.record(
                    {
                        "kind": "execution",
                        "action": "exit",
                        "status": "execution_rejected",
                        "reason": error_summary(exc),
                        "diagnostics": getattr(self.executor, "attempt", {}),
                    }
                )
                raise
            self.read_controls()
            self.process_mode(busy=True)
            self.record(result)
            if self.store.data["position"] is None:
                self.store.data["exit_done"] = request
                self.store.save()

    def included(self, tx):
        try:
            with rpc.purpose(rpc.RECEIPTS):
                receipt = self.executor.w3.eth.get_transaction_receipt(tx["hash"])
        except TransactionNotFound:
            return
        if (
            self.engine.snapshot is None
            or receipt["blockNumber"] != self.engine.snapshot["block"]
        ):
            return
        self.fees.confirm(tx, receipt, self.engine.snapshot)
        decision = self.engine.handle(
            {
                "kind": "mined",
                "now": self.clock(),
                "transaction": tx,
                "receipt": receipt_dict(receipt),
            }
        )
        if decision.get("status") == "paper_revalidated":
            decision["target_inclusion"] = {
                "block": receipt["blockNumber"],
                "block_hash": hexstr(receipt["blockHash"]),
                "transaction_index": receipt.get("transactionIndex"),
                "source": "receipt_matching_snapshot",
            }
        self.record(decision)
        self.maybe_execute(decision)

    def handle(self, event):
        if (
            event.get("generation") != self.generation
            or not self.feed.ready.is_set()
            or self.feed.gap.is_set()
        ):
            self.discarded_events["feed_changed"] += 1
            return
        if event["kind"] == "head":
            self.fees.schedule()
            self.refresh()
            for tx in self.engine.pending_transactions():
                self.included(tx)
            self.cross_pool()
            return
        if event.get("removed"):
            self.fees.invalidate()
            self.executor.history_verified = False
            self.invalidate_prices()
            if self.observer:
                self.observer.invalidate()
            self.engine.handle({"kind": "reorg", "now": self.clock()})
            self.snapshots.snapshot = None
            self.feed.gap.set()
            return
        if self.observer and event.get("transaction"):
            self.observer.remember(event["transaction"])
        if event.get("transaction"):
            self.fees.observe(
                event["transaction"],
                received=event.get("received"),
                received_mono=event.get("received_mono"),
            )
        if event["kind"] == "mined":
            self.refresh()
            self.included(event["transaction"])
            return
        if self.clock() - event["received"] > 2:
            self.discarded_events["stale_pending"] += 1
            return
        tx = event["transaction"]
        decision = self.engine.handle(
            {"kind": "pending", "now": self.clock(), "transaction": tx}
        )
        self.record(decision)
        self.maybe_execute(decision)

    def maintain(self):
        self.read_controls()
        gap = self.feed.gap.is_set() or self.generation != self.feed.generation
        if (
            gap and not self.fee_gap
        ) or self.receipt_generation != self.feed.generation:
            self.fees.invalidate()
        self.fee_gap = gap
        if self.receipt_generation != self.feed.generation:
            self.executor.history_verified = False
            self.receipt_generation = self.feed.generation
        if self.feed.gap.is_set() or self.generation != self.feed.generation:
            self.price_history.verified = False
        with rpc.purpose(rpc.RECEIPTS):
            self.executor.verify_recent()
            # A request seen with inventory must be refused even if this pass closes it.
            if self.store.data["position"] is not None:
                self.process_mode()
            self.exits()
        self.process_mode()
        scan = (
            self.controls.event_scan_request > self.scan_done
            and self.store.data["position"] is None
        )
        if scan:
            self.price_history.verified = False
            self.store.acknowledge_scan(self.controls.event_scan_request)
            self.scan_done = self.controls.event_scan_request
            self.health, self.message = "scanning", "Scanning both pools as requested"
            self.publish()
            self.engine.handle({"kind": "reorg", "now": self.clock()})
            try:
                self.refresh(scan=True)
            except Exception:
                self.scan_status = "Failed. Request another scan to retry."
                self.store.scan_result(self.scan_done, self.scan_status)
                raise
            self.scan_status = "Completed"
            self.store.scan_result(self.scan_done, self.scan_status)
        if self.feed.gap.is_set() or self.generation != self.feed.generation:
            if not scan:
                self.engine.handle({"kind": "reorg", "now": self.clock()})
            self.health = "disconnected"
            if not self.feed.ready.is_set():
                self.message = "Waiting for event feed"
                if self.feed.last_error:
                    self.message += f": {self.feed.last_error}"
                return
            generation = self.feed.generation
            self.feed.gap.clear()
            if not scan:
                self.refresh()
            if self.feed.gap.is_set() or generation != self.feed.generation:
                return
            self.generation = generation
            if self.store.data["position"] is None:
                self.health = "ready"
        if scan or self.clock() - self.last_refresh >= 12:
            if not scan:
                self.refresh()
            for tx in self.engine.pending_transactions():
                self.included(tx)
        self.engine.handle({"kind": "tick", "now": self.clock()})
        self.cross_pool()

    def publish(self):
        snapshot = self.engine.snapshot
        self.store.state(
            {
                "updated": self.clock(),
                "health": self.health,
                "message": self.message,
                "mode": self.settings.mode,
                "mode_block": self.mode_block,
                "control_error": self.control_error,
                "mode_done": self.mode_done,
                "start_floor": self.start_floor,
                "cooldown_until": self.next_attempt,
                "intent_pending": self.store.data["intent"] is not None,
                "execution_verified": getattr(self.executor, "history_verified", False),
                "history_error": self.history_error,
                "observation_error": self.observer.error if self.observer else "",
                "exit_seconds": self.settings.exit_seconds,
                "fee_policy": self.settings.fee_policy,
                "mode_availability": {
                    "simulate": ""
                    if self.settings.bot_address
                    else "Configure a Phase2TradingBot contract to use Simulation.",
                    "live": ""
                    if self.settings.bot_address and self.settings.live_enabled
                    else "Live requires a configured Phase2TradingBot and DRY_RUN=false. Signer and vault checks still apply.",
                },
                "last_evaluation": self.last_evaluation,
                "last_execution": self.last_execution,
                "execution_attempt": getattr(self.executor, "attempt", None),
                "decision_counts": dict(self.decision_counts),
                "discarded_events": dict(self.discarded_events),
                "feed": {
                    **self.feed.status(),
                    "gap": self.feed.gap.is_set(),
                    "recovering": self.generation != self.feed.generation,
                },
                "rpc": self.metrics.snapshot() if self.metrics else None,
                "scan_status": (
                    "Scanning"
                    if self.health == "scanning"
                    else "Deferred until position closes"
                    if self.controls.event_scan_request > self.scan_done
                    and self.store.data["position"]
                    else self.scan_status
                ),
                "contract": "Phase2TradingBot",
                "bot_address": self.settings.bot_address,
                "connected": self.feed.ready.is_set(),
                "feed_error": self.feed.last_error,
                "snapshot_block": snapshot["block"] if snapshot else None,
                "snapshot_time": snapshot["timestamp"] if snapshot else None,
                "pools": [
                    {"fee": p["fee"], "address": p["pool_address"], "block": p["block"]}
                    for p in snapshot["profiles"]
                ]
                if snapshot
                else [],
                "controls": {
                    k: getattr(self.controls, k) for k in control.EVENT_FIELDS
                },
                "actors": {
                    "market_bots": self.settings.market_bots,
                    "competitors": self.settings.competitors,
                },
                "caps": {k: str(v) for k, v in self.settings.limits.items()},
                "position": self.store.data["position"],
                "trades": self.store.data["trades"][-30:],
                "decisions": self.recent,
                "scan_done": self.scan_done,
                "exit_done": self.store.data["exit_done"],
            }
        )

    def run(self, stop, seconds=None):
        started = self.clock()
        failures = 0
        self.publish()
        self.feed.start()
        try:
            while not stop.is_set() and (
                seconds is None or self.clock() - started < seconds
            ):
                try:
                    self.maintain()
                    try:
                        event = self.feed.queue.get(timeout=1)
                    except queue.Empty:
                        event = None
                    if event:
                        self.handle(event)
                    if self.feed.ready.is_set():
                        self.message = (
                            "Listening for market-bot and competitor transactions"
                        )
                    failures = 0
                except Halt as exc:
                    self.health, self.message = (
                        "halted",
                        f"Execution halted. {exc}",
                    )
                    raise
                except SnapshotUnavailable:
                    self.health, self.message = (
                        "awaiting_scan",
                        (
                            "Saved liquidity unavailable or invalid; entries withheld. "
                            "Scan required. Click Scan Pool Liquidity in the dashboard."
                        ),
                    )
                    stop.wait(1)
                except (OSError, Web3Exception, ValueError) as exc:
                    self.engine.handle({"kind": "reorg", "now": self.clock()})
                    failures += 1
                    delay = min(60, 2 ** min(failures, 6))
                    if self.store.data["position"] is not None:
                        delay = min(delay, self.settings.cooldown)
                    self.health, self.message = (
                        "degraded",
                        f"Read or validation failed: {error_summary(exc)}; entries withheld, retry in {delay}s",
                    )
                    log.warning(self.message)
                    stop.wait(delay)
                except Exception as exc:
                    self.health, self.message = (
                        "failed",
                        f"Service failed: {error_summary(exc)}",
                    )
                    log.error(self.message)
                    raise
                finally:
                    if self.health not in ("halted", "failed") and not stop.is_set():
                        self.fees.schedule()
                    if (
                        self.observer
                        and self.health not in ("halted", "failed")
                        and not stop.is_set()
                    ):
                        self.observer.schedule(
                            self.executor,
                            self.engine.snapshot,
                            self.store.data["position"],
                        )
                    self.publish()
        finally:
            stop.set()
            if self.health not in ("halted", "failed"):
                self.health, self.message = "stopped", "Event service stopped"
            self.publish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, help="stop after a bounded observation")
    parser.add_argument(
        "--load-env",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-env", action="store_true", help="use exported environment only"
    )
    args = parser.parse_args()
    if args.seconds is not None and args.seconds <= 0:
        parser.error("seconds must be positive")
    if args.load_env and args.no_env:
        parser.error("--load-env and --no-env cannot be combined")
    if not args.no_env:
        load_dotenv()
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    settings = EventSettings.from_env()
    uri = endpoint()
    metrics = rpc.RpcMetrics()
    w3 = Web3(config.rpc_provider(required("RPC_URL"), metrics))
    pools = markets(w3)
    identity = {
        "chain_id": 11155111,
        "pools": market.POOLS,
        "base": market.BASE,
        "quote": market.QUOTE,
        "contract": "Phase2TradingBot",
        "bot_address": settings.bot_address,
        "owner": settings.owner,
    }
    # Normalize integer map keys before comparing against persisted JSON.
    identity = json.loads(json.dumps(identity))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with (
        operator_lock.acquire(DIRECTORY.parent / "event_service.lock"),
        closing(Modes(w3, pools, settings, DIRECTORY, identity)) as modes,
    ):
        try:
            executor = modes.startup()
        except (Halt, ConfigurationError, OSError, ValueError, Web3Exception) as exc:
            reason = str(exc) if isinstance(exc, Halt) else error_summary(exc)
            ledger.atomic_write(
                DIRECTORY / "state.json",
                json.dumps(
                    {
                        "health": "halted"
                        if isinstance(exc, (Halt, operator_lock.SendBlocked))
                        else "failed",
                        "updated": time.time(),
                        "message": reason,
                        "rpc": metrics.snapshot(),
                    }
                ),
            )
            raise
        snapshots = Snapshots(w3, pools, DIRECTORY / "snapshot.json", stop.is_set)
        feed = Feed(
            uri, settings.market_bots + settings.competitors, stop, metrics=metrics
        )
        Service(
            executor.settings,
            executor.store,
            snapshots,
            executor,
            feed,
            control_file=DIRECTORY.parent / "control.json",
            clock=time.time,
            modes=modes,
            metrics=metrics,
            observer=Observer(w3, pools, DIRECTORY, settings),
        ).run(stop, args.seconds)


def cli():
    try:
        main()
    except operator_lock.SendBlocked as exc:
        print(str(exc))
    except Halt as exc:
        print(f"Event execution halted. Reconcile before restarting. {exc}")
    except Exception as exc:  # noqa: BLE001 — CLI boundary must not print provider bodies
        print(f"Event service failed: {error_summary(exc)}", flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
