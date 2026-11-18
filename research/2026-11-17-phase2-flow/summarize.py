"""Offline timings, opportunity intervals, and observed two-pool transaction flows."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise

from analyze import TARGET, distribution, load, num, save


def utc(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def main():
    swaps, blocks, opportunities = (
        load("swaps.json"),
        load("blocks.json"),
        load("opportunities.json"),
    )
    manifest, events = load("manifest.json"), load("decoded_events.json")
    timestamp = lambda block: num(blocks[str(block)]["timestamp"])
    episodes, active = [], None
    for i, r in enumerate(opportunities):
        start = timestamp(r["block"])
        end = (
            timestamp(opportunities[i + 1]["block"])
            if i + 1 < len(opportunities)
            else manifest["end_timestamp"]
        )
        r.update(
            {"utc": utc(start), "seconds_until_next_pool_event_block": end - start}
        )
        if r["profit"] > 0.01:
            if active is None:
                active = {
                    "start_block": r["block"],
                    "start": start,
                    "peak_profit": r["profit"],
                    "peak_block": r["block"],
                }
            if r["profit"] > active["peak_profit"]:
                active.update({"peak_profit": r["profit"], "peak_block": r["block"]})
            active["end"] = end
            active["seconds"] = end - active["start"]
        elif active:
            episodes.append(active)
            active = None
    if active:
        active["right_censored"] = True
        episodes.append(active)
    save("opportunities.json", opportunities)
    save("opportunity_episodes.json", episodes)
    target = [
        r for r in swaps if TARGET in (r["from"], r["to"], r["recipient"], r["sender"])
    ]
    per_pool = {}
    for fee in (500, 10000):
        items = [r for r in target if r["fee"] == fee]
        gaps = [b["timestamp"] - a["timestamp"] for a, b in pairwise(items)]
        per_pool[fee] = {
            "swaps": len(items),
            "gaps": distribution(gaps),
            "common_gaps": Counter(gaps).most_common(12),
            "buys": sum(r["side"] == "buy" for r in items),
            "direction_flips": sum(a["side"] != b["side"] for a, b in pairwise(items)),
            "size_f564": distribution([r["f564"] for r in items]),
        }
    tx_groups = defaultdict(list)
    for r in swaps:
        tx_groups[r["hash"]].append(r)
    cross_pool = []
    decoded = {r["hash"]: [] for r in swaps}
    for r in events:
        if r["event"] == "Swap":
            decoded[r["hash"]].append(r)
    for tx_hash, items in tx_groups.items():
        if len({r["pool"] for r in items}) < 2:
            continue
        a0 = sum(r["args"]["amount0"] for r in decoded[tx_hash])
        a1 = sum(r["args"]["amount1"] for r in decoded[tx_hash])
        cross_pool.append(
            {
                "hash": tx_hash,
                "from": items[0]["from"],
                "utc": items[0]["utc"],
                "block": items[0]["block"],
                "swaps": len(items),
                "net_f564_from_pools": -a0 / 1e18,
                "net_feth_from_pools": -a1 / 1e18,
                "gas_eth": items[0]["gas_eth"],
            }
        )
    save("observed_cross_pool.json", cross_pool)
    # Time to next different signer's swap. Association does not establish a response or intent.
    follow = []
    for r in target:
        subsequent = next(
            (
                x
                for x in swaps
                if (x["block"], x["tx_index"], x["log_index"])
                > (r["block"], r["tx_index"], r["log_index"])
                and x["from"] != r["from"]
            ),
            None,
        )
        if subsequent:
            follow.append(
                {
                    "target_hash": r["hash"],
                    "hash": subsequent["hash"],
                    "signer": subsequent["from"],
                    "seconds": subsequent["timestamp"] - r["timestamp"],
                    "blocks": subsequent["block"] - r["block"],
                }
            )
    save("following_trades.json", follow)
    positive = [r for r in opportunities if r["profit"] > 0.01]
    last_day = [
        r
        for r in positive
        if timestamp(r["block"]) >= manifest["end_timestamp"] - 86400
    ]
    summary = {
        "coverage_start_utc": utc(manifest["start_timestamp"]),
        "coverage_end_utc": manifest["end_utc"],
        "swaps_by_pool": dict(Counter(str(r["fee"]) for r in swaps)),
        "positive_event_block_states": len(positive),
        "positive_episodes": len(episodes),
        "positive_state_seconds": sum(
            r["seconds_until_next_pool_event_block"] for r in positive
        ),
        "episode_duration_seconds": distribution([r["seconds"] for r in episodes]),
        "positive_peak_profit_distribution": distribution(
            [r["profit"] for r in positive]
        ),
        "latest_day_top": sorted(last_day, key=lambda r: r["profit"], reverse=True)[:5],
        "target_per_pool": per_pool,
        "observed_cross_pool_transactions": len(cross_pool),
        "following_swap_delay_seconds": distribution([r["seconds"] for r in follow]),
        "following_within_36_seconds": dict(
            Counter(r["signer"] for r in follow if r["seconds"] <= 36)
        ),
        "target_signers": dict(Counter(r["from"] for r in target)),
    }
    save("summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
