"""Offline event replay and actor analysis. Run after collect.py logs/enrich."""

import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import v3pool

OUT = Path(__file__).parent
TARGET = "0x6011139d5f16689bde1d245c6247b088f9582026"
POOLS = {
    "0x68d8bc396df8e791efd45a9b214cb07fe4300f48": 500,
    "0xc67fcb8459c792ae1e271f24ce212ba90ecc2920": 10000,
}


def load(name, default=None):
    path = OUT / name
    return json.loads(path.read_text()) if path.exists() else default


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n")


def num(value):
    return int(value, 16) if isinstance(value, str) else value


def decode():
    w3 = Web3()
    abis = json.loads((ROOT / "abis/pool.json").read_text())
    events = {
        Web3.keccak(
            text=a["name"] + "(" + ",".join(i["type"] for i in a["inputs"]) + ")"
        ).to_0x_hex(): a
        for a in abis
        if a["type"] == "event"
    }
    rows = []
    for log in load("pool_events.json"):
        abi = events[log["topics"][0]]
        indexed = iter(log["topics"][1:])
        plain = [i for i in abi["inputs"] if not i["indexed"]]
        values = iter(
            w3.codec.decode([i["type"] for i in plain], bytes.fromhex(log["data"][2:]))
        )
        args = {
            i["name"]: w3.codec.decode([i["type"]], bytes.fromhex(next(indexed)[2:]))[0]
            if i["indexed"]
            else next(values)
            for i in abi["inputs"]
        }
        rows.append(
            {
                "block": num(log["blockNumber"]),
                "tx_index": num(log["transactionIndex"]),
                "log_index": num(log["logIndex"]),
                "hash": log["transactionHash"],
                "pool": log["address"].lower(),
                "event": abi["name"],
                "args": args,
            }
        )
    return sorted(rows, key=lambda r: (r["block"], r["tx_index"], r["log_index"]))


def profile(state):
    return {
        "d0": 18,
        "d1": 18,
        "fee": state["fee"],
        "base_is_token0": False,
        "ticks": [{"tick": t, "net": n} for t, n in sorted(state["net"].items()) if n],
    }


def best_cycle(states, ceiling=10000):
    if any(not s.get("sqrt") or not s["net"] for s in states.values()):
        return None
    pools = list(states)
    best = {"profit": 0.0, "input": 0.0}
    for buy, sell in (pools, pools[::-1]):
        b, s = states[buy], states[sell]
        pb, ps = (2**96 / b["sqrt"]) ** 2, (2**96 / s["sqrt"]) ** 2
        if ps / pb * (1 - b["fee"] / 1e6) * (1 - s["fee"] / 1e6) <= 1:
            continue
        bp, sp = profile(b), profile(s)

        def profit(size, bp=bp, pb=pb, sp=sp, ps=ps):
            try:
                base = v3pool.V3Pool(bp, pb).buy_with_quote(size)
                return v3pool.V3Pool(sp, ps).sell_base(base) - size
            except (ValueError, ZeroDivisionError):
                return -size

        # For standard V3 pools, marginal cycle output decreases with size.
        lo, hi = 0.0, float(ceiling)
        for _ in range(65):
            x, y = lo + (hi - lo) / 3, hi - (hi - lo) / 3
            if profit(x) < profit(y):
                lo = x
            else:
                hi = y
        size = (lo + hi) / 2
        gain = profit(size)
        if gain > best["profit"]:
            best = {
                "buy_fee": b["fee"],
                "sell_fee": s["fee"],
                "input": size,
                "profit": gain,
                "return_pct": gain / size * 100,
                "buy_price": pb,
                "sell_price": ps,
                "at_ceiling": size > ceiling - 0.01,
                "size_profits": {
                    str(q): profit(q) for q in (100, 500, 1000, 2500, 5000, 10000)
                },
            }
    return best


def replay(rows):
    states = {p: {"fee": fee, "net": defaultdict(int)} for p, fee in POOLS.items()}
    snapshot = json.loads(
        (ROOT / "research/2026-11-17-phase2-arbitrage/snapshot.json").read_text()
    )
    block_rows = defaultdict(list)
    for row in rows:
        block_rows[row["block"]].append(row)
    opportunities, errors, liquidity_changes = [], [], []
    checked_snapshot = False

    def check_snapshot():
        for p in snapshot["profiles"]:
            s = states[p["pool_address"]]
            assert s["sqrt"] == p["sqrtPriceX96"]
            assert {t: n for t, n in s["net"].items() if n} == {
                t["tick"]: t["net"] for t in p["ticks"] if t["net"]
            }

    for block, events in block_rows.items():
        if block > snapshot["block"] and not checked_snapshot:
            check_snapshot()
            checked_snapshot = True
        for row in events:
            s, a = states[row["pool"]], row["args"]
            if row["event"] == "Initialize":
                assert "sqrt" not in s
                s["sqrt"], s["tick"] = a["sqrtPriceX96"], a["tick"]
            elif row["event"] in ("Mint", "Burn"):
                change = a["amount"] * (1 if row["event"] == "Mint" else -1)
                s["net"][a["tickLower"]] += change
                s["net"][a["tickUpper"]] -= change
                liquidity_changes.append(
                    row
                    | {
                        "fee": s["fee"],
                        "lower_price": 1.0001 ** -a["tickUpper"],
                        "upper_price": 1.0001 ** -a["tickLower"],
                    }
                )
            elif row["event"] == "Swap":
                assert "sqrt" in s, "history missing Initialize"
                before = (2**96 / s["sqrt"]) ** 2
                model = v3pool.V3Pool(profile(s), before)
                if a["amount0"] > 0:
                    observed, predicted = (
                        -a["amount1"] / 1e18,
                        model.buy_with_quote(a["amount0"] / 1e18),
                    )
                else:
                    observed, predicted = (
                        -a["amount0"] / 1e18,
                        model.sell_base(a["amount1"] / 1e18),
                    )
                error_f564 = abs(predicted - observed) * (
                    before if a["amount0"] > 0 else 1
                )
                errors.append(error_f564)
                assert error_f564 < 0.001, (row["hash"], error_f564)
                s["sqrt"], s["tick"] = a["sqrtPriceX96"], a["tick"]
                assert (
                    sum(n for t, n in s["net"].items() if t <= s["tick"])
                    == a["liquidity"]
                )
                row.update(
                    {
                        "price_before": before,
                        "price_after": (2**96 / s["sqrt"]) ** 2,
                        "side": "buy" if a["amount0"] > 0 else "sell",
                        "f564": abs(a["amount0"]) / 1e18,
                        "feth": abs(a["amount1"]) / 1e18,
                        "fee": s["fee"],
                    }
                )
        # Only states after ALL transactions in a block are actionable from mined data.
        result = best_cycle(states)
        if result is not None:
            opportunities.append(
                {"block": block, "last_tx": events[-1]["hash"], **result}
            )
    if not checked_snapshot:
        check_snapshot()
    save("opportunities.json", opportunities)
    save("liquidity_changes.json", liquidity_changes)
    save("decoded_events.json", rows)
    result = {
        "events": dict(Counter(r["event"] for r in rows)),
        "swaps": len(errors),
        "max_replay_error_f564": max(errors),
        "snapshot_matches": True,
        "states_evaluated": len(opportunities),
        "positive_states_above_0_01": sum(r["profit"] > 0.01 for r in opportunities),
        "top_opportunities": sorted(
            opportunities, key=lambda r: r["profit"], reverse=True
        )[:12],
    }
    save("replay_summary.json", result)
    print(json.dumps(result, indent=2))


