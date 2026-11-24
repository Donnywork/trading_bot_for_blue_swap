"""Reproduce the spot-move estimates from a saved monitor snapshot, offline."""

import argparse
import json
import math
from pathlib import Path


def input_to(market, pct):
    # token0 is f564. In quote-per-base units the price falls as the tick rises.
    sqrt_now = math.sqrt(market["spot"])
    target = sqrt_now * math.sqrt(1 + pct / 100)
    tick = market["tick"]
    active = market["liquidity"]
    net = {int(t): n for t, n in market["liquidity_net_by_tick"].items()}
    selling_base = pct < 0
    edges = (
        sorted(t for t in net if t > tick)
        if selling_base
        else sorted((t for t in net if t <= tick), reverse=True)
    )
    total = 0
    for edge in edges:
        if active <= 0:
            break
        price = 1.0001 ** (-edge / 2)
        end = max(price, target) if selling_base else min(price, target)
        total += active * (1 / end - 1 / sqrt_now if selling_base else end - sqrt_now)
        if end == target:
            return total / 10**18
        sqrt_now = end
        active += net[edge] if selling_base else -net[edge]
    raise ValueError("target outside contiguous recorded liquidity")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "snapshot",
        type=Path,
        nargs="?",
        default=Path(__file__).with_name("monitor-snapshot.json"),
    )
    market = json.loads(parser.parse_args().snapshot.read_text())["market"]
    print(
        f"Block {market['block']}, spot {market['spot']:.4f} f564/fETH, amounts before fee"
    )
    for pct in (-0.5, 0.5, -5, 5, -25, 33):
        print(
            f"{pct:+5g}%  {input_to(market, pct):>12,.2f} {'fETH' if pct < 0 else 'f564'}"
        )
