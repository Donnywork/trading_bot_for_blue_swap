"""Public, bounded Swap history. A router's event sender is never actor evidence."""

import json
import math
from decimal import Decimal, localcontext

from eth_abi import decode
from web3 import Web3

import event_history
import event_market as market
import ledger

MAX_BYTES = 5_000_000
MAX_ROWS = 4000
SWAP_TOPIC = Web3.to_hex(
    Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)")
)


def hex_text(value):
    return value.lower() if isinstance(value, str) else Web3.to_hex(value).lower()


def spot(sqrt):
    with localcontext() as ctx:
        ctx.prec = 80
        if type(sqrt) is not int or sqrt <= 0:
            raise ValueError("invalid spot price")
        return str(Decimal(2**192) / Decimal(sqrt) ** 2)


def execution_price(quote, base):
    with localcontext() as ctx:
        ctx.prec = 80
        return str(Decimal(quote) / Decimal(base)) if base else None


def swap_row(event, fee, *, timestamp=None, mode="live", session=0):
    args = event["args"]
    quote, base = int(args["amount0"]), int(args["amount1"])
    if quote * base >= 0:
        raise ValueError("invalid swap amounts")
    tx = hex_text(event["transactionHash"])
    return {
        "id": f"{tx}:{int(event['logIndex'])}",
        "tx_hash": tx,
        "block": int(event["blockNumber"]),
        "block_hash": hex_text(event["blockHash"]),
        "transaction_index": int(event["transactionIndex"]),
        "log_index": int(event["logIndex"]),
        "pool": market.POOLS[fee],
        "fee": fee,
        "timestamp": timestamp,
        "side": "buy" if base < 0 else "sell",
        "base_raw": str(abs(base)),
        "quote_raw": str(abs(quote)),
        "execution_price": execution_price(abs(quote), abs(base)),
        "spot_price": spot(args["sqrt" + "PriceX96"]),
        "actor": None,
        "category": "unknown",
        "status": "confirmed",
        "canonical": True,
        "observed_mode": mode,
        "session": session,
    }


def receipt_legs(receipt, timestamp):
    result = []
    for log in receipt.get("logs", []):
        fee = next(
            (f for f, a in market.POOLS.items() if a == log["address"].lower()), None
        )
        if (
            fee is None
            or not log.get("topics")
            or hex_text(log["topics"][0]) != SWAP_TOPIC
        ):
            continue
        raw = log["data"]
        values = decode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            bytes.fromhex(raw[2:]) if isinstance(raw, str) else bytes(raw),
        )
        event = dict(log) | {
            "blockNumber": receipt["blockNumber"],
            "blockHash": receipt["blockHash"],
            "transactionHash": receipt["transactionHash"],
            "transactionIndex": receipt.get(
                "transactionIndex", log.get("transactionIndex", 0)
            ),
            "args": dict(
                zip(
                    ("amount0", "amount1", "sqrt" + "PriceX96", "liquidity", "tick"),
                    values,
                    strict=True,
                )
            ),
        }
        result.append(swap_row(event, fee, timestamp=timestamp))
    return result


def classify(sender, actors):
    if not sender:
        return "unknown"
    sender = market.hex_value(sender.lower(), 20)
    if sender in actors["market_bots"]:
        return "market_bot"
    if sender in actors["competitors"]:
        return "competitor"
    return "other"