def distribution(values):
    if not values:
        return None
    values = sorted(values)
    return {
        "min": values[0],
        "median": statistics.median(values),
        "max": values[-1],
        "p90": values[min(len(values) - 1, int(len(values) * 0.9))],
    }


def actors(rows):
    blocks, txs, receipts = (
        load("blocks.json"),
        load("transactions.json"),
        load("receipts.json"),
    )
    manifest = load("manifest.json")
    assert manifest.get("enriched"), "finish enrichment first"
    swaps, groups = [], defaultdict(list)
    for row in rows:
        if row["event"] != "Swap":
            continue
        tx, receipt, header = (
            txs[row["hash"]],
            receipts[row["hash"]],
            blocks[str(row["block"])],
        )
        timestamp = num(header["timestamp"])
        gas_price = num(receipt["effectiveGasPrice"])
        item = {k: v for k, v in row.items() if k != "args"}
        item.update(
            {
                "from": tx["from"].lower(),
                "to": (tx["to"] or "").lower(),
                "recipient": row["args"]["recipient"].lower(),
                "sender": row["args"]["sender"].lower(),
                "timestamp": timestamp,
                "utc": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                "nonce": num(tx["nonce"]),
                "selector": tx["input"][:10],
                "gas_used": num(receipt["gasUsed"]),
                "gas_limit": num(tx["gas"]),
                "tip_gwei": (gas_price - num(header["baseFeePerGas"])) / 1e9,
                "gas_eth": gas_price * num(receipt["gasUsed"]) / 1e18,
                "type": num(tx.get("type", "0x0")),
            }
        )
        swaps.append(item)
        groups[item["from"]].append(item)
    summaries = []
    for address, items in groups.items():
        unique = list({r["hash"]: r for r in items}.values())
        gaps = [b["timestamp"] - a["timestamp"] for a, b in pairwise(unique)]
        same_pool_changes = []
        for pool in POOLS:
            subset = [r for r in items if r["pool"] == pool]
            same_pool_changes.extend(
                a["side"] != b["side"] for a, b in pairwise(subset)
            )
        summaries.append(
            {
                "address": address,
                "swaps": len(items),
                "transactions": len(unique),
                "pools": dict(Counter(str(r["fee"]) for r in items)),
                "sides": dict(Counter(r["side"] for r in items)),
                "targets": dict(Counter(r["to"] for r in unique)),
                "recipients": dict(Counter(r["recipient"] for r in items)),
                "selectors": dict(Counter(r["selector"] for r in unique)),
                "f564_volume": sum(r["f564"] for r in items),
                "size_f564": distribution([r["f564"] for r in items]),
                "gaps_seconds": distribution(gaps),
                "common_gaps": Counter(gaps).most_common(10),
                "tip_gwei": distribution([r["tip_gwei"] for r in unique]),
                "seconds_of_hour": Counter(
                    r["timestamp"] % 3600 for r in unique
                ).most_common(12),
                "direction_flip_rate": statistics.mean(same_pool_changes)
                if same_pool_changes
                else None,
                "first_utc": items[0]["utc"],
                "last_utc": items[-1]["utc"],
                "gas_eth": sum(r["gas_eth"] for r in unique),
            }
        )
    save("actors.json", sorted(summaries, key=lambda r: r["swaps"], reverse=True))
    save("swaps.json", swaps)
    with (OUT / "swaps.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=swaps[0].keys())
        writer.writeheader()
        writer.writerows(swaps)
    target = [
        r for r in swaps if TARGET in (r["from"], r["to"], r["recipient"], r["sender"])
    ]
    save("target_swaps.json", target)
    print(
        json.dumps(
            {
                "actors": len(groups),
                "target_swaps": len(target),
                "top": sorted(summaries, key=lambda r: r["swaps"], reverse=True)[:6],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["actors"]:
        actors(load("decoded_events.json"))
    else:
        replay(decode())
