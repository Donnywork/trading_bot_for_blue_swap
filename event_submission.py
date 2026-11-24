"""Public local timings and observed order; never infer unseen submission times."""

import time

from event_snapshot import hexstr


class Timing:
    def __init__(self, first_seen=None, clock=time.time, monotonic=time.monotonic):
        self.clock, self.monotonic = clock, monotonic
        self.started = monotonic()
        self.seen = first_seen["mono"] if first_seen else None
        self.points = {}
        self.data = {
            "first_seen_at": first_seen["at"] if first_seen else None,
            "first_seen_source": first_seen.get("source") if first_seen else None,
            "queue_wait_ms": None,
            "dispatch_ms": None,
            "validation_started_at": clock(),
            "validation_complete_at": None,
            "broadcast_start_at": None,
            "broadcast_return_at": None,
            "provider_accepted": None,
            "validation_ms": None,
            "first_seen_to_broadcast_ms": None,
            "validation_to_broadcast_ms": None,
            "broadcast_ms": None,
        }

    def mark(self, stage):
        now = self.monotonic()
        self.points[stage] = now
        self.data[stage + "_at"] = self.clock()
        if stage == "validation_complete":
            self.data["validation_ms"] = round((now - self.started) * 1000, 3)
        elif stage == "broadcast_start":
            self.data["first_seen_to_broadcast_ms"] = (
                round((now - self.seen) * 1000, 3) if self.seen is not None else None
            )
            completed = self.points.get("validation_complete")
            self.data["validation_to_broadcast_ms"] = (
                round((now - completed) * 1000, 3) if completed is not None else None
            )
        elif stage == "broadcast_return":
            self.data["provider_accepted"] = True
            self.data["broadcast_ms"] = round(
                (now - self.points["broadcast_start"]) * 1000, 3
            )


def relationship(receipt, other):
    if not other:
        return None
    result = dict(other)
    result["block_delta"] = receipt["blockNumber"] - other["block"]
    result["index_delta"] = None
    if result["block_delta"] == 0:
        if hexstr(receipt["blockHash"]) != other["block_hash"]:
            return None  # Different branches cannot establish order.
        index = receipt.get("transactionIndex")
        if index is not None and other.get("transaction_index") is not None:
            result["index_delta"] = index - other["transaction_index"]
    return result


def inclusion(receipt, header=None, target=None, competitor=None):
    price = receipt.get("effectiveGasPrice")
    base = header.get("baseFeePerGas") if header else None
    priority = (
        price - base
        if type(price) is int and type(base) is int and price >= base
        else None
    )
    return {
        "block": receipt["blockNumber"],
        "block_hash": hexstr(receipt["blockHash"]),
        "transaction_index": receipt.get("transactionIndex"),
        "block_time": header.get("timestamp") if header else None,
        "gas_used": receipt.get("gasUsed"),
        "effective_gas_price_wei": price,
        "effective_priority_wei": priority,
        "target": relationship(receipt, target),
        "competitor": relationship(receipt, competitor),
    }
