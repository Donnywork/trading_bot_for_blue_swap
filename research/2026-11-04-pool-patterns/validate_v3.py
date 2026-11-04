# Does the exact V3 model reproduce the real swaps? Starts from the previous swap's price and applies
# the real input amount; compares the predicted price after the swap with the real one.
import json
import statistics
import sys

sys.path.insert(0, "research/2026-11-04-pool-patterns")
from v3pool import V3Pool

D = "research/2026-11-04-pool-patterns/"
rows = json.load(open(D + "swaps_8h.json")) + json.load(open(D + "swaps_next_24min.json"))
errs, outs = [], []
for prev, r in zip(rows, rows[1:]):
    pool = V3Pool(D + "liquidity_profile.json", prev["price"])
    if r["side"] == "buy":  # quote in (gross), base out
        got = pool.buy_with_quote(r["quote"])
        out_err = got / r["base"] - 1
    else:  # base in, quote out
        got = pool.sell_base(r["base"])
        out_err = got / (r["quote"] * 1.0) - 1
    errs.append((pool.price() / r["price"] - 1) * 100)
    outs.append(out_err * 100)
abs_e = [abs(e) for e in errs]
print(f"{len(errs)} swaps: price after the swap, model vs real: median error {statistics.median(abs_e):.3f}%, "
      f"p90 {sorted(abs_e)[int(.9 * len(abs_e))]:.3f}%, worst {max(abs_e):.3f}%")
print(f"amount received, model vs real: median {statistics.median(abs(o) for o in outs):.3f}%, worst {max(abs(o) for o in outs):.3f}%")
bad = [(i, round(e, 2)) for i, e in enumerate(errs) if abs(e) > 0.2]
print("swaps off by more than 0.2%:", bad[:10] or "none")
