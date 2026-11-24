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
        at = row.get("first_seen_at") or (row.get("timing") or {}).get("first_seen_at")
        if row.get("kind") == "pending" and at:
            # Keep the earliest sighting the bot recorded for the hash.
            bot[row["hash"]] = min(bot.get(row["hash"], at), at)
    per_endpoint = defaultdict(dict)
    senders = {}
    for line in (HERE / "observations.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        senders[row["hash"]] = row["sender"]
        seen = per_endpoint[row["endpoint"]]
        seen[row["hash"]] = min(seen.get(row["hash"], row["seen_at"]), row["seen_at"])
    matched_total = 0
    for endpoint, seen in sorted(per_endpoint.items()):
        deltas = []
        for hash_value, seen_at in seen.items():
            if hash_value in bot:
                deltas.append((bot[hash_value] - seen_at) * 1000.0)
        matched_total = max(matched_total, len(deltas))
        if not deltas:
            print(f"{endpoint}: {len(seen)} signers' pendings, none matched bot rows yet")
            continue
        print(
            f"{endpoint}: {len(seen)} signers' pendings, {len(deltas)} matched bot rows"
            f" | delta bot-minus-public ms:"
            f" median {statistics.median(deltas):.0f}"
            f" p10 {percentile(deltas, 0.1):.0f}"
            f" p90 {percentile(deltas, 0.9):.0f}"
            f" | public earlier: {sum(1 for d in deltas if d > 0)}/{len(deltas)}"
        )
    if matched_total == 0:
        print("No overlapping hashes yet — let both sides run through a few targets.")


if __name__ == "__main__":
    main()
