"""Paper-only buy-before-buy / sell-after-target estimates. No inventory or sender."""

import copy

from backrun import optimize, validate_limits
from event_market import UNIT, apply_target


def evaluate(pools, swap, quote, buy_fee, sell_fee, max_base):
    if swap["side"] != "buy":
        raise ValueError("front-running sells requires existing base inventory")
    state = copy.deepcopy(pools)
    base = state[buy_fee].buy_with_quote(quote)
    if base > max_base:
        raise ValueError("base cap exceeded")
    after, target_output = apply_target(state, swap)
    output = after[sell_fee].sell_base(base)
    # Counterfactual: same entry and immediate exit, without the target transaction.
    no_target_output = state[sell_fee].sell_base(base)
    return {
        "strategy": "frontrun",
        "buy_fee": buy_fee,
        "sell_fee": sell_fee,
        "input_f564": quote,
        "base_feth": base,
        "output_f564": output,
        "profit_f564": output - quote,
        "profit_without_target_f564": no_target_output - quote,
        "target_contribution_f564": output - no_target_output,
        "predicted_target_output": target_output,
        "paper_only": True,
        "gas_included": False,
        "exact_validation_required": True,
        "assumed_order": ["our_buy", "target_buy", "our_sell"],
        "inventory_risk": "target may be replaced, revert, or execute in a different order",
    }


def prepare(pools, swap, limits):
    validate_limits(limits)
    if swap["side"] != "buy":
        return None
    best = None
    for buy_fee in pools:
        for sell_fee in pools:
            row = optimize(
                lambda size, buy_fee=buy_fee, sell_fee=sell_fee: evaluate(
                    pools, swap, size, buy_fee, sell_fee, limits["max_base_raw"] / UNIT
                ),
                limits["max_quote_raw"] / UNIT,
            )
            if (
                row
                and row["profit_f564"] >= limits["min_profit_raw"] / UNIT
                and row["target_contribution_f564"] > 0
                and (best is None or row["profit_f564"] > best["profit_f564"])
            ):
                best = row
    return best
