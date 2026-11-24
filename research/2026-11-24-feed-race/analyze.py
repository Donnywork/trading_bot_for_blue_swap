"""Compare public-source pending arrivals with the bot's Alchemy feed.

Reads observations.jsonl (listen.py) and the live bot's
logs/events/decisions.jsonl, joins per transaction hash, and reports how
much earlier or later each public endpoint saw the same pending than the
bot's own subscription. Positive deltas mean the public source saw it
FIRST — racing it into the hot path would save that much (minus our own
processing). Read-only; run from the repo root:

    python research/2026-11-24-feed-race/analyze.py
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DECISIONS = Path("logs/events/decisions.jsonl")


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def main():
    bot = {}
    for line in DECISIONS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") != "pending":
            continue
        arrival = row.get("feed_arrival")
        if arrival is None and row.get("queue_wait_ms") is not None:
            # Rows from before feed_arrival existed: record time minus the
            # measured queue wait approximates the same arrival stamp (both
            # sampled from the same clock within milliseconds of dequeue).
            arrival = row["now"] - row["queue_wait_ms"] / 1000.0
        if arrival:
            # Keep the earliest sighting the bot recorded for the hash.
            bot[row["hash"]] = min(bot.get(row["hash"], arrival), arrival)
    per_endpoint = defaultdict(dict)
    for line in (HERE / "observations.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        seen = per_endpoint[row["endpoint"]]
        previous = seen.get(row["hash"])
        if previous is None or row["seen_at"] < previous[0]:
            seen[row["hash"]] = (row["seen_at"], row["full_tx"])
    for endpoint, seen in sorted(per_endpoint.items()):
        full = sum(1 for _, (_, flag) in seen.items() if flag)
        deltas = [
            (bot[hash_value] - seen_at) * 1000.0
            for hash_value, (seen_at, _) in seen.items()
            if hash_value in bot
        ]
        flavour = f"{full}/{len(seen)} full-tx" if seen else "no pendings"
        if not deltas:
            print(f"{endpoint}: {len(seen)} signers' pendings ({flavour}), none matched bot rows yet")
            continue
        print(
            f"{endpoint}: {len(seen)} signers' pendings ({flavour}),"
            f" {len(deltas)} matched bot rows | delta bot-minus-public ms:"
            f" median {statistics.median(deltas):.0f}"
            f" p10 {percentile(deltas, 0.1):.0f}"
            f" p90 {percentile(deltas, 0.9):.0f}"
            f" | public earlier: {sum(1 for d in deltas if d > 0)}/{len(deltas)}"
        )
    if not any(hash_value in bot for seen in per_endpoint.values() for hash_value in seen):
        print("No overlapping hashes yet — let both sides run through a few targets.")


if __name__ == "__main__":
    main()