def validate(value):
    if value.get("version") != 1 or value.get("identity") != event_history.IDENTITY:
        raise ValueError("activity identity mismatch")
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("invalid activity rows")
    ids = set()
    for row in rows:
        market.hex_value(row["tx_hash"], 32)
        market.hex_value(row["block_hash"], 32)
        if row["pool"] != market.POOLS[row["fee"]] or row["id"] in ids:
            raise ValueError("invalid activity identity")
        ids.add(row["id"])
        if row["category"] not in (
            "mine",
            "market_bot",
            "competitor",
            "other",
            "unknown",
        ):
            raise ValueError("invalid actor category")
        if row["side"] not in ("buy", "sell") or row["status"] != "confirmed":
            raise ValueError("invalid swap status")
        if type(row["canonical"]) is not bool or (
            row["timestamp"] is not None
            and (
                type(row["timestamp"]) not in (int, float)
                or not math.isfinite(row["timestamp"])
                or row["timestamp"] < 0
            )
        ):
            raise ValueError("invalid canonical status/time")
        if row["actor"] is not None:
            market.hex_value(row["actor"], 20)
        for key in ("execution_price", "spot_price"):
            if row[key] is not None:
                price = Decimal(row[key])
                if not price.is_finite() or price <= 0:
                    raise ValueError("invalid activity price")
        for key in ("block", "transaction_index", "log_index"):
            if key != "block" and row[key] is None and row.get("source"):
                continue
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError("invalid swap ordering")
        for key in ("base_raw", "quote_raw"):
            if int(row[key]) <= 0:
                raise ValueError("invalid swap amount")


def read(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("activity exceeds size limit")
        value = json.loads(raw)
        validate(value)
        return value
    except FileNotFoundError:
        return {
            "version": 1,
            "identity": event_history.IDENTITY,
            "rows": [],
            "anchor": None,
            "verified": False,
            "started": None,
            "dropped": 0,
        }


def save(path, value):
    value["rows"].sort(
        key=lambda r: (r["block"], r["transaction_index"] or 0, r["log_index"] or 0)
    )
    while len(value["rows"]) > MAX_ROWS:
        value["rows"].pop(0)
        value["dropped"] += 1
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False)
    while len(raw) > MAX_BYTES:
        value["rows"] = value["rows"][len(value["rows"]) // 4 + 1 :]
        value["compacted"] = True
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False)
    validate(value)
    ledger.atomic_write(path, raw)


def merge(value, incoming):
    rows = {row["id"]: row for row in value["rows"]}
    for row in incoming:
        old = rows.get(row["id"])
        if (
            old
            and old["block_hash"] == row["block_hash"]
            and old["canonical"]
            and row["category"] != "mine"
        ):
            # A catch-up log must not discard receipt/actor enrichment.
            row = row | {k: v for k, v in old.items() if v is not None}
        rows[row["id"]] = row
    value["rows"] = list(rows.values())


def invalidate(value, reason):
    for row in value["rows"]:
        row["canonical"] = False
    value["verified"] = False
    value["anchor"] = None
    value["error"] = reason


def own_rows(trades, address):
    result = []
    for trade in trades:
        if trade.get("status") != "confirmed":
            continue
        legs = trade.get("legs", [])
        # A confirmed single fill still has an exact receipt-derived price when
        # old records lack a decoded pool log. Its log ordering is unavailable.
        if (
            not legs
            and trade.get("fee") in market.POOLS
            and trade["action"] in ("entry", "exit")
        ):
            buy = trade["action"] == "entry"
            base = trade["received_raw"] if buy else trade["amount_raw"]
            quote = trade["amount_raw"] if buy else trade["received_raw"]
            legs = [
                {
                    "id": trade["tx_hash"] + ":receipt",
                    "tx_hash": trade["tx_hash"],
                    "block": trade["block"],
                    "block_hash": trade["block_hash"],
                    "transaction_index": trade.get("transaction_index"),
                    "log_index": None,
                    "fee": trade["fee"],
                    "pool": market.POOLS[trade["fee"]],
                    "timestamp": trade.get("block_time"),
                    "side": "buy" if buy else "sell",
                    "base_raw": str(base),
                    "quote_raw": str(quote),
                    "execution_price": execution_price(quote, base),
                    "spot_price": None,
                    "status": "confirmed",
                    "canonical": True,
                    "observed_mode": "live",
                    "session": 0,
                    "source": "single receipt amounts; log ordering unavailable",
                }
            ]
        for leg in legs:
            result.append(
                leg
                | {
                    "category": "mine",
                    "actor": trade.get("operator"),
                    "contract": address,
                    "action": trade["action"],
                    "position_id": trade.get("position_id"),
                    "profit_raw": str(trade["profit_raw"])
                    if "profit_raw" in trade
                    else None,
                }
            )
    return result
