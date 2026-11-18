"""Bounded Phase 2 fee evidence. No signer, sends, scans or transaction lookups."""

import copy
import threading
import time

import event_market as market
import rpc_metrics as rpc
from event_snapshot import hexstr
from runtime import ConfigurationError

WINDOW = 8
PERCENTILE = 90
REFRESH_SECONDS = 12
NETWORK_SECONDS = 36
NETWORK_BLOCKS = 3
OBSERVATION_SECONDS = 60
MINED_BLOCKS = 5
MAX_OBSERVATIONS = 256
MAX_HASHES = 512


def quantity(value):
    number = market.integer(value)
    if not 0 <= number < 2**256:
        raise ValueError("invalid fee quantity")
    return number


def multiply(value, bps):
    return (value * bps + 9999) // 10000


def hash_text(value):
    return market.hex_value(value if isinstance(value, str) else hexstr(value), 32)


def fee_fields(tx):
    kind = quantity(tx.get("type", 0))
    if kind == 2 or "maxFeePerGas" in tx or "maxPriorityFeePerGas" in tx:
        if kind not in (0, 2):
            raise ValueError("unsupported fee type")
        maximum, priority = (
            quantity(tx.get(k)) for k in ("maxFeePerGas", "maxPriorityFeePerGas")
        )
        if priority > maximum:
            raise ValueError("invalid priority fee")
        return {"type": "eip1559", "max_fee_wei": maximum, "priority_wei": priority}
    if kind not in (0, 1):
        raise ValueError("unsupported fee type")
    return {"type": "legacy", "gas_price_wei": quantity(tx.get("gasPrice"))}


def effective_tip(fields, base_fee):
    if fields["type"] == "eip1559":
        return max(0, min(fields["priority_wei"], fields["max_fee_wei"] - base_fee))
    return max(0, fields["gas_price_wei"] - base_fee)


