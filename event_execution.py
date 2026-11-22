"""Exact event execution adapters; live sends require explicit startup configuration."""

import time

from web3 import Web3
from web3.exceptions import TimeExhausted, Web3Exception

import arbitrage
import bot_contract
import event_activity
import event_fees
import event_market as market
import event_submission
import frontrun
import rpc_metrics as rpc
from event_snapshot import hexstr
from legacy_main import Halt, received_from_receipt
from phase2_contract import load_abi
from runtime import ConfigurationError, error_summary


def decode_block(raw):
    # Normalize one eth_getBlockByNumber result from a read batch into the
    # keys the pending path consumes. Raw JSON numbers arrive as hex
    # strings; the hash stays 0x-hex (hexstr handles both shapes upstream).
    if not isinstance(raw, dict) or not isinstance(raw.get("hash"), str):
        raise TypeError("read batch block response invalid")
    try:
        return {
            "number": int(raw["number"], 16),
            "hash": raw["hash"].lower(),
            "timestamp": int(raw["timestamp"], 16),
            "baseFeePerGas": int(raw.get("baseFeePerGas") or "0x0", 16),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("read batch block response invalid") from exc


def sizes(cap):
    # Calldata sizes are derived exclusively from integer configuration, never model floats.
    # Dense low rungs: thin pools peak at small sizes (observed ~3% of cap on Sepolia),
    # and a six-rung ladder spaced 2x cannot express that optimum. Adjacent rungs stay
    # within 1.5x — except the smallest step, which integer rungs cannot split below 2x.
    # n=20 and n=36 (recomputed Nov 20) bracket the observed big-window optima
    # (~4.7k and ~5.6k on a 10k cap) that previously fell between rungs.
    return sorted(
        {
            max(1, cap * n // 64)
            for n in (1, 2, 3, 4, 6, 8, 12, 16, 20, 24, 32, 36, 48, 64)
        }
    )


def sizes_near(cap, optimum_raw):
    # ------------------------------------
    # Entry races are won in seconds (a 92 f564 window was lost to a 17 s quote
    # ladder). Split the ladder into the three rungs nearest the paper optimum and
    # the rest. The paper float only ORDERS configuration-derived integer rungs:
    # every calldata amount stays a rung, never a model float. A missing or
    # invalid hint degrades to the whole ladder in rung order.
    # ------------------------------------
    ladder = sizes(cap)
    if type(optimum_raw) is not int or optimum_raw <= 0:
        return ladder, []
    ordered = sorted(ladder, key=lambda n: abs(n - optimum_raw))
    return ordered[:3], ordered[3:]


class Executor:
    def __init__(self, w3, pools, settings, store, account=None, clock=time.time):
        self.w3, self.pools, self.settings, self.store = w3, pools, settings, store
        self.account, self.clock = account, clock
        self.live_history = store if settings.mode == "live" else None
        self.history_verified = False
        self.entry_guard = lambda action: None
        self.fees = event_fees.FeeCache(w3, settings, clock)
        self.attempt = {}
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
        if self.w3.eth.get_balance(self.operator) < s.max_gas * s.fee_ceiling:
            raise ConfigurationError("Operator ETH below maximum transaction cost")

    def current(self, block, block_hash):
        header = self.w3.eth.get_block("latest")
        if (
            header["number"] != block
            or hexstr(header["hash"]) != block_hash
            or not 0 <= self.clock() - header["timestamp"] <= 30
        ):
            raise ConfigurationError("execution snapshot is no longer current")
        return header

    def pending_header(self, block, block_hash, window=2):
        # A pending target waits in the pool while the head keeps advancing,
        # so current()'s strict pinned==latest equality refuses almost every
        # window (observed 2026-11-20 16:20: rejected 16 ms into a +21.7 f564
        # plan because the head had moved one block past the pin). The pending
        # cycle carries no exact quotes to invalidate: require a fresh head,
        # the pinned snapshot within a bounded block window behind it, and
        # the pinned block still canonical. Drift across the window is held
        # by the price limits and the on-chain floor - a stale cycle simply
        # reverts both legs.
        header = self.w3.eth.get_block("latest")
        if not 0 <= self.clock() - header["timestamp"] <= 30:
            raise ConfigurationError("pending submission head is not fresh")
        if not 0 <= header["number"] - block <= window:
            raise ConfigurationError("pending snapshot outside the submission window")
        pinned = self.w3.eth.get_block(block)
        if hexstr(pinned["hash"]) != block_hash:
            raise ConfigurationError("pending snapshot block was reorganized")
        return header

    def pending_reads(self, block, block_hash, window=2):
        # ------------------------------------
        # pending_header()'s freshness/canonical rules plus the chain id,
        # operator balance and pending nonce, ALL in one batched round trip.
        # Five sequential reads cost five round trips (~350 ms measured
        # 2026-11-21 — the same-block cycle broadcast at +0.72 s lost the
        # window to rivals arriving ~0.5 s); the batch answers in one. The
        # chain id comes from the same provider that will broadcast, so a
        # mid-flight failover only ever lands on another Sepolia endpoint.
        # ------------------------------------
        replies = self.w3.provider.read_batch(
            [
                ("eth_chainId", []),
                ("eth_getBlockByNumber", ["latest", False]),
                ("eth_getBlockByNumber", [hex(block), False]),
                ("eth_getBalance", [self.operator, "pending"]),
                ("eth_getTransactionCount", [self.operator, "pending"]),
            ]
        )
        chain, latest, pinned, balance, nonce = replies
        if int(chain, 16) != 11155111:
            raise ValueError("execution requires Sepolia")
        header = decode_block(latest)
        if not 0 <= self.clock() - header["timestamp"] <= 30:
            raise ConfigurationError("pending submission head is not fresh")
        if not 0 <= header["number"] - block <= window:
            raise ConfigurationError("pending snapshot outside the submission window")
        if pinned is None or decode_block(pinned)["hash"] != block_hash.lower():
            raise ConfigurationError("pending snapshot block was reorganized")
        return header, int(balance, 16), int(nonce, 16)

    def start_attempt(self, candidate, action):
        timing = event_submission.Timing(
            self.fees.first_seen(candidate["hash"]), self.clock
        )
        self.attempt = {
            "action": action,
            "target": candidate["hash"],
            "policy": self.settings.fee_policy,
            "timing": timing.data,
        }
        return timing

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
        # The deadline must hold at SEND time, not at the pinned quote block:
        # current() admits snapshots up to 30 s stale, so a pinned timestamp
        # +30 could already be expired when the transaction lands - a
        # guaranteed revert on an entry that passed every simulation.
        deadline = (
            max(header["timestamp"], self.w3.eth.get_block("latest")["timestamp"]) + 30
        )
        fn = self.bot.functions.swap((buy, fee, amount, minimum, deadline, limit))
        actual = fn.call({"from": self.operator}, block_identifier=block)
        if actual < minimum:
            raise ValueError("single swap simulation failed")
        if buy and actual > base_cap:
            raise ConfigurationError("simulated entry exceeds exit cap")
        return fn, output, minimum

    def backrun(self, candidate, action="backrun"):
        timing = self.start_attempt(candidate, action)
        if action not in ("backrun", "arbitrage"):
            raise ValueError("unsupported cycle action")
        if self.bot is None:
            raise ValueError("Phase 2 bot not configured")

        self.attempt.update(
            stage="quote_context",
            quote_rungs=0,
            snapshot_block=candidate["snapshot_block"],
        )
        context = arbitrage.CycleContext(
            candidate["snapshot_block"],
            candidate["snapshot_hash"],
            header=self.current(
                candidate["snapshot_block"], candidate["snapshot_hash"]
            ),
            vault_identity=(
                self.bot.address,
                market.QUOTE,
                market.BASE,
                market.POOLS[500],
                market.POOLS[10000],
            ),
            estimate_gas=False,
        )

        def quote(amount):
            return arbitrage.quote_cycle(
                self.pools[candidate["backrun"]["buy_fee"]],
                self.pools[candidate["backrun"]["sell_fee"]],
                amount,
                self.settings.min_profit,
                self.settings.base_cap,
                self.settings.slippage_bps,
                self.settings.move_bps,
                ttl=30,
                context=context,
            )

        # A profitable bracket quote is sent immediately: every extra rung risks
        # the window (three 25.97 f564 plans died to 18 s full-ladder quotes on
        # Nov 19 while competitors swept in). The remaining rungs are quoted only
        # when the bracket found nothing — no race to lose in a quiet market.
        best = None
        priority, rest = self.quote_ladder(candidate, action)
        self.attempt["stage"] = "quote_ladder"
        for rung in (priority, rest):
            for amount in rung:
                self.attempt["quote_rungs"] += 1
                try:
                    row = quote(amount)
                except ValueError:
                    continue
                if best is None or row["profit"] > best["profit"]:
                    best = row
            if best is not None:
                break
        if best is None:
            raise ConfigurationError("no exact profitable bounded cycle")
        if best["block"] != candidate["snapshot_block"]:
            raise ConfigurationError("backrun state advanced; candidate expired")
        # Reject head drift before the whole-cycle call and latest gas estimation.
        self.attempt["stage"] = "cycle_freshness"
        self.current(candidate["snapshot_block"], candidate["snapshot_hash"])
        self.attempt["stage"] = "cycle_simulation"
        checked = arbitrage.simulate_cycle(
            self.w3, self.bot, self.operator, best, context
        )
        self.attempt["stage"] = "gas_freshness"
        self.current(candidate["snapshot_block"], candidate["snapshot_hash"])
        # The deadline is anchored to the pinned quote block, but current()
        # still admits a header up to 30 s stale: after a slow validation the
        # transaction would broadcast with seconds of margin and revert
        # "expired" at inclusion — the class single() fixed on Nov 19.
        # Inclusion-block timestamps track wall time, so require room for the
        # send plus the next block against the wall clock.
        if best["deadline"] <= self.clock() + 13:
            raise ConfigurationError("cycle deadline cannot cover inclusion")
        return self.perform(
            action + ":" + candidate["hash"],
            self.bot.functions.execute(checked["args"]),
            action,
            best["amount_in"],
            best["min_quote_out"],
            candidate,
            timing=timing,
            cycle_deadline=best["deadline"],
        )

    def pending_cycle(self, candidate, snapshot):
        # ------------------------------------
        # Same-block pending submission. A competitor twice took our windows
        # (+12.30/+35.79 f564) by broadcasting the atomic cycle as a PENDING
        # transaction ~1 s after the market bot appeared pending, landing in
        # the block right after the target. This variant submits before any
        # receipt exists. The target is NOT on-chain yet, so exact quotes,
        # whole-cycle eth_call and gas estimation all read the wrong
        # (pre-target) state and would reject or revert: build the cycle from
        # the pinned snapshot's reconciled sqrt prices and the paper plan,
        # skip every quote/simulation read, and let the contract's atomic
        # floor be the guard — a lost race reverts both legs together.
        # ------------------------------------
        timing = self.start_attempt(candidate, "backrun")
        if self.bot is None or self.settings.mode not in ("simulate", "live"):
            raise ValueError("Phase 2 bot not configured")
        if not {"hash", "snapshot_block", "snapshot_hash"} <= set(candidate):
            raise ValueError("pending backrun candidate is invalid")
        plan = candidate.get("backrun") or {}
        if not {"buy_fee", "sell_fee", "input_f564"} <= set(plan):
            raise ValueError("pending backrun plan is invalid")
        try:
            hint = int(plan["input_f564"] * market.UNIT)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("pending backrun plan is invalid") from None
        if hint <= 0:
            raise ValueError("pending backrun plan is invalid")
        if (
            snapshot is None
            or snapshot.get("block") != candidate["snapshot_block"]
            or snapshot.get("block_hash") != candidate["snapshot_hash"]
        ):
            raise ConfigurationError("pending plan snapshot advanced")
        self.attempt.update(
            stage="pending_submission",
            quote_rungs=0,
            snapshot_block=candidate["snapshot_block"],
        )
        # The head normally advances while the target sits pending: use the
        # bounded-window freshness check, not strict pinned==latest equality.
        # ONE batched round trip also carries the chain id, operator balance
        # and pending nonce for both sends (2026-11-22: five sequential reads
        # pushed the broadcasts to +0.45 s/+0.72 s and lost every race).
        header, balance, nonce = self.pending_reads(
            candidate["snapshot_block"], candidate["snapshot_hash"]
        )
        if {plan["buy_fee"], plan["sell_fee"]} != set(market.POOLS) or {
            p.get("fee") for p in snapshot.get("profiles", [])
        } != set(market.POOLS):
            raise ValueError("pending backrun plan pools missing from snapshot")
        sqrt = {p["fee"]: p["sqrtPriceX96"] for p in snapshot["profiles"]}
        # First bracket rung nearest the paper optimum; the float hint only
        # ORDERS configuration-derived integer rungs, never calldata amounts.
        amount = sizes_near(self.settings.quote_cap, hint)[0][0]
        # No quote exists pre-target, so the slippage-bps minimum cannot
        # apply: the on-chain floor (minQuoteOut = amount + min_profit, which
        # the contract also enforces as its own required output) is the only
        # guard. min_base_out just needs the contract's nonzero minimum; the
        # atomic second leg catches a bad buy fill through the final floor.
        minimum = amount + self.settings.min_profit
        buy_limit = arbitrage.price_limit(
            sqrt[plan["buy_fee"]],
            int(market.QUOTE, 16) < int(market.BASE, 16),
            self.settings.move_bps,
        )
        sell_limit = arbitrage.price_limit(
            sqrt[plan["sell_fee"]],
            int(market.BASE, 16) < int(market.QUOTE, 16),
            self.settings.move_bps,
        )
        deadline = max(header["timestamp"], snapshot.get("timestamp", 0)) + 30
        if deadline <= self.clock() + 13:
            raise ConfigurationError("cycle deadline cannot cover inclusion")
        args = (
            plan["buy_fee"] == 500,  # pool0 is the fee-500 pool, validated at startup
            amount,
            1,
            minimum,
            self.settings.min_profit,
            deadline,
            buy_limit,
            sell_limit,
        )
        fn = self.bot.functions.execute(args)
        sweep = self.prepare_sweep(
            candidate, header, fn, timing, amount, minimum, nonce, balance
        )
        # Validate the same-block cycle with EVERY guard before either
        # broadcast: a refusal here cancels both (nothing in flight), and
        # the two raw sends then leave back-to-back instead of straddling
        # a re-validation pass.
        prepared = self.prepare(
            "backrun_pending:" + candidate["hash"],
            fn,
            "backrun",
            amount,
            minimum,
            candidate,
            timing=timing,
            cycle_deadline=deadline,
            gas_override=self.settings.max_gas,
            fee_header=header,
            nonce_override=nonce + (1 if sweep is not None else 0),
            prefetched={"chain": 11155111, "balance": balance, "nonce": nonce},
        )
        if isinstance(prepared, dict):
            return prepared  # simulated
        tx, row, position, decision, _ = prepared
        if sweep is not None:
            # Both transactions validated: record the sweep's durable intent
            # immediately before ITS broadcast (dispatch does the same for
            # the same-block cycle).
            self.store.begin_extra(sweep["key"], sweep["row"])
            try:
                timing.mark("sweep_broadcast_start")
                tx_hash = self.w3.eth.send_raw_transaction(sweep["signed"])
                timing.mark("sweep_broadcast_return")
            except Exception:  # noqa: BLE001 — even 'already known' may have landed
                self.halt_uncertain()
            sweep["hash"] = tx_hash.to_0x_hex()
        try:
            result = self.dispatch(
                "backrun_pending:" + candidate["hash"],
                row,
                position,
                tx,
                candidate,
                decision,
                timing=timing,
                gas_override=self.settings.max_gas,
            )
        except Halt:
            # Uncertainty about the primary send: both durable intents stay
            # exactly as recorded for manual reconciliation. Never settle
            # (or guess) around an unresolved broadcast.
            raise
        except Exception:
            # The sweep is in flight while the primary definitely failed
            # after its durable intent: settle the sweep before the refusal
            # propagates, so no window ends with an open intent. (A pre-send
            # refusal happens BEFORE the sweep broadcasts now — nothing to
            # settle in that case.)
            if sweep is not None:
                self.settle_sweep(sweep, wait=True)
            raise
        if sweep is not None:
            # Nonce N mines before the primary's N+1: the primary's settled
            # outcome implies the sweep has one too.
            self.settle_sweep(sweep, wait=False)
        return result

    def prepare_sweep(
        self, candidate, header, fn, timing, amount, minimum, nonce, balance
    ):
        # ------------------------------------
        # Competitor-1's next-block sweep (operator-approved 2026-11-21;
        # supersedes the earlier no-parallel-broadcast scope). Built and
        # SIGNED in the same ~0.5 s window as the same-block cycle, but
        # PURE: no store write, no broadcast. pending_cycle validates BOTH
        # transactions first (2026-11-22) and only then records the durable
        # intents and fires the two raw sends back-to-back — a pre-send
        # refusal of either cancels both, leaving nothing in flight and
        # nothing open to reconcile. Nonce N carries the sweep whose 16 gwei
        # tip tops the block AFTER the target's whenever the same-block
        # cycle misses it (they took +77.39, +40.46 and +28.49 exactly this
        # way, index 0 of the next block); nonce N+1 carries the 2-wei
        # same-block cycle. Fee-descending order and account-nonce order
        # agree (sweep tip > same-block tip > target tip), so a builder
        # can include both; every losing combination reverts BOTH legs on
        # the on-chain floor - gas only.
        # ------------------------------------
        s = self.settings
        if not s.pending_sweep or s.mode != "live":
            return None
        self.entry_guard("backrun")
        sweep_deadline = header["timestamp"] + 45  # must outlive the target's block
        if sweep_deadline <= self.clock() + 13:
            raise ConfigurationError("sweep deadline cannot cover inclusion")
        if s.fee_ceiling < header["baseFeePerGas"] + s.sweep_priority_wei:
            raise ConfigurationError("configured fee ceiling too low for sweep")
        if balance < 2 * s.max_gas * s.fee_ceiling:
            raise ConfigurationError("operator ETH below two-cycle maximum cost")
        tx = fn.build_transaction(
            {
                "from": self.operator,
                "chainId": 11155111,
                "gas": s.max_gas,
                "nonce": nonce,
                "maxPriorityFeePerGas": s.sweep_priority_wei,
                "maxFeePerGas": s.fee_ceiling,
            }
        )
        row = {
            "action": "backrun",
            "target": candidate["hash"],
            "amount_raw": amount,
            "minimum_raw": minimum,
            "reason": "next-block sweep",
            "estimated_gas": None,
            "gas_limit": s.max_gas,
            "snapshot_block": candidate["snapshot_block"],
            "stage": "sweep_submission",
            "quote_rungs": 0,
            "sweep": True,
        }
        key = "backrun_sweep:" + candidate["hash"]
        try:
            signed = self.account.sign_transaction(tx)
            timing.mark("sweep_signed")
        except (OSError, Web3Exception, ValueError, TypeError) as exc:
            # Nothing was broadcast: refuse the sweep only, the same-block
            # cycle still races (with this nonce, not nonce+1).
            self.attempt["sweep_error"] = error_summary(exc)
            return None
        return {
            "key": key,
            "row": row,
            "signed": signed.raw_transaction,
            "nonce": nonce,
        }

    def settle_sweep(self, sweep, wait):
        # Classify the sweep's outcome once its receipt exists. A bounded
        # wait covers both the primary's definite pre-send refusal (nonce
        # N+1 unused, so the sweep may still be in the mempool) and the
        # rare missing-receipt read; exhausting it records the terminal
        # "unsettled" status for manual reconciliation - never a silent
        # guess, never a retry. Transport failures propagate as ordinary
        # read errors and leave the durable intent for reconciliation.
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                Web3.to_bytes(hexstr=sweep["hash"]), timeout=60 if wait else 10
            )
        except TimeExhausted:
            self.store.finish_extra(sweep["key"], "unsettled", sweep["row"])
            return
        row = sweep["row"] | {
            "tx_hash": sweep["hash"],
            "block": receipt["blockNumber"],
            "block_hash": hexstr(receipt["blockHash"]),
            "gas_wei": receipt["gasUsed"] * receipt["effectiveGasPrice"],
            "transaction_index": receipt.get("transactionIndex"),
        }
        if receipt["status"] != 1:
            self.store.finish_extra(sweep["key"], "reverted", row)
            return
        received = received_from_receipt(receipt, market.QUOTE, self.bot.address)
        if received < sweep["row"]["minimum_raw"]:
            # Same protection as the primary path: a mined fill below the
            # recorded floor leaves accounting uncertain - halt for a human.
            self.store.halt("sweep receipt output below recorded minimum")
        row["received_raw"] = received
        self.store.finish_extra(sweep["key"], "confirmed", row)

    def quote_ladder(self, candidate, action=None):
        # Both cycle actions quote the bracket around the paper optimum first;
        # event-chaos arbitrage windows are races too. The float hint only
        # ORDERS configuration-derived integer rungs (never calldata amounts).
        plan = candidate.get("backrun") or {}
        hint = int((plan.get("input_f564") or 0) * market.UNIT)
        return sizes_near(self.settings.quote_cap, hint)

    def enter(self, candidate, snapshot):
        timing = self.start_attempt(candidate, "entry")
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
        # The model pass is free (pure math over the snapshot); only single() exact
        # quotes cost reads. Rank every rung by modeled profit, then quote the best
        # three: a 12-rung quote ladder costs ~60 reads and loses inclusion races.
        ranked = []
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
            except ValueError:
                continue
            if (
                estimate["profit_f564"] < self.settings.min_profit / market.UNIT
                or estimate["target_contribution_f564"] <= 0
            ):
                continue
            ranked.append((estimate["profit_f564"], amount))
        best = None
        for _, amount in sorted(ranked, reverse=True)[:3]:
            try:
                fn, output, minimum = self.single(
                    plan["buy_fee"], amount, True, snapshot["block"]
                )
            except ValueError:
                continue
            # Ranked descending by modeled profit, so the first rung that
            # passes exact validation IS the selection; quoting the remaining
            # bracket rungs could never change it and burns ~6-7 reads each
            # inside the tightest race (the target must still be pending at
            # broadcast, re-checked in perform()).
            best = (fn, amount, output, minimum)
            break
        if best is None:
            raise ConfigurationError("no bounded front-run entry passes validation")
        self.current(snapshot["block"], snapshot["block_hash"])
        return self.perform(
            "entry:" + candidate["hash"],
            best[0],
            "entry",
            best[1],
            best[3],
            candidate,
            timing=timing,
        )

    def exit(self, reason):
        position = self.store.data["position"]
        candidate = {"hash": position["target"], "reason": reason}
        timing = self.start_attempt(candidate, "exit")
        header = self.w3.eth.get_block("latest")
        if not 0 <= self.clock() - header["timestamp"] <= 30:
            raise ValueError("exit chain state stale")
        fn, _, minimum = self.single(
            position["exit_fee"], position["base_raw"], False, header["number"]
        )
        # Fresh attempts are allowed only after a definite pre-send failure or
        # confirmed revert. The persisted sequence number keeps retry keys
        # unique even though attempts are trimmed in the store.
        key = f"exit:{position['target']}:{self.store.reserve_seq()}"
        return self.perform(
            key,
            fn,
            "exit",
            position["base_raw"],
            minimum,
            candidate,
            timing=timing,
        )

    def perform(
        self,
        key,
        fn,
        action,
        amount,
        minimum,
        candidate,
        *,
        timing=None,
        cycle_deadline=None,
        gas_override=None,
        fee_header=None,
        nonce_override=None,
        prefetched=None,
    ):
        # ------------------------------------
        # The shared send path: every guard in prepare(), then the durable
        # intent and broadcast in dispatch(). Split (2026-11-22) so the
        # pending dual submission can validate BOTH transactions before
        # either broadcasts — the same-block cycle used to leave ~250 ms of
        # re-validation between the sweep's broadcast and its own, arriving
        # at +0.72 s in races decided by ~0.2 s.
        # ------------------------------------
        prepared = self.prepare(
            key,
            fn,
            action,
            amount,
            minimum,
            candidate,
            timing=timing,
            cycle_deadline=cycle_deadline,
            gas_override=gas_override,
            fee_header=fee_header,
            nonce_override=nonce_override,
            prefetched=prefetched,
        )
        if isinstance(prepared, dict):
            return prepared  # the simulate path finished inside prepare
        tx, row, position, decision, timing = prepared
        return self.dispatch(
            key,
            row,
            position,
            tx,
            candidate,
            decision,
            timing=timing,
            gas_override=gas_override,
        )

    def prepare(
        self,
        key,
        fn,
        action,
        amount,
        minimum,
        candidate,
        *,
        timing=None,
        cycle_deadline=None,
        gas_override=None,
        fee_header=None,
        nonce_override=None,
        prefetched=None,
    ):
        if key in self.store.data["attempts"]:
            raise ValueError("candidate already attempted")
        s = self.settings
        timing = timing or self.start_attempt(candidate, action)
        # Prefetched values (pending path) come from the same batched round
        # trip as the freshness header; every other caller re-reads live.
        if prefetched is not None:
            if prefetched["chain"] != 11155111:
                raise ValueError("execution requires Sepolia")
        elif self.w3.eth.chain_id != 11155111:
            raise ValueError("execution requires Sepolia")
        if gas_override is not None and (
            type(gas_override) is not int or gas_override <= 0
        ):
            raise ValueError("gas override must be a positive integer")
        if nonce_override is not None and (
            type(nonce_override) is not int or nonce_override < 0
        ):
            raise ValueError("nonce override must be a non-negative integer")
        if action in ("backrun", "arbitrage") and gas_override is None:
            self.attempt["stage"] = "gas_estimate"
        if gas_override is None:
            # A new head during estimation can turn valid pinned limits into SPL;
            # the estimation error itself is always the recorded verdict (the
            # stage field plus timing identify the drift context).
            gas = fn.estimate_gas({"from": self.operator})
            buffered = int(gas * bot_contract.SWAP_GAS_BUFFER)
            estimated = gas
        else:
            # A pending cycle executes against state that does not exist yet:
            # estimating against the pre-target pools would revert. The
            # override is already the configured ceiling, so the 2x estimate
            # buffer must never stack on top of it.
            gas = buffered = gas_override
            estimated = None  # no estimate happened; gas_limit carries the override
        if action in ("backrun", "arbitrage") and gas_override is None:
            self.attempt["stage"] = "final_guards"
        self.attempt.update(estimated_gas=estimated, gas_limit=buffered)
        if buffered > s.max_gas:
            raise ConfigurationError("buffered gas exceeds event limit")
        balance = (
            prefetched["balance"]
            if prefetched is not None
            else self.w3.eth.get_balance(self.operator)
        )
        row = {
            "action": action,
            "target": candidate["hash"],
            "amount_raw": amount,
            "minimum_raw": minimum,
            "reason": candidate.get("reason", ""),
            "estimated_gas": estimated,
            "gas_limit": buffered,
            "timing": timing.data,
        }
        if action in ("backrun", "arbitrage"):
            row.update(
                stage=self.attempt["stage"],
                quote_rungs=self.attempt.get("quote_rungs", 0),
                snapshot_block=candidate["snapshot_block"],
            )
        self.attempt = row
        position = self.store.data["position"]
        row["mode"] = s.mode
        row["position_id"] = (
            position.get("entry_tx") if action == "exit" and position else None
        )
        # Read the route and own deadline from the actual swap calldata, not
        # the valuation pool or the target transaction's separate deadline.
        single_deadline = None
        if action in ("entry", "exit"):
            args = getattr(fn, "arguments", None)
            single = None
            if isinstance(args, (dict, list, tuple)) and len(args) == 1:
                single = (
                    next(iter(args.values())) if isinstance(args, dict) else args[0]
                )
            if isinstance(single, dict) and set(single) == {
                "buyBase",
                "fee",
                "amountIn",
                "amountOutMinimum",
                "deadline",
                "sqrtPriceLimitX96",
            }:
                fee, single_deadline = single["fee"], single["deadline"]
            elif isinstance(single, (list, tuple)) and len(single) == 6:
                fee, single_deadline = single[1], single[4]
            else:
                fee = None
            if (
                type(fee) is not int
                or type(single_deadline) is not int
                or single_deadline <= 0
            ):
                raise ConfigurationError("single swap deadline metadata invalid")
            row["fee"] = fee
        if s.mode not in ("simulate", "live"):
            raise ValueError("paper execution cannot send")
        tx = None
        # The dual pending submission assigns consecutive nonces itself
        # (sweep first); reading the pending nonce here would race our own
        # in-flight sweep and produce a replacement, not a second cycle.
        if nonce_override is not None:
            nonce = nonce_override
        elif s.mode == "live":
            nonce = (
                prefetched["nonce"]
                if prefetched is not None
                else self.w3.eth.get_transaction_count(self.operator, "pending")
            )
        else:
            nonce = None
        if action != "exit":
            if fee_header is not None:
                # Pending submission: pending_header() validated freshness
                # against the bounded window moments earlier; re-reading only
                # adds latency to a race decided in hundreds of milliseconds.
                header = fee_header
            else:
                header = self.current(
                    candidate["snapshot_block"], candidate["snapshot_hash"]
                )
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
            header = self.w3.eth.get_block("latest")
        decision = self.fees.decide(header, action)
        if fee_header is not None:
            # The pending cycle must sort INTO the target's fee group, never
            # above it: builders order by tip descending and the market bot's
            # targets pay 0 gwei, so any positive tip buys an EARLIER slot —
            # and earlier than the target is a guaranteed pre-target revert
            # (block 12178067: our 10 gwei cycle sat at index 1 while the
            # 0-gwei target sat at index 135). Joining the group with the
            # pending tip keeps arrival order — we broadcast ~0.5 s after the
            # target — placing the cycle right behind it. This overrides any
            # fee policy for this one send; the max fee stays at the decided
            # ceiling so inclusion never depends on the tip.
            decision = dict(
                decision,
                selected_priority_wei=s.pending_priority_wei,
                pending_tip=True,
            )
        self.attempt["fee_decision"] = row["fee_decision"] = decision
        if not decision["allowed"]:
            raise ConfigurationError("configured fee ceiling is too low")
        if balance < s.max_gas * decision["selected_max_fee_wei"]:
            raise ConfigurationError("operator ETH below maximum transaction cost")
        if s.mode == "live":
            tx = fn.build_transaction(
                {
                    "from": self.operator,
                    "chainId": 11155111,
                    "gas": gas,
                    "nonce": nonce,
                    "maxPriorityFeePerGas": decision["selected_priority_wei"],
                    "maxFeePerGas": decision["selected_max_fee_wei"],
                }
            )
            row.update(operator=self.operator, nonce=nonce)
        if action != "exit":
            self.entry_guard(action)
        # Blocking preparation and the final control guard can consume the
        # inclusion margin. Recheck before recording intent or signing.
        if cycle_deadline is not None and cycle_deadline <= self.clock() + 13:
            raise ConfigurationError("cycle deadline cannot cover inclusion")
        if single_deadline is not None and single_deadline <= self.clock() + 13:
            raise ConfigurationError("single swap deadline cannot cover inclusion")
        timing.mark("validation_complete")
        if s.mode == "simulate":
            self.store.begin(key, row)
            self.store.finish(key, "simulated", position, row)
            return row | {"status": "simulated"}
        return tx, row, position, decision, timing

    def dispatch(
        self, key, row, position, tx, candidate, decision, *, timing, gas_override=None
    ):
        # ------------------------------------
        # Durable intent, broadcast, receipt classification: perform()'s
        # original tail, unchanged. store.begin lands here so the intent is
        # recorded immediately before ITS OWN broadcast — never before some
        # other transaction's.
        # ------------------------------------
        action, amount, minimum = row["action"], row["amount_raw"], row["minimum_raw"]
        self.store.begin(key, row)
        try:
            with rpc.purpose(rpc.LIVE):
                receipt = bot_contract.send_tx(
                    self.w3,
                    self.account,
                    tx,
                    gas_buffer=(
                        bot_contract.SWAP_GAS_BUFFER
                        if gas_override is None
                        else 1.0  # the override is already the ceiling; never double it
                    ),
                    timing=timing,
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
                        inclusion=event_submission.inclusion(receipt),
                    )
                    # Optional diagnostics must not turn a known revert into uncertainty.
                    try:
                        with rpc.purpose(rpc.RECEIPTS):
                            header = self.w3.eth.get_block(receipt["blockNumber"])
                        if header["hash"] == receipt["blockHash"]:
                            row["inclusion"] = self.inclusion(
                                receipt, header, candidate, decision
                            )
                    except Exception:  # noqa: BLE001 — accounting retains the known revert
                        row["inclusion_error"] = "Revert block metadata unavailable."
                self.store.finish(key, "reverted", position, row)
                return row | {"status": "reverted"}
            self.halt_uncertain()
        except Exception:  # noqa: BLE001 — any uncertainty after intent must halt
            self.halt_uncertain()
        # The receipt itself proves the fill; the block header is enrichment
        # metadata. A transient provider failure on that one read must not
        # halt a confirmed trade (Nov 19 15:41: a +22.98 f564 fill halted on
        # a single failed get_block right after the send). Retry like every
        # other read, then degrade to metadata-unavailable exactly like the
        # known-revert path above; verify_recent() revalidates the block on
        # the next startup. The reorg and minimum checks below still halt
        # whenever the evidence is present to justify it.
        header = None
        for attempt in range(3):
            try:
                with rpc.purpose(rpc.RECEIPTS):
                    header = self.w3.eth.get_block(receipt["blockNumber"])
                break
            except (OSError, Web3Exception):
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        if header is not None and header["hash"] != receipt["blockHash"]:
            self.store.halt("execution receipt reorganized")
        try:
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
                block_time=header["timestamp"] if header is not None else None,
                transaction_index=receipt.get("transactionIndex"),
                inclusion=self.inclusion(receipt, header, candidate, decision),
            )
            if header is None:
                row["inclusion_error"] = "Confirmed block metadata unavailable."
            else:
                try:
                    row["legs"] = event_activity.receipt_legs(
                        receipt, header["timestamp"]
                    )
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

    def halt_uncertain(self):
        try:
            self.store.save()  # Best effort to retain broadcast timings in the durable intent.
        finally:
            self.store.halt("execution outcome uncertain; reconcile before restart")

    def inclusion(self, receipt, header, candidate, decision):
        target = candidate.get("target_inclusion")
        known = self.fees.first_seen(candidate["hash"])
        if target is None and known:
            target = known["inclusion"]
        rival = decision["competitor"]
        return event_submission.inclusion(
            receipt, header, target, rival["inclusion"] if rival else None
        )

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
                # A missing block is a provider gap, not reorg evidence: let
                # the read error propagate so run() backs off and retries,
                # instead of accusing good inventory of reorganization and
                # forcing a manual reconciliation of verified trades.
                hashes[block] = hexstr(self.w3.eth.get_block(block)["hash"])
            if hashes[block] != row["block_hash"]:
                self.live_history.data["canonical_invalid"] = True
                self.live_history.save()
                self.live_history.halt(
                    "confirmed event trade reorganized; reconcile inventory"
                )
        self.history_verified = True
