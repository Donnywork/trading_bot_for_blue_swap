"""Decode saved public receipts and quantify the missed 23:35 race, offline."""

import json
from decimal import Decimal
from pathlib import Path


def amounts(data):
    raw = bytes.fromhex(data.removeprefix("0x"))
    if len(raw) != 160:
        raise ValueError("Swap event must contain five ABI words")
    return tuple(int.from_bytes(raw[i : i + 32], "big", signed=True) for i in (0, 32))


def main():
    evidence = json.loads(Path(__file__).with_name("evidence.json").read_text())
    target, rival = evidence["target"], evidence["competitor"]
    buy_quote, buy_base = amounts(rival["buy"]["data"])
    sell_quote, sell_base = amounts(rival["sell"]["data"])
    if buy_quote <= 0 or sell_quote >= 0 or buy_base + sell_base != 0:
        raise ValueError("Receipt legs do not form a matching base round trip")
    unit = Decimal(10**18)
    before = int(evidence["buy_pool_sqrt_before_hex"], 16)
    after = int(evidence["buy_pool_sqrt_after_hex"], 16)
    print("Competitor input f564:", Decimal(buy_quote) / unit)
    print("Competitor output f564:", Decimal(-sell_quote) / unit)
    print("Matched fETH:", Decimal(sell_base) / unit)
    print("Competitor quote gain f564:", Decimal(-sell_quote - buy_quote) / unit)
    print("Raw pool price move %:", (Decimal(after**2) / Decimal(before**2) - 1) * 100)
    print("Our validation seconds:", target["rejection_at"] - target["validation_started_at"])
    print("Our broadcast:", target["broadcast_start_at"])
    print("Limitation:", evidence["limits"])


if __name__ == "__main__":
    main()
