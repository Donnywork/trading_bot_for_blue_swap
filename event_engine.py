"""Stateful paper event processing. No RPC, dotenv, signer, or execution capability."""

import copy
import math

import backrun
import event_market as market
import frontrun
from runtime import error_summary

ACTIVE = {"prepared", "stale", "awaiting_snapshot", "paper_revalidated"}


class PaperEngine:
    def __init__(
        self,
        limits,
        market_bots=(market.MARKET_BOT,),
        competitors=(),
        ttl=60,
        max_age=30,
    ):
        self.limits = backrun.validate_limits(dict(limits))
        self.market_bots = {market.hex_value(a, 20) for a in market_bots}
        self.competitors = {market.hex_value(a, 20) for a in competitors}
        if (
            type(ttl) is not int
            or not 1 <= ttl <= 120
            or type(max_age) is not int
            or not 1 <= max_age <= 60
        ):
            raise ValueError("invalid candidate TTL or snapshot age")
        self.ttl, self.max_age = ttl, max_age
        self.snapshot = None
        self.candidates, self.nonces, self.seen = {}, {}, set()
        self.settled_nonces = set()
        self._transactions = {}
        self.clock = 0.0

    def pending_transactions(self):
        """Copies of targets that still need a receipt, including superseded hashes.

        A superseded transaction can still win the race to inclusion. Keep polling
        it until expiry; completed or invalidated candidates need no more reads.
        """
        return [
            copy.deepcopy(tx)
            for h, tx in self._transactions.items()
            if h in self.candidates
            and self.candidates[h]["expires"] >= self.clock
            and self.candidates[h]["status"]
            in {"prepared", "stale", "awaiting_snapshot", "superseded"}
        ]

    def settle_nonces(self, sender, next_nonce):
        """Apply an account nonce read at the current canonical snapshot block."""
        sender = market.hex_value(sender, 20)
        if self.snapshot is None or type(next_nonce) is not int or next_nonce < 0:
            raise ValueError("nonce settlement requires a snapshot and a valid nonce")
        self.settled_nonces.update(
            (s, n) for s, n in self.nonces if s == sender and n < next_nonce
        )

    def advance(self, now):
        if type(now) not in (int, float) or not math.isfinite(now) or now < self.clock:
            raise ValueError("events require nondecreasing finite observation times")
        self.clock = now
        expired = []
        for candidate in self.candidates.values():
            if candidate["status"] in ACTIVE and now > candidate["expires"]:
                candidate.update(
                    status="expired",
                    reason="candidate expired",
                    backrun=None,
                    frontrun=None,
                )
                expired.append(candidate["hash"])
        self.candidates = {
            h: c for h, c in self.candidates.items() if c["expires"] + 120 >= now
        }
        self._transactions = {
            h: tx
            for h, tx in self._transactions.items()
            if h in self.candidates and self.candidates[h]["expires"] >= now
        }
        if len(self.seen) > 4096:
            self.seen = set(self.candidates)
        if len(self.nonces) > 4096:
            # Dropping sequencing history must also discard every pending prediction.
            self.invalidate("nonce history compacted", "invalidated")
            self.nonces.clear()
            self.settled_nonces.clear()
            self._transactions.clear()
        return expired

    def reset(self, reason):
        changed = self.invalidate(reason, "invalidated")
        self.snapshot = None
        self.settled_nonces.clear()
        self._transactions.clear()
        return changed

    def invalidate(self, reason, status="stale"):
        changed = []
        for candidate in self.candidates.values():
            if candidate["status"] in ACTIVE or (
                status == "invalidated" and candidate["status"] == "superseded"
            ):
                candidate.update(
                    status=status, reason=reason, backrun=None, frontrun=None
                )
                changed.append(candidate["hash"])
        return changed

    def pools(self, now):
        if self.snapshot is None:
            raise market.PredictionRejected("snapshot unavailable")
        return market.models(self.snapshot, now, self.max_age)

    def handle(self, event):
        now = event.get("now")
        expired = self.advance(now)
        kind = event.get("kind")
        decision = {"kind": kind, "now": now, "paper_only": True, "expired": expired}
        if kind == "snapshot":
            snapshot = event["snapshot"]
            try:
                market.models(snapshot, now, self.max_age)
                if self.snapshot and snapshot["block"] < self.snapshot["block"]:
                    raise ValueError(
                        "snapshot moved backwards; explicit reorg required"
                    )
            except (ValueError, KeyError, TypeError):
                decision["invalidated"] = self.reset("invalid snapshot")
                return decision | {"status": "rejected", "reason": "invalid snapshot"}
            changed_hash = (
                self.snapshot and snapshot["block_hash"] != self.snapshot["block_hash"]
            )
            decision["invalidated"] = (
                self.invalidate("snapshot changed") if changed_hash else []
            )
            self.snapshot = copy.deepcopy(snapshot)
            return decision | {"status": "snapshot_loaded", "block": snapshot["block"]}
        if kind == "reorg":
            decision["invalidated"] = self.reset("chain reorganization")
            return decision | {"status": "invalidated"}
        if kind == "tick":
            return decision | {"status": "clock_advanced"}
        if kind not in ("pending", "mined"):
            raise ValueError("unsupported event kind")
        tx = event.get("transaction", {})
        try:
            sender, nonce, tx_hash = market.transaction_identity(tx)
        except (ValueError, TypeError):
            return decision | {
                "status": "rejected",
                "reason": "invalid transaction identity",
            }
        decision["hash"] = tx_hash
        if kind == "mined" and event.get("removed"):
            decision["invalidated"] = self.reset("removed mined transaction")
            return decision | {"status": "invalidated"}
        if sender not in self.market_bots:
            if sender in self.competitors or kind == "mined":
                decision["invalidated"] = self.invalidate("competitor activity")
                if kind == "mined":
                    self.snapshot = None
                return decision | {"status": "competitor_observed"}
            # A new successful pool snapshot is the authoritative source for other actors.
            return decision | {"status": "ignored"}
        if kind == "mined":
            return decision | self.included(tx_hash, sender, nonce, event, now)
        if tx_hash in self.seen:
            return decision | {"status": "duplicate"}
        self.seen.add(tx_hash)
        previous = self.nonces.get((sender, nonce))
        if previous and previous in self.candidates:
            self.candidates[previous].update(
                status="superseded",
                reason="same-nonce replacement",
                backrun=None,
                frontrun=None,
            )
            decision["superseded"] = previous
        self.nonces[sender, nonce] = tx_hash
        decision["invalidated"] = self.invalidate(
            "another market-bot pending transaction"
        )
        stage = "target decoding"
        try:
            swap = market.decode_swap(tx, now)
            stage = "snapshot validation"
            pools = self.pools(now)
            # An independently mined earlier nonce would change this prediction. Refuse
            # multi-pending sequencing until a future adapter explicitly simulates it.
            if any(
                s == sender and n < nonce and (s, n) not in self.settled_nonces
                for s, n in self.nonces
            ):
                raise market.PredictionRejected(
                    "earlier market-bot nonce is unresolved"
                )
            expiry = min(now + self.ttl, swap["deadline"] or now + self.ttl)
            stage = "opportunity modeling"
            candidate = {
                "hash": tx_hash,
                "sender": sender,
                "nonce": nonce,
                "created": now,
                "expires": expiry,
                "snapshot_block": self.snapshot["block"],
                "snapshot_hash": self.snapshot["block_hash"],
                "status": "prepared",
                "reason": "hypothetical target inclusion",
                "swap": swap,
                "backrun": backrun.prepare(pools, swap, self.limits),
                "frontrun": frontrun.prepare(pools, swap, self.limits),
            }
        except (
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            ZeroDivisionError,
        ) as exc:
            return decision | {
                "status": "rejected",
                "stage": stage,
                "reason": str(exc)
                if isinstance(exc, market.PredictionRejected)
                else error_summary(exc),
            }
        self.candidates[tx_hash] = candidate
        self._transactions[tx_hash] = copy.deepcopy(tx)
        return decision | copy.deepcopy(candidate)

    def included(self, tx_hash, sender, nonce, event, now):
        candidate = self.candidates.get(tx_hash)
        if candidate and (candidate["sender"], candidate["nonce"]) != (sender, nonce):
            return {
                "status": "rejected",
                "reason": "transaction identity changed for known hash",
            }
        for other_hash, other in self.candidates.items():
            if other_hash != tx_hash and other["status"] in ACTIVE:
                other.update(
                    status="stale",
                    reason="another market-bot inclusion",
                    backrun=None,
                    frontrun=None,
                )
        receipt = event.get("receipt")
        try:
            if (
                receipt
                and market.hex_value(receipt["transactionHash"], 32) == tx_hash
                and market.hex_value(receipt["from"], 20) == sender
                and market.integer(receipt["status"]) in (0, 1)
            ):
                self.settled_nonces.add((sender, nonce))
        except (ValueError, KeyError, TypeError):
            pass
        if candidate is None:
            self.invalidate("market-bot inclusion without preparation")
            self.snapshot = None
            return {"status": "unprepared", "reason": "no pending candidate"}
        if now > candidate["expires"]:
            candidate.update(
                status="expired",
                reason="candidate expired",
                backrun=None,
                frontrun=None,
            )
        if candidate["status"] in (
            "expired",
            "invalidated",
            "reverted",
            "paper_revalidated",
            "no_opportunity",
        ):
            return {"status": candidate["status"], "reason": "terminal candidate"}
        replacement = self.nonces.get((sender, nonce))
        if replacement != tx_hash and replacement in self.candidates:
            self.candidates[replacement].update(
                status="invalidated",
                reason="nonce consumed by another hash",
                backrun=None,
                frontrun=None,
            )
        if receipt is None:
            candidate.update(
                status="awaiting_snapshot",
                reason="successful receipt and snapshot required",
                backrun=None,
                frontrun=None,
            )
            return copy.deepcopy(candidate)
        try:
            if (
                market.hex_value(receipt["transactionHash"], 32) != tx_hash
                or market.hex_value(receipt["from"], 20) != sender
            ):
                raise ValueError("receipt identity mismatch")
            success = market.integer(receipt["status"])
            if success not in (0, 1):
                raise ValueError("invalid receipt status")
            if success == 0:
                candidate.update(
                    status="reverted",
                    reason="target reverted",
                    backrun=None,
                    frontrun=None,
                )
                self.snapshot = None
                return copy.deepcopy(candidate)
            block = market.integer(receipt["blockNumber"])
            if self.snapshot is None or self.snapshot["block"] < block:
                raise ValueError("inclusion-block snapshot still required")
            if self.snapshot["block"] == block and (
                self.snapshot["block_hash"].lower()
                != market.hex_value(receipt["blockHash"], 32)
            ):
                raise ValueError("exact inclusion-block snapshot required")
            # A snapshot AHEAD of the receipt block is valid revalidation
            # state: cumulative block state already includes the target's
            # effects, and the receipt proves its inclusion. Sepolia can
            # advance past the receipt while the feed burst is still being
            # processed; the old exact-block equality dead-ended those
            # candidates until TTL. Exact block-pinned quotes at send time
            # keep every real guarantee - the paper plan is only a hint.
            pools = self.pools(now)
            # Do not apply the target again: the refreshed end-of-block state already includes it.
            plan = backrun.best_cycle(pools, self.limits)
        except (ValueError, KeyError, TypeError):
            candidate.update(
                status="awaiting_snapshot",
                reason="verified receipt and fresh matching snapshot required",
                backrun=None,
                frontrun=None,
            )
            return copy.deepcopy(candidate)
        candidate.update(
            status="paper_revalidated" if plan else "no_opportunity",
            backrun=plan,
            frontrun=None,
            reason="actual end-of-block state; exact eth_call validation still required",
            snapshot_block=self.snapshot["block"],
            snapshot_hash=self.snapshot["block_hash"],
        )
        return copy.deepcopy(candidate)
