"""Offline analysis of the recorded liquidity and comparison with the actual quoter results."""

import json
import math
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import v3pool  # noqa: E402

OUT = Path(__file__).parent


def main():
    saved = json.loads((OUT / "snapshot.json").read_text())
    profiles = {p["fee"]: p for p in saved["profiles"]}
    results = []
    for fee, p in profiles.items():
        active = next(r for r in p["intervals"] if r["active"])
        liquidity = p["liquidity"] / 1e18
        s = math.sqrt(p["price_f564"])
        results.append({
            "fee": fee, "active_lower": active["lower_f564"], "active_upper": active["upper_f564"],
            "active_to_backstop_liquidity_ratio": p["liquidity"] / p["intervals"][0]["liquidity"],
            "buy_f564_to_next_boundary": liquidity * (math.sqrt(active["upper_f564"]) - s) / (1 - fee / 1e6),
            "sell_feth_to_next_boundary": liquidity * (1 / math.sqrt(active["lower_f564"]) - 1 / s) / (1 - fee / 1e6),
        })
    errors = []
    for row in saved["cycles"]:
        if "error" in row:
            continue
        buy, sell = profiles[row["buy_fee"]], profiles[row["sell_fee"]]
        base = v3pool.V3Pool(buy, buy["price_f564"]).buy_with_quote(row["input_f564"])
        out = v3pool.V3Pool(sell, sell["price_f564"]).sell_base(base)
        observed = int(row["quote_raw"]) / 1e18
        errors.append(abs(out - observed))
        assert abs(out - observed) < 0.000001, (out, observed)
    narrow, wide = profiles[500]["price_f564"], profiles[10000]["price_f564"]
    analysis = {
        "block": saved["block"], "utc": saved["utc"],
        "spot_gap_pct": (wide / narrow - 1) * 100,
        "zero_impact_fee_hurdle_pct": str((1 / (Decimal("0.9995") * Decimal("0.99")) - 1) * 100),
        "liquidity_boundaries": results,
        "positive_sampled_cycles": sum(Decimal(r.get("profit_f564", "0")) > 0 for r in saved["cycles"]),
        "offline_model_max_abs_quote_error_f564": max(errors),
    }
    (OUT / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
    print(json.dumps(analysis, indent=2))


if __name__ == "__main__":
    main()
