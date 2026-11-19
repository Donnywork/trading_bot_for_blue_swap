"""Low-priority public diagnostics. No keys, signing, execution, scans or backfill."""

import copy
import json
import threading
import time

from web3 import Web3

import event_activity as activity
import event_history
import event_market as market
import event_portfolio
import ledger
import rpc_metrics as rpc
from runtime import error_summary

BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]
INTERVAL = 15
LOOKUPS = 4


def balances(w3, pools, address, operator, snapshot, now):
    # A recent reconciled snapshot saves a header and slot0 read. Otherwise pin a
    # lightweight valuation; it is not a liquidity snapshot or a chart observation.
    if snapshot and 0 <= now - snapshot["timestamp"] <= 15:
        number, timestamp, block_hash = (
            snapshot[k] for k in ("block", "timestamp", "block_hash")
        )
        sqrt = next(
            p["sqrt" + "PriceX96"] for p in snapshot["profiles"] if p["fee"] == 500
        )
    else:
        header = w3.eth.get_block("latest")
        number, timestamp, block_hash = (
            header["number"],
            header["timestamp"],
            activity.hex_text(header["hash"]),
        )
        sqrt = pools[500]["pool"].functions.slot0().call(block_identifier=number)[0]
    values = {}
    for name, token in (("quote_raw", market.QUOTE), ("base_raw", market.BASE)):
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token), abi=BALANCE_ABI
        )
        raw = contract.functions.balanceOf(Web3.to_checksum_address(address)).call(
            block_identifier=number
        )
        if type(raw) is not int or raw < 0:
            raise ValueError("invalid token balance")
        values[name] = str(raw)
    gas = w3.eth.get_balance(operator, block_identifier=number) if operator else None
    if activity.hex_text(w3.eth.get_block(number)["hash"]) != block_hash:
        raise ValueError("holdings block changed")
    return {
        **values,
        "gas_wei": str(gas) if gas is not None else None,
        "operator": operator,
        "block": number,
        "block_hash": block_hash,
        "timestamp": timestamp,
        "observed": now,
        "price": activity.spot(sqrt),
        "fee": 500,
        "pool": market.POOLS[500],
    }


