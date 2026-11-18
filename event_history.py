"""Bounded Phase 2 market observations. Display prices never become calldata."""

import json
import math

from web3.exceptions import BlockNotFound

import event_market as market
import ledger

MAX_BYTES = 5_000_000
GAP_SECONDS = 60
MODES = ("paper", "simulate", "live")
IDENTITY = {
    "chain_id": 11155111,
    "base": market.BASE,
    "quote": market.QUOTE,
    "pools": {str(fee): address for fee, address in market.POOLS.items()},
}


def observation(snapshot, mode, session, now):
    # This is called only after the service accepts a fully reconciled snapshot.
    prices = {
        str(p["fee"]): (2**96 / p["sqrt" + "PriceX96"]) ** 2
        for p in snapshot["profiles"]
    }
    row = {
        "identity": IDENTITY,
        "mode": mode,
        "session": session,
        "observed": now,
        "timestamp": snapshot["timestamp"],
        "block": snapshot["block"],
        "block_hash": snapshot["block_hash"],
        "prices": prices,
    }
    validate(row)
    return row


def validate(row):
    if (
        not isinstance(row, dict)
        or not {
            "identity",
            "mode",
            "timestamp",
            "observed",
            "session",
            "block",
            "block_hash",
            "prices",
        }
        <= set(row)
        or not isinstance(row["prices"], dict)
    ):
        raise ValueError("invalid price observation schema")
    if row["identity"] != IDENTITY or row["mode"] not in MODES:
        raise ValueError("price history market or mode mismatch")
    for key in ("timestamp", "observed", "session"):
        if (
            type(row[key]) not in (int, float)
            or not math.isfinite(row[key])
            or row[key] < 0
        ):
            raise ValueError("invalid price observation time")
    if type(row["block"]) is not int or row["block"] < 0:
        raise ValueError("invalid price observation block")
    market.hex_value(row["block_hash"], 32)
    if "canonical" in row and type(row["canonical"]) is not bool:
        raise ValueError("invalid price canonical status")
    if set(row["prices"]) != set(IDENTITY["pools"]) or any(
        type(p) not in (float, int) or not math.isfinite(p) or p <= 0
        for p in row["prices"].values()
    ):
        raise ValueError("invalid recorded prices")


def read(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("price history exceeds its size limit")
        rows = [json.loads(line) for line in raw.splitlines()]
        for row in rows:
            validate(row)
        return rows
    except FileNotFoundError:
        return []


class Recorder:
    def __init__(self, path):
        self.path, self.rows = path, None
        self.verified = False

    def invalidate(self):
        # Keep rejected observations on disk for diagnosis, but never plot them.
        rows = read(self.path) if self.rows is None else self.rows
        if any(r.get("canonical", True) for r in rows):
            self.save([r | {"canonical": False} for r in rows])
        self.verified = False

    def revalidate(self, get_block):
        if self.verified:
            return
        if self.rows is None:
            self.rows = read(self.path)
        anchor = next(
            (r for r in reversed(self.rows) if r.get("canonical", True)), None
        )
        if anchor:
            try:
                block_hash = "0x" + bytes(get_block(anchor["block"])["hash"]).hex()
            except BlockNotFound:
                block_hash = None
            if block_hash != anchor["block_hash"]:
                self.invalidate()
        self.verified = True

    def append(self, row):
        validate(row)
        if self.rows is None:
            self.rows = read(self.path)
        if (
            self.rows
            and all(
                self.rows[-1][key] == row[key]
                for key in ("block_hash", "mode", "session")
            )
            and self.rows[-1].get("canonical", True)
        ):
            return
        # A replacement/lower block invalidates the old branch of chart observations.
        rows = [r for r in self.rows if r["block"] < row["block"]] + [row]
        self.save(rows)

    def save(self, rows):
        lines = [
            json.dumps(r, allow_nan=False, separators=(",", ":")) + "\n" for r in rows
        ]
        if sum(map(len, lines)) > MAX_BYTES:
            size, start = 0, len(lines)
            for line in reversed(lines):
                if size + len(line) > MAX_BYTES // 2:
                    break
                size += len(line)
                start -= 1
            rows, lines = rows[start:], lines[start:]
        # Readers get a complete file even during compaction or reorg replacement.
        ledger.atomic_write(self.path, "".join(lines))
        self.rows = rows


def segments(rows, mode, fee, lo, hi):
    result, previous = [], None
    for row in rows:
        if (
            not row.get("canonical", True)
            or (mode != "all" and row["mode"] != mode)
            or not lo <= row["timestamp"] <= hi
        ):
            previous = None
            continue
        if (
            previous is None
            or row["mode"] != previous["mode"]
            or row["session"] != previous["session"]
            or not 0 < row["timestamp"] - previous["timestamp"] <= GAP_SECONDS
        ):
            result.append([])
        result[-1].append((row["timestamp"], row["prices"][str(fee)]))
        previous = row
    return result
