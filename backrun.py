"""Pure paper estimates for f564-funded atomic cross-pool backruns."""

import copy
import math

from event_market import UNIT, apply_target


def validate_limits(limits):
    if set(limits) != {"max_quote_raw", "max_base_raw", "min_profit_raw"}:
        raise ValueError("expected quote cap, base cap and f564 profit floor")
    if any(type(v) is not int or v <= 0 for v in limits.values()):
        raise ValueError("limits must be positive raw integers")
    return limits


def cycle(pools, buy_fee, quote, max_base):
    buy = copy.deepcopy(pools[buy_fee])
    sell_fee = next(fee for fee in pools if fee != buy_fee)
    sell = copy.deepcopy(pools[sell_fee])
    base = buy.buy_with_quote(quote)
    if base > max_base:
        raise ValueError("base cap exceeded")
    output = sell.sell_base(base)
    if not math.isfinite(output) or base <= 0:
        raise ValueError("invalid cycle output")
    return {
        "buy_fee": buy_fee,
        "sell_fee": sell_fee,
        "input_f564": quote,
        "base_feth": base,
        "output_f564": output,
        "profit_f564": output - quote,
    }


def optimize(evaluate, ceiling):
    # Cross-pool marginal output decreases with input, but lumpy liquidity (discrete
    # positions at discrete ticks) can leave narrow profitable bands that a plain
    # ternary search walks past. Sweep a coarse grid first, remember the best feasible
    # point seen anywhere, then refine ternary inside the bracket around it. A rejected
    # cap or target minimum creates an infeasible upper tail. Keep zero as a valid
    # no-trade choice; infeasible sizes score -inf.
    best = None
    best_size = None

    def score(size):
        nonlocal best, best_size
        try:
            row = evaluate(size)
        except (ValueError, ZeroDivisionError, OverflowError):
            return -math.inf
        gain = row["profit_f564"]
        if (
            math.isfinite(gain)
            and gain > 0
            and (best is None or gain > best["profit_f564"])
        ):
            best, best_size = row, size
        return gain

    lo, hi = 0.0, ceiling
    for n in range(1, 33):
        score(ceiling * n / 32)
    score(ceiling)
    if best_size is not None:
        # Refine inside the grid bracket around the winner; the bracket is one grid
        # step wide, so a band that caught a grid point cannot hide from refinement.
        step = ceiling / 32
        lo, hi = max(0.0, best_size - step), min(ceiling, best_size + step)
    for _ in range(64):
        x, y = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if score(x) < score(y):
            lo = x
        else:
            hi = y
    score(hi)
    score(ceiling)
    return best


def best_cycle(pools, limits):
    validate_limits(limits)
    best = None
    for fee in pools:
        row = optimize(
            lambda size, fee=fee: cycle(
                pools, fee, size, limits["max_base_raw"] / UNIT
            ),
            limits["max_quote_raw"] / UNIT,
        )
        if (
            row
            and row["profit_f564"] >= limits["min_profit_raw"] / UNIT
            and (best is None or row["profit_f564"] > best["profit_f564"])
        ):
            best = row
    if best:
        best.update(
            {
                "strategy": "backrun",
                "paper_only": True,
                "exact_validation_required": True,
                "gas_included": False,
            }
        )
    return best


def prepare(pools, swap, limits):
    after, output = apply_target(pools, swap)
    plan = best_cycle(after, limits)
    if plan:
        plan["predicted_target_output"] = output
    return plan