def reference(header, now):
    try:
        result = {
            k: quantity(header[k]) for k in ("number", "timestamp", "baseFeePerGas")
        }
        result["hash"] = hash_text(header["hash"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ConfigurationError(
            "fresh base-fee reference unavailable or invalid"
        ) from None
    if not 0 <= now - result["timestamp"] <= 30:
        raise ConfigurationError("fee reference block is stale or in the future")
    return result


def history_sample(value, header):
    oldest = quantity(value["oldestBlock"])
    rewards, bases = value["reward"], value["baseFeePerGas"]
    if (
        len(rewards) != WINDOW
        or len(bases) != WINDOW + 1
        or oldest + WINDOW - 1 != header["number"]
    ):
        raise ValueError("invalid fee history window")
    bases = [quantity(v) for v in bases]
    if bases[-2] != header["baseFeePerGas"]:
        raise ValueError("fee history reference changed")
    tips = []
    for row in rewards:
        if len(row) != 1:
            raise ValueError("invalid fee history reward")
        tips.append(quantity(row[0]))
    positive = [tip for tip in tips if tip > 0]
    return max(positive) if positive else None


class FeeCache:
    def __init__(self, w3, settings, clock=time.time, monotonic=time.monotonic):
        self.w3, self.settings = w3, settings
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.Lock()
        self.thread, self.next_run = None, float("-inf")
        self.epoch = 0
        self.network = None
        self.error = ""
        self.observations, self.seen = {}, {}

    def invalidate(self):
        with self.lock:
            self.epoch += 1
            self.network = None
            self.observations.clear()
            self.seen.clear()

    def prune(self, now):
        self.observations = {
            k: v
            for k, v in self.observations.items()
            if 0 <= now - v["mono"] <= OBSERVATION_SECONDS
        }
        self.seen = {
            k: v
            for k, v in self.seen.items()
            if 0 <= now - v["mono"] <= OBSERVATION_SECONDS
        }
        while len(self.observations) > MAX_OBSERVATIONS:
            self.observations.pop(next(iter(self.observations)))
        while len(self.seen) > MAX_HASHES:
            self.seen.pop(next(iter(self.seen)))

    def observe(self, tx, *, received=None, received_mono=None):
        """Use feed signers, never Swap sender/router fields; no RPC work here."""
        now, mono = self.clock(), self.monotonic()
        received = now if received is None else received
        received_mono = mono if received_mono is None else received_mono
        if (
            not 0 <= now - received <= OBSERVATION_SECONDS
            or not 0 <= mono - received_mono <= OBSERVATION_SECONDS
        ):
            return
        try:
            sender, nonce, tx_hash = market.transaction_identity(tx)
            if sender not in self.settings.market_bots + self.settings.competitors:
                return
            inclusion = None
            if tx.get("blockNumber") is not None:
                inclusion = {
                    "block": quantity(tx["blockNumber"]),
                    "block_hash": hash_text(tx["blockHash"]),
                    "transaction_index": quantity(tx["transactionIndex"]),
                    "source": "mined_feed_unverified",
                }
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            self.prune(mono)
            old = self.seen.get(tx_hash)
            if old:
                if inclusion:
                    old["inclusion"] = inclusion
                else:
                    return  # A delayed duplicate cannot revive a superseded bid.
            else:
                self.seen[tx_hash] = {
                    "at": received,
                    "mono": received_mono,
                    "inclusion": inclusion,
                    "source": "mined_feed" if inclusion else "pending_feed",
                }
            if (
                sender in self.settings.competitors
                and self.settings.fee_policy == "competitive"
            ):
                key = sender, nonce
                previous = self.observations.get(key)
                if previous and previous["inclusion"] and not inclusion:
                    self.prune(mono)
                    return  # Observed inclusion consumes this nonce until recovery/expiry.
                # Unsupported or malformed replacements invalidate the prior bid too.
                row = {
                    "hash": tx_hash,
                    "sender": sender,
                    "nonce": nonce,
                    "at": received,
                    "mono": received_mono,
                    "inclusion": inclusion,
                    "fields": None,
                }
                try:
                    swap = market.decode_swap(
                        dict(tx) | {"blockNumber": None}, 0 if inclusion else now
                    )
                    row["deadline"] = swap["deadline"] if not inclusion else None
                    row["fields"] = fee_fields(tx)
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
                if previous and previous["hash"] == tx_hash:
                    row["at"], row["mono"] = previous["at"], previous["mono"]
                self.observations.pop(key, None)
                self.observations[key] = row
            self.prune(mono)

    def first_seen(self, tx_hash):
        with self.lock:
            self.prune(self.monotonic())
            return copy.deepcopy(self.seen.get(tx_hash))

    def confirm(self, tx, receipt, snapshot):
        """Enrich only from receipt work the service already performed."""
        try:
            sender, nonce, tx_hash = market.transaction_identity(tx)
            if (
                hash_text(receipt["transactionHash"]) != tx_hash
                or receipt["from"].lower() != sender
                or quantity(receipt["status"]) not in (0, 1)
                or quantity(receipt["blockNumber"]) != snapshot["block"]
                or hash_text(receipt["blockHash"]) != snapshot["block_hash"]
            ):
                return
            index = receipt.get("transactionIndex")
            inclusion = {
                "block": snapshot["block"],
                "block_hash": snapshot["block_hash"],
                "transaction_index": quantity(index) if index is not None else None,
                "source": "receipt_matching_snapshot",
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            if tx_hash in self.seen:
                self.seen[tx_hash]["inclusion"] = inclusion
            rival = self.observations.get((sender, nonce))
            if rival and rival["hash"] == tx_hash:
                rival["inclusion"] = inclusion

    def schedule(self):
        if self.settings.fee_policy != "competitive":
            return
        with self.lock:
            if (
                self.monotonic() < self.next_run
                or self.thread
                and self.thread.is_alive()
            ):
                return
            self.next_run = self.monotonic() + REFRESH_SECONDS
            self.thread = threading.Thread(
                target=self.refresh, args=(self.epoch,), daemon=True, name="event-fees"
            )
            self.thread.start()

    def refresh(self, epoch):
        # Locks cover memory only. Shared provider pacing and metrics cover all I/O.
        try:
            with rpc.purpose(rpc.FEES):
                header = reference(self.w3.eth.get_block("latest"), self.clock())
                with self.lock:
                    if epoch != self.epoch:
                        return
                    if self.network and self.network["block_hash"] == header["hash"]:
                        return  # Do not refresh ages or repeatedly query one block.
                value = self.w3.eth.fee_history(WINDOW, header["number"], [PERCENTILE])
                tip = history_sample(value, header)
                if (
                    hash_text(self.w3.eth.get_block(header["number"])["hash"])
                    != header["hash"]
                ):
                    raise ValueError("fee history reorganized")
                reference(
                    header, self.clock()
                )  # A slow response must not become fresh evidence.
            sample = {
                "tip_wei": tip,
                "block": header["number"],
                "block_hash": header["hash"],
                "block_time": header["timestamp"],
                "at": self.clock(),
                "mono": self.monotonic(),
                "source": "fee_history_8_blocks_p90_max",
            }
            with self.lock:
                if epoch == self.epoch:
                    self.network, self.error = sample, ""
        except Exception:  # noqa: BLE001 — background fee failures must never stop exits
            with self.lock:
                if epoch == self.epoch:
                    self.error = (
                        "Fee refresh failed; retained evidence keeps its original age."
                    )

    def inputs(self, header):
        with self.lock:
            mono = self.monotonic()
            self.prune(mono)
            network = copy.deepcopy(self.network)
            error = self.error
            competitors = copy.deepcopy(list(self.observations.values()))
        state = "missing"
        if network:
            age = mono - network.pop("mono")
            network["age_seconds"] = age
            fresh = (
                0 <= age <= NETWORK_SECONDS
                and 0 <= self.clock() - network["block_time"] <= NETWORK_SECONDS
                and 0 <= header["number"] - network["block"] <= NETWORK_BLOCKS
                and (
                    header["number"] != network["block"]
                    or header["hash"] == network["block_hash"]
                )
            )
            state = (
                "fresh"
                if fresh and network["tip_wei"] is not None
                else "zero_samples"
                if fresh
                else "stale"
            )
            network["usable"] = state == "fresh"
        rivals = []
        for row in competitors:
            if row["fields"] is None:
                continue
            if row.get("deadline") and row["deadline"] <= self.clock():
                continue
            inclusion = row["inclusion"]
            if inclusion and (
                not 0 <= header["number"] - inclusion["block"] <= MINED_BLOCKS
                or inclusion["block"] == header["number"]
                and inclusion["block_hash"] != header["hash"]
            ):
                continue
            row["age_seconds"] = mono - row.pop("mono")
            row["tip_wei"] = effective_tip(row["fields"], header["baseFeePerGas"])
            row["source"] = (
                "mined_feed_bid_estimate" if inclusion else "pending_feed_bid_estimate"
            )
            rivals.append(row)
        competitor = max(rivals, key=lambda r: r["tip_wei"], default=None)
        return network, state, competitor, error

    def decide(self, header, action):
        now = self.clock()
        header = reference(header, now)
        s = self.settings
        base = header["baseFeePerGas"]
        reasons = []
        network, state, competitor, error = (None, "not_used", None, "")
        if s.fee_policy == "fixed":
            desired_tip = tip = s.priority_wei
            desired_max = maximum = s.max_fee_wei
            allowed = maximum >= base + tip
            if not allowed:
                reasons.append("fixed_max_fee_below_base_plus_tip")
        else:
            network, state, competitor, error = self.inputs(header)
            desired_tip = max(
                s.fee_baseline_wei,
                multiply(network["tip_wei"], s.fee_network_bps)
                if state == "fresh"
                else 0,
                multiply(competitor["tip_wei"], s.fee_competitor_bps)
                if competitor
                else 0,
            )
            tip = min(desired_tip, s.fee_tip_cap_wei, max(0, s.fee_max_cap_wei - base))
            desired_max = 2 * base + tip
            maximum = min(desired_max, s.fee_max_cap_wei)
            allowed = tip > 0 and maximum >= base + tip
            if state != "fresh":
                reasons.append("network_" + state)
            if competitor is None:
                reasons.append("competitor_missing")
            if desired_tip > s.fee_tip_cap_wei:
                reasons.append("priority_ceiling")
            if tip < min(desired_tip, s.fee_tip_cap_wei):
                reasons.append("max_fee_ceiling_reduced_tip")
            if maximum < desired_max:
                reasons.append("max_fee_ceiling_reduced_headroom")
            if tip < s.fee_baseline_wei:
                reasons.append("baseline_unavailable_under_ceiling")
            if not allowed:
                reasons.append("no_positive_tip_fits_current_base_fee")
        return {
            "policy": s.fee_policy,
            "action": action,
            "decided_at": now,
            "reference_block": header["number"],
            "reference_hash": header["hash"],
            "reference_time": header["timestamp"],
            "base_fee_wei": base,
            "baseline_wei": s.priority_wei
            if s.fee_policy == "fixed"
            else s.fee_baseline_wei,
            "network_bps": s.fee_network_bps if s.fee_policy == "competitive" else None,
            "competitor_bps": s.fee_competitor_bps
            if s.fee_policy == "competitive"
            else None,
            "tip_cap_wei": s.fee_tip_cap_wei if s.fee_policy == "competitive" else None,
            "max_cap_wei": s.fee_ceiling,
            "network": network,
            "network_status": state,
            "competitor": competitor,
            "refresh_error": error,
            "desired_priority_wei": desired_tip,
            "desired_max_fee_wei": desired_max,
            "selected_priority_wei": tip,
            "selected_max_fee_wei": maximum,
            "allowed": allowed,
            "reasons": reasons,
            "selected_tip_above_observed_estimate": tip > competitor["tip_wei"]
            if competitor and allowed
            else None,
            "ordering_guaranteed": False,
        }
