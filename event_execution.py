"""Exact event execution adapters; live sends require explicit startup configuration."""

import time

from web3 import Web3
from web3.exceptions import BlockNotFound

import arbitrage
import bot_contract
import event_activity
import event_market as market
import frontrun
import rpc_metrics as rpc
from event_snapshot import hexstr
from legacy_main import received_from_receipt
from phase2_contract import load_abi
from runtime import ConfigurationError


def sizes(cap):
    # Calldata sizes are derived exclusively from integer configuration, never model floats.
    return sorted({max(1, cap * n // 16) for n in (1, 2, 4, 8, 12, 16)})


class Executor:
    def __init__(self, w3, pools, settings, store, account=None, clock=time.time):
        self.w3, self.pools, self.settings, self.store = w3, pools, settings, store
        self.account, self.clock = account, clock
        self.live_history = store if settings.mode == "live" else None
        self.history_verified = False
        self.entry_guard = lambda action: None
        self.bot = None
        self.operator = None
        if settings.mode == "paper":
            return
        if w3.eth.chain_id != 11155111:
            raise ConfigurationError("execution requires Sepolia")
        self.bot = w3.eth.contract(
            address=Web3.to_checksum_address(settings.bot_address), abi=load_abi()
        )
        fn = self.bot.functions
        if not w3.eth.get_code(self.bot.address) or fn.contractVersion().call() != 2:
            raise ConfigurationError(
                "BOT_CONTRACT_ADDRESS must be the new Phase2TradingBot"
            )
        if fn.owner().call().lower() != settings.owner:
            raise ConfigurationError("Phase 2 bot owner mismatch")
        if (
            fn.router().call().lower() != market.ROUTER
            or fn.quoteToken().call().lower() != market.QUOTE
            or fn.baseToken().call().lower() != market.BASE
            or fn.pool0().call().lower() != market.POOLS[500]
            or fn.pool1().call().lower() != market.POOLS[10000]
        ):
            raise ConfigurationError("Phase 2 bot market mismatch")
        self.operator = fn.operator().call()
        if int(self.operator, 16) == 0 or self.operator.lower() == settings.owner:
            raise ConfigurationError("Phase 2 bot needs a separate nonzero operator")
        if settings.mode == "live" and (
            account is None or account.address != self.operator
        ):
            raise ConfigurationError("live key must match the event vault operator")

    def check_live_limits(self):
        fn, s = self.bot.functions, self.settings
        if fn.paused().call():
            raise ConfigurationError("The owner has paused the vault")
        if (
            fn.maxQuoteIn().call() < s.quote_cap
            or fn.maxBaseIn().call() < s.base_cap
            or fn.minProfitFloor().call() > s.min_profit
        ):
            raise ConfigurationError(
                "Event limits conflict with the owner-configured vault limits"
            )
        if self.w3.eth.get_balance(self.operator) < s.max_gas * s.max_fee_wei:
            raise ConfigurationError("Operator ETH below maximum transaction cost")

    def current(self, block, block_hash):
        header = self.w3.eth.get_block("latest")
        if (
            header["number"] != block
            or hexstr(header["hash"]) != block_hash
            or not 0 <= self.clock() - header["timestamp"] <= 30
        ):
            raise ConfigurationError("execution snapshot is no longer current")

    def single(self, fee, amount, buy, block):
        if self.bot is None:
            raise ValueError("Phase 2 bot not configured")
        token_in, token_out = (
            (market.QUOTE, market.BASE) if buy else (market.BASE, market.QUOTE)
        )
        token_in, token_out = map(Web3.to_checksum_address, (token_in, token_out))
        if not self.bot.functions.feeAllowed(fee).call():
            raise ConfigurationError("front-run pool fee not permitted")
        if amount > self.bot.functions.tokenCap(token_in).call():
            raise ConfigurationError("front-run input exceeds owner cap")
        header = self.w3.eth.get_block(block)
        sqrt = self.pools[fee]["pool"].functions.slot0().call(block_identifier=block)[0]
        limit = arbitrage.price_limit(
            sqrt, int(token_in, 16) < int(token_out, 16), self.settings.move_bps
        )
        output, after, _, _ = (
            self.pools[fee]["quoter"]
            .functions.quoteExactInputSingle((token_in, token_out, amount, fee, limit))
            .call(block_identifier=block)
        )
        if output <= 0 or after == limit:
            raise ConfigurationError(
                "single quote reached price limit or returned no output"
            )
        base_cap = min(self.settings.base_cap, self.bot.functions.maxBaseIn().call())
        if buy and output > base_cap:
            raise ConfigurationError("entry output exceeds exit cap")
        minimum = output * (10000 - self.settings.slippage_bps) // 10000
        if minimum <= 0:
            raise ValueError("zero output minimum")
        fn = self.bot.functions.swap(
            (buy, fee, amount, minimum, header["timestamp"] + 30, limit)
        )
        actual = fn.call({"from": self.operator}, block_identifier=block)
        if actual < minimum:
            raise ValueError("single swap simulation failed")
        if buy and actual > base_cap:
            raise ConfigurationError("simulated entry exceeds exit cap")
        return fn, output, minimum

    def backrun(self, candidate, action="backrun"):
        if action not in ("backrun", "arbitrage"):
            raise ValueError("unsupported cycle action")
        if self.bot is None:
            raise ValueError("Phase 2 bot not configured")
        best = None
        for amount in sizes(self.settings.quote_cap):
            try:
                row = arbitrage.quote_cycle(
                    self.pools[candidate["backrun"]["buy_fee"]],
                    self.pools[candidate["backrun"]["sell_fee"]],
                    amount,
                    self.settings.min_profit,
                    self.settings.base_cap,
                    self.settings.slippage_bps,
                    self.settings.move_bps,
                    ttl=30,
                )
                if best is None or row["profit"] > best["profit"]:
                    best = row
            except ValueError:
                continue
        if best is None:
            raise ConfigurationError("no exact profitable bounded cycle")
        if best["block"] != candidate["snapshot_block"]:
            raise ConfigurationError("backrun state advanced; candidate expired")
        checked = arbitrage.simulate_cycle(self.w3, self.bot, self.operator, best)
        self.current(candidate["snapshot_block"], candidate["snapshot_hash"])
        return self.perform(
            action + ":" + candidate["hash"],
            self.bot.functions.execute(checked["args"]),
            action,
            best["amount_in"],
            best["min_quote_out"],
            candidate,
        )

    def enter(self, candidate, snapshot):
        plan, swap = candidate["frontrun"], candidate["swap"]
        if (
            self.bot is None
            or not self.bot.functions.feeAllowed(plan["sell_fee"]).call()
        ):
            raise ConfigurationError("front-run exit route is not permitted")
        tx = self.w3.eth.get_transaction(candidate["hash"])
        if (
            tx.get("blockNumber") is not None
            or self.w3.eth.get_transaction_count(
                Web3.to_checksum_address(swap["sender"]), "latest"
            )
            != swap["nonce"]
        ):
            raise ConfigurationError("target is no longer the next pending nonce")
        if swap["deadline"] and swap["deadline"] <= self.clock() + 2:
            raise ConfigurationError("target is expiring")
        pools = market.models(snapshot, self.clock())
        best = None
        for amount in sizes(self.settings.quote_cap):
            try:
                estimate = frontrun.evaluate(
                    pools,
                    swap,
                    amount / market.UNIT,
                    plan["buy_fee"],
                    plan["sell_fee"],
                    self.settings.base_cap / market.UNIT,
                )
                if (
                    estimate["profit_f564"] < self.settings.min_profit / market.UNIT
                    or estimate["target_contribution_f564"] <= 0
                ):
                    continue
                fn, output, minimum = self.single(
                    plan["buy_fee"], amount, True, snapshot["block"]
                )
                if best is None or estimate["profit_f564"] > best[0]:
                    best = (estimate["profit_f564"], fn, amount, output, minimum)
            except ValueError:
                continue
        if best is None:
            raise ConfigurationError("no bounded front-run entry passes validation")
        self.current(snapshot["block"], snapshot["block_hash"])
        return self.perform(
            "entry:" + candidate["hash"], best[1], "entry", best[2], best[4], candidate
        )

    def exit(self, reason):
        position = self.store.data["position"]
        header = self.w3.eth.get_block("latest")
        if not 0 <= self.clock() - header["timestamp"] <= 30:
            raise ValueError("exit chain state stale")
        fn, _, minimum = self.single(
            position["exit_fee"], position["base_raw"], False, header["number"]
        )
        # Fresh attempts are allowed only after a definite pre-send failure or confirmed revert.
        key = f"exit:{position['target']}:{len(self.store.data['attempts'])}"
        return self.perform(
            key,
            fn,
            "exit",
            position["base_raw"],
            minimum,
            {"hash": position["target"], "reason": reason},
        )

    def perform(self, key, fn, action, amount, minimum, candidate):
        if key in self.store.data["attempts"]:
            raise ValueError("candidate already attempted")
        s = self.settings
        if self.w3.eth.chain_id != 11155111:
            raise ValueError("execution requires Sepolia")
        gas = fn.estimate_gas({"from": self.operator})
        if gas * bot_contract.SWAP_GAS_BUFFER > s.max_gas:
            raise ConfigurationError("buffered gas exceeds event limit")
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        if s.max_fee_wei < base_fee + s.priority_wei:
            raise ConfigurationError("configured fee ceiling is too low")
        if self.w3.eth.get_balance(self.operator) < s.max_gas * s.max_fee_wei:
            raise ConfigurationError("operator ETH below maximum transaction cost")
        row = {
            "action": action,
            "target": candidate["hash"],
            "amount_raw": amount,
            "minimum_raw": minimum,
            "reason": candidate.get("reason", ""),
            "estimated_gas": gas,
        }
        position = self.store.data["position"]
        row["mode"] = s.mode
        row["position_id"] = (
            position.get("entry_tx") if action == "exit" and position else None
        )
        # The actual single-swap function carries its route; never infer it from
        # the valuation pool. Old records may have no recoverable entry route.
        args = getattr(fn, "arguments", ()) or ()
        single = (
            next(iter(args.values()), None)
            if isinstance(args, dict)
            else args[0]
            if len(args) == 1
            else None
        )
        if action in ("entry", "exit"):
            row["fee"] = (
                single[1]
                if isinstance(single, (list, tuple)) and len(single) == 6
                else position["exit_fee"]
                if action == "exit" and position
                else candidate.get("frontrun", {}).get("buy_fee")
            )
        if s.mode not in ("simulate", "live"):
            raise ValueError("paper execution cannot send")
        tx = None
        if s.mode == "live":
            tx = fn.build_transaction(
                {
                    "from": self.operator,
                    "gas": gas,
                    "maxPriorityFeePerGas": s.priority_wei,
                    "maxFeePerGas": s.max_fee_wei,
                    "nonce": self.w3.eth.get_transaction_count(
                        self.operator, "pending"
                    ),
                }
            )
            row.update(operator=self.operator, nonce=tx["nonce"])
        if action != "exit":
            self.entry_guard(action)
            self.current(candidate["snapshot_block"], candidate["snapshot_hash"])
            if action == "entry":
                target = self.w3.eth.get_transaction(candidate["hash"])
                if target.get("blockNumber") is not None:
                    raise ValueError("target mined during entry preparation")
                swap = candidate["swap"]
                if swap["deadline"] and swap["deadline"] <= self.clock() + 2:
                    raise ValueError("target expired during entry preparation")
                if (
                    self.w3.eth.get_transaction_count(
                        Web3.to_checksum_address(swap["sender"]), "latest"
                    )
                    != swap["nonce"]
                ):
                    raise ValueError("target nonce changed during entry preparation")
        else:
            fn.call({"from": self.operator})
        if s.mode == "simulate":
            self.store.begin(key, row)
            self.store.finish(key, "simulated", position, row)
            return row | {"status": "simulated"}
        self.store.begin(key, row)
        try:
            with rpc.purpose(rpc.LIVE):
                receipt = bot_contract.send_tx(
                    self.w3, self.account, tx, gas_buffer=bot_contract.SWAP_GAS_BUFFER
                )
        except bot_contract.NotSent:
            self.store.finish(key, "not_sent", position, row)
            return row | {"status": "not_sent"}
        except RuntimeError as exc:
            if str(exc).startswith("Transaction reverted: "):
                receipt = getattr(exc, "receipt", None)
                if receipt:
                    row.update(
                        tx_hash=hexstr(receipt["transactionHash"]),
                        block=receipt["blockNumber"],
                        block_hash=hexstr(receipt["blockHash"]),
                        gas_wei=receipt["gasUsed"] * receipt["effectiveGasPrice"],
                    )
                self.store.finish(key, "reverted", position, row)
                return row | {"status": "reverted"}
            self.store.halt("execution outcome uncertain; reconcile before restart")
        except Exception:  # noqa: BLE001 — any uncertainty after intent must halt
            self.store.halt("execution outcome uncertain; reconcile before restart")
        try:
            with rpc.purpose(rpc.RECEIPTS):
                header = self.w3.eth.get_block(receipt["blockNumber"])
            if header["hash"] != receipt["blockHash"]:
                self.store.halt("execution receipt reorganized")
            output_token = market.BASE if action == "entry" else market.QUOTE
            received = received_from_receipt(receipt, output_token, self.bot.address)
            if received < minimum:
                self.store.halt("receipt output below recorded minimum")
            row.update(
                tx_hash=hexstr(receipt["transactionHash"]),
                block=receipt["blockNumber"],
                block_hash=hexstr(receipt["blockHash"]),
                received_raw=received,
                gas_wei=receipt["gasUsed"] * receipt["effectiveGasPrice"],
                block_time=header["timestamp"],
                transaction_index=receipt.get("transactionIndex"),
            )
            try:
                row["legs"] = event_activity.receipt_legs(receipt, header["timestamp"])
            except (ValueError, KeyError, TypeError):
                row["legs"] = []
                row["legs_error"] = (
                    "Pool log metadata unavailable; receipt accounting retained."
                )
            if action == "entry":
                row["position_id"] = row["tx_hash"]
                swap = candidate["swap"]
                position = {
                    "base_raw": received,
                    "quote_raw": amount,
                    "opened": self.clock(),
                    "target": candidate["hash"],
                    "target_nonce": swap["nonce"],
                    "target_sender": swap["sender"],
                    "exit_fee": candidate["frontrun"]["sell_fee"],
                    "entry_tx": row["tx_hash"],
                    "entry_block": row["block"],
                    "entry_block_hash": row["block_hash"],
                    "entry_fee": row["fee"],
                    "entry_time": row["block_time"],
                }
            else:
                row["profit_raw"] = received - (
                    position["quote_raw"] if action == "exit" else amount
                )
                if action == "exit":
                    position = None
            self.store.finish(key, "confirmed", position, row)
        except Exception:  # noqa: BLE001 — a confirmed fill must never lose accounting
            self.store.halt("receipt accounting failed; reconcile before restart")
        return row | {"status": "confirmed"}

    def verify_recent(self):
        if self.live_history is None:
            return
        recovered = self.history_verified
        # A failed read must leave recovery pending for the next maintenance pass.
        self.history_verified = False
        confirmed = [
            row
            for row in self.live_history.data["trades"]
            if row["status"] in ("confirmed", "reverted") and "block_hash" in row
        ]
        position = self.live_history.data["position"]
        if position:
            # The entry can have fallen out of the bounded trade history.
            confirmed.insert(
                0,
                {
                    "block": position["entry_block"],
                    "block_hash": position["entry_block_hash"],
                },
            )
        # A later reorg must not silently strand inventory or cause a second exit.
        latest = self.w3.eth.block_number if confirmed else 0
        hashes = {}
        for row in confirmed:
            block = row["block"]
            if (
                recovered
                and latest - block >= 64
                and (position is None or block != position["entry_block"])
            ):
                continue
            if block not in hashes:
                try:
                    hashes[block] = hexstr(self.w3.eth.get_block(block)["hash"])
                except BlockNotFound:
                    hashes[block] = None
            if hashes[block] != row["block_hash"]:
                self.live_history.data["canonical_invalid"] = True
                self.live_history.save()
                self.live_history.halt(
                    "confirmed event trade reorganized; reconcile inventory"
                )
        self.history_verified = True