class Observer:
    def __init__(self, w3, pools, directory, settings, clock=time.time):
        self.w3, self.pools, self.directory, self.settings, self.clock = (
            w3,
            pools,
            directory,
            settings,
            clock,
        )
        # The interval is CU spend: every pass costs a handful of reads, and the
        # dashboard stays useful at a slower cadence on a rate-limited plan.
        self.interval = getattr(settings, "observer_seconds", None) or INTERVAL
        self.lock = threading.Lock()
        self.thread, self.next_run = None, 0
        self.pending, self.senders = {}, {}
        self.epoch, self.dropped = 0, 0
        self.error = ""
        self.session = clock()
        self.activity_path = directory / "activity.json"
        self.portfolio_path = directory / "portfolio.json"
        self.invalidate()

    def remember(self, tx):
        try:
            tx_hash = market.hex_value(activity.hex_text(tx["hash"]), 32)
            sender = market.hex_value(tx["from"].lower(), 20)
        except (KeyError, TypeError, ValueError, AttributeError):
            return
        with self.lock:
            self.senders[tx_hash] = sender
            if len(self.senders) > activity.MAX_ROWS:
                self.senders.pop(next(iter(self.senders)))

    def accept(self, snapshot, swaps, mode, dropped=0):
        try:
            rows = [
                activity.swap_row(
                    e,
                    fee,
                    mode=mode,
                    session=self.session,
                    timestamp=snapshot["timestamp"]
                    if e["blockNumber"] == snapshot["block"]
                    else None,
                )
                for fee, e in swaps
            ]
            with self.lock:
                self.dropped += dropped
                for row in rows:
                    self.pending[row["id"]] = row
                while len(self.pending) > activity.MAX_ROWS:
                    self.pending.pop(next(iter(self.pending)))
                    self.dropped += 1
        except (ValueError, KeyError, TypeError) as exc:
            self.error = f"Swap history: {error_summary(exc)}"

    def invalidate(self):
        # Immediately hide canonical markers while a new branch is being checked.
        with self.lock:
            self.epoch += 1
            self.pending.clear()
            try:
                value = activity.read(self.activity_path)
                value["verified"] = False
                value["error"] = (
                    "Canonical history requires revalidation after feed/snapshot recovery."
                )
                activity.save(self.activity_path, value)
            except (
                ValueError,
                OSError,
                KeyError,
                TypeError,
                AttributeError,
                ArithmeticError,
            ) as exc:
                self.error = f"Swap history: {error_summary(exc)}"

    def schedule(self, executor, snapshot, position):
        if self.clock() < self.next_run or (self.thread and self.thread.is_alive()):
            return
        self.next_run = self.clock() + self.interval
        # Copies prevent mode changes and receipt accounting from racing diagnostics.
        live = getattr(executor, "live_history", None)
        trades = copy.deepcopy(live.data["trades"]) if live else []
        self.thread = threading.Thread(
            target=self.update,
            args=(
                copy.deepcopy(snapshot),
                trades,
                getattr(executor, "operator", None),
                position is not None,
                self.epoch,
            ),
            daemon=True,
            name="event-diagnostics",
        )
        self.thread.start()

    def update(self, snapshot, trades, operator, inventory, epoch):
        self.error = ""
        with rpc.purpose(rpc.DIAGNOSTICS):
            try:
                self.update_holdings(snapshot, operator)
            except Exception as exc:  # noqa: BLE001 — diagnostic storage cannot stop exits
                self.error = f"Holdings: {error_summary(exc)}"
            try:
                self.update_activity(snapshot, trades, inventory, epoch)
            except Exception as exc:  # noqa: BLE001 — background diagnostic boundary
                self.error = f"Swap history: {error_summary(exc)}"

    def update_holdings(self, snapshot, operator):
        address = self.settings.bot_address
        if not address:
            return
        try:
            saved = json.loads(self.portfolio_path.read_text())
            if (
                saved["identity"] != event_history.IDENTITY
                or saved["contract"] != address
            ):
                raise ValueError("portfolio identity mismatch")
            if "observation" not in saved:
                raise ValueError("invalid portfolio schema")
            event_portfolio.validate_observation(saved["observation"])
        except FileNotFoundError:
            saved = {
                "identity": event_history.IDENTITY,
                "contract": address,
                "observation": None,
            }
        try:
            saved["observation"] = balances(
                self.w3, self.pools, address, operator, snapshot, self.clock()
            )
            saved["error"] = ""
        except Exception as exc:  # noqa: BLE001 — retain last observation, including on provider bugs
            # Preserve the original timestamp and every last successful raw amount.
            saved["error"] = f"Holdings read failed: {error_summary(exc)}"
        saved["attempted"] = self.clock()
        ledger.atomic_write(self.portfolio_path, json.dumps(saved, allow_nan=False))

    def update_activity(self, snapshot, trades, inventory, epoch):
        value = activity.read(self.activity_path)
        # Read failure leaves the file intact. A proven ancestry change invalidates
        # all earlier markers conservatively; only reobserved swaps become canonical.
        headers = {}
        anchor = value["anchor"]
        if anchor:
            header = self.w3.eth.get_block(anchor["block"])
            headers[anchor["block"]] = header
            if activity.hex_text(header["hash"]) != anchor["block_hash"]:
                activity.invalidate(
                    value, "Reorg: earlier markers invalidated; no historical backfill."
                )
        with self.lock:
            incoming = list(self.pending.values())
            senders = dict(self.senders)
        own = activity.own_rows(trades, self.settings.bot_address)
        invalid = {r["id"] for r in value["rows"] if not r["canonical"]}
        own = [r for r in own if r["id"] not in invalid]
        # Receipt-confirmed positions must appear while full snapshot work is
        # deferred for exits. Validate the newest receipt's block as an anchor.
        newest = max(own, key=lambda r: r["block"], default=None)
        if newest and (snapshot is None or newest["block"] > snapshot["block"]):
            header = self.w3.eth.get_block(newest["block"])
            headers[newest["block"]] = header
            if activity.hex_text(header["hash"]) == newest["block_hash"]:
                snapshot = {
                    "block": newest["block"],
                    "block_hash": newest["block_hash"],
                    "timestamp": header["timestamp"],
                }
            else:
                own = []
                activity.invalidate(
                    value,
                    "Reorg: execution markers invalidated; reconcile the live ledger.",
                )
        incoming = [r for r in incoming if snapshot and r["block"] <= snapshot["block"]]
        activity.merge(value, incoming)
        activity.merge(value, own)
        own_by_hash = {
            r["tx_hash"]: r for r in trades if r.get("status") == "confirmed"
        }
        actual_hashes = {
            r["tx_hash"] for r in value["rows"] if r["log_index"] is not None
        }
        value["rows"] = [
            r
            for r in value["rows"]
            if r["log_index"] is not None or r["tx_hash"] not in actual_hashes
        ]
        for row in value["rows"]:
            trade = own_by_hash.get(row["tx_hash"])
            if trade:
                row.update(
                    category="mine",
                    actor=trade.get("operator"),
                    contract=self.settings.bot_address,
                    action=trade["action"],
                    position_id=trade.get("position_id"),
                    profit_raw=str(trade["profit_raw"])
                    if "profit_raw" in trade
                    else None,
                )
        # A receipt fallback is replaced by its actual pool log when catch-up
        # supplies it. Preserve all distinct log indices within one transaction.
        actors = {k: getattr(self.settings, k) for k in ("market_bots", "competitors")}
        budget = 0 if inventory else LOOKUPS
        for row in reversed(value["rows"]):
            if not row["canonical"]:
                continue
            if row["category"] != "mine":
                sender = row.get("actor") or senders.get(row["tx_hash"])
                if not sender and budget:
                    budget -= 1
                    tx = self.w3.eth.get_transaction(row["tx_hash"])
                    if activity.hex_text(tx["hash"]) != row["tx_hash"]:
                        raise ValueError("transaction hash mismatch")
                    sender = market.hex_value(tx["from"].lower(), 20)
                    senders[row["tx_hash"]] = sender
                row.update(actor=sender, category=activity.classify(sender, actors))
            header = headers.get(row["block"])
            if row["timestamp"] is None and header is None and budget:
                budget -= 1
                header = self.w3.eth.get_block(row["block"])
                headers[row["block"]] = header
            if header:
                if activity.hex_text(header["hash"]) != row["block_hash"]:
                    row["canonical"] = False
                else:
                    row["timestamp"] = header["timestamp"]
        if snapshot:
            header = headers.get(snapshot["block"]) or self.w3.eth.get_block(
                snapshot["block"]
            )
            if activity.hex_text(header["hash"]) != snapshot["block_hash"]:
                activity.invalidate(
                    value, "Reorg: chart snapshot changed; markers invalidated."
                )
            else:
                value["anchor"] = {
                    k: snapshot[k] for k in ("block", "block_hash", "timestamp")
                }
                value["verified"] = True
                if not any(not r["canonical"] for r in value["rows"]):
                    value.pop("error", None)
        value["started"] = value["started"] or self.session
        value["updated"] = self.clock()
        value["actors"] = actors
        with self.lock:
            if epoch != self.epoch:
                return
            value["dropped"] += self.dropped
            activity.save(self.activity_path, value)
            for row in incoming:
                self.pending.pop(row["id"], None)
            self.senders.update(senders)
            self.senders = dict(list(self.senders.items())[-activity.MAX_ROWS :])
            self.dropped = 0
