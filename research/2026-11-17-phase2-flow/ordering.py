"""Offline same-block ordering patterns; associations are not proof of intent."""

import json
from collections import Counter, defaultdict

from analyze import TARGET, load, save


def main():
    swaps = load("swaps.json")
    grouped = defaultdict(list)
    for row in swaps:
        grouped[(row["block"], row["from"])].append(row)
    paired = []
    for (block, signer), rows in grouped.items():
        if len({r["pool"] for r in rows}) < 2:
            continue
        quote = sum(r["f564"] * (-1 if r["side"] == "buy" else 1) for r in rows)
        base = sum(r["feth"] * (1 if r["side"] == "buy" else -1) for r in rows)
        paired.append(
            {
                "block": block,
                "signer": signer,
                "transactions": [r["hash"] for r in rows],
                "f564_pool_flow": quote,
                "feth_pool_flow": base,
                "near_base_neutral": abs(base) < 1e-12,
                "gas_eth": sum(r["gas_eth"] for r in rows),
            }
        )
    brackets = []
    for i, target in enumerate(swaps):
        if target["from"] != TARGET:
            continue
        before = next(
            (r for r in reversed(swaps[:i]) if r["pool"] == target["pool"]), None
        )
        after = next((r for r in swaps[i + 1 :] if r["pool"] == target["pool"]), None)
        if (
            before
            and after
            and before["from"] == after["from"] != TARGET
            and before["block"] == target["block"] == after["block"]
            and before["side"] == target["side"] != after["side"]
        ):
            brackets.append(
                {
                    "block": target["block"],
                    "signer": before["from"],
                    "before_hash": before["hash"],
                    "target_hash": target["hash"],
                    "after_hash": after["hash"],
                }
            )
    save("same_block_cross_pool.json", paired)
    save("same_block_brackets.json", brackets)
    summary = {
        "same_block_cross_pool_groups": len(paired),
        "by_signer": dict(Counter(r["signer"] for r in paired)),
        "near_base_neutral_groups": sum(r["near_base_neutral"] for r in paired),
        "positive_base_neutral_groups": sum(
            r["near_base_neutral"] and r["f564_pool_flow"] > 0 for r in paired
        ),
        "same_pool_same_block_target_brackets": len(brackets),
        "bracket_signers": dict(Counter(r["signer"] for r in brackets)),
        "example_cross_pool_groups": paired[-3:],
        "example_brackets": brackets[:3],
    }
    save("ordering_summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
