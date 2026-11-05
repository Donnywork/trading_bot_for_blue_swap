import itertools
import statistics
from collections import deque
from dataclasses import replace

import strategy

# ------------------------------------
# What every entry strategy would have done on the recorded prices, with the settings in force now
# (take profit, stop loss, time stop, pattern guard, cooldown) and each strategy's own saved
# numbers. Pure: prices in, numbers out. It reads no chain and sends nothing, so the dashboard can
# show it without an RPC call.
#
# It is a paper replay: fills happen at the recorded price, minus the costs of each leg (the
# pool's 0.3% fee plus an allowance for our own price impact, see strategy.POOL_FEE_PCT). Gas is
# paid in Sepolia ETH and not counted. It is judged on the same prices the settings were tuned
# on, so it ranks ideas; it does not forecast.
# ------------------------------------

MIN_STEPS = (
    6  # fewer big price steps than this and "the pool's typical jump" is not known
)
# Two samples further apart than this belong to different stretches of the market: the bot, when it
# restarts, only keeps its price history if it is younger than this (main.PRELOAD_MAX_AGE), so the
# replay forgets its history across such a gap too, and a jump across one is not a "step".
GAP_SECONDS = 600


def percentile(values, share):
    # nearest rank on a list that need not be sorted (None for an empty one)
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def replay(points, cfg, start_ts, cooldown, size):
    # points: [(unix time, price)], oldest first. Prices before `start_ts` only warm the history
    # up (as the bot's own history is warm); trades start from `start_ts`.
    fee, impact = strategy.POOL_FEE_PCT / 100, strategy.IMPACT_PCT / 100
    cost = fee + impact  # per leg
    prices = deque(maxlen=strategy.HISTORY)
    entry = opened = last_trade = None
    trades, holds_won, holds = [], [], []
    equity = gross = fees = impacts = peak = worst = 0.0
    size = float(size)
    before = None

    def mark(value):
        nonlocal peak, worst
        peak = max(peak, value)
        worst = max(worst, peak - value)

    for ts, price in points:
        if before is not None and ts - before > GAP_SECONDS:
            prices.clear()  # the bot would have started again from nothing
        before = ts
        prices.append(price)
        if ts < start_ts:
            continue
        held = None if entry is None else ts - opened
        if entry is None and last_trade is not None and ts - last_trade < cooldown:
            continue  # a buy is not allowed yet: nothing to decide
        position = strategy.Position(
            base=0.0, quote=1.0, entry_price=entry, held_seconds=held
        )
        # an open position is only judged on the latest price: no need to copy the history
        decision = strategy.decide(
            [price] if entry is not None else list(prices), position, cfg
        )
        if decision == "buy" and (last_trade is None or ts - last_trade >= cooldown):
            entry, opened, last_trade = price, ts, ts
        elif decision == "sell":
            reason = strategy.exit_reason(price, entry, cfg, held)
            ratio = price / entry
            net = size * (ratio * (1 - cost) ** 2 - 1)
            moved = size * (ratio - 1)
            paid = (
                moved - net
            )  # what both legs cost, split between the fee and the impact
            equity += net
            gross += moved
            fees += paid * fee / cost
            impacts += paid * impact / cost
            holds.append(held)
            if net > 0:
                holds_won.append(held)
            trades.append(
                {
                    "opened": opened,
                    "closed": ts,
                    "entry_price": entry,
                    "exit_price": price,
                    "reason": reason,
                    "return_pct": net / size * 100,
                }
            )
            entry = opened = None
            last_trade = ts
            mark(equity)

    open_now = 0.0
    if entry is not None:
        # still holding: valued at the last price, with the exit's cost taken off
        open_now = size * (points[-1][1] / entry * (1 - cost) ** 2 - 1)
    mark(equity + open_now)
    return {
        "trades": len(trades),
        "wins": len(holds_won),
        "gross": gross,  # what the price moves alone would have made
        "fees": fees,  # paid to the pool (positive number)
        "impact": impacts,  # paid in price impact (positive number)
        "profit": equity,  # gross - fees - impact, on the closed trades
        "open": open_now,
        "holding": entry is not None,
        "worst_drawdown": worst,
        "win_hold_p90": percentile(holds_won, 0.9),
        "median_hold": statistics.median(holds) if holds else None,
        "reasons": {
            name: sum(1 for t in trades if t["reason"] == name)
            for name in ("take_profit", "stop_loss", "time_stop")
        },
    }


def step_stats(points):
    # How the price moves: the size of the big jumps (strategy.GUARD_STEP_PCT or more between two
    # neighbouring samples) in the recorded prices. None when there are too few to say anything.
    up, down = [], []
    for (ta, a), (tb, b) in itertools.pairwise(points):
        if tb - ta > GAP_SECONDS:
            continue  # a jump across a stretch with no samples is not one step
        change = (b / a - 1) * 100
        if abs(change) >= strategy.GUARD_STEP_PCT:
            (up if change > 0 else down).append(abs(change))
    if len(up) + len(down) < MIN_STEPS:
        return None
    jumps = up + down
    return {
        "count": len(jumps),
        "median": statistics.median(jumps),
        "up_median": statistics.median(up) if up else None,
        "up_p90": percentile(up, 0.9),
        "down_median": statistics.median(down) if down else None,
        "p90": percentile(jumps, 0.9),
    }


def advice(cfg, result, stats):
    # ------------------------------------
    # Warnings for one strategy's settings. `result` is that strategy's replay under `cfg`;
    # `stats` is step_stats() (or None). Each is {"level": "warn" | "info", "text"}. Plain rules
    # of arithmetic and of what the recorded prices did: they say where the settings fight the
    # costs or the pool, not that a different number would win.
    # ------------------------------------

    out = []

    def add(level, text):
        out.append({"level": level, "text": text})

    friction = strategy.FRICTION_PCT
    tp, sl = cfg.take_profit_pct, cfg.stop_loss_pct
    if (
        tp <= friction
    ):  # (the form's lowest take profit is above this; the maths below needs it)
        add(
            "warn",
            f"A take profit of {tp:g}% does not even cover the costs ({friction:g}%).",
        )
        return out
    net_win, net_loss = tp - friction, sl + friction
    breakeven = net_loss / (net_win + net_loss) * 100
    text = (
        f"After the pool fee ({strategy.POOL_FEE_PCT:g}% each way) and about "
        f"{2 * strategy.IMPACT_PCT:g}% price impact, a take profit nets about "
        f"+{net_win:.2f}% and a stop loss about -{net_loss:.2f}%: it has to win "
        f"{breakeven:.0f}% of its trades to break even."
    )
    if result["trades"] >= 5:
        won = result["wins"] / result["trades"] * 100
        text += f" The replay won {won:.0f}%."
        add("warn" if won < breakeven + 5 else "info", text)
    else:
        add("info", text)
    if tp < 2 * friction:
        add(
            "warn",
            f"A take profit of {tp:g}% is close to the costs: only {net_win:.2f}% is left.",
        )
    if net_loss / net_win > 3:
        add(
            "warn",
            f"One stop loss (-{net_loss:.2f}%) wipes out {net_loss / net_win:.1f} take "
            "profits: a wide stop needs a very high win rate.",
        )
    if stats:
        if sl < stats["median"] * 0.9:
            add(
                "warn",
                f"The price moves in jumps of about {stats['median']:.1f}%, so a {sl:g}% stop "
                "loss is crossed by a single adverse jump and fills after it, below the line.",
            )
        if stats["up_p90"] and tp > stats["up_p90"] * 1.1:
            add(
                "warn",
                f"A take profit of {tp:g}% is above 9 in 10 of the pool's up-jumps "
                f"({stats['up_p90']:.1f}%): it would rarely be reached.",
            )
    if cfg.time_stop_on:
        hold = result["win_hold_p90"]
        if hold is not None and cfg.time_stop_min * 60 < hold:
            add(
                "warn",
                f"The time stop ({cfg.time_stop_min:g} min) is shorter than 9 in 10 winning "
                f"trades took in the replay ({hold / 60:.0f} min): it cuts winners early.",
            )
        if result["reasons"]["time_stop"]:
            add(
                "info",
                f"{result['reasons']['time_stop']} trade(s) ended by the time stop in the replay.",
            )
    if cfg.guard_flip_pct == 0 and cfg.guard_drift_pct == 0:
        add(
            "warn",
            "The pattern guard is off: nothing pauses buying if the pool's pattern breaks "
            "(in the research's simulations it cut the worst drawdown by 56 to 72% when the "
            "pattern broke, and cost 8 to 60% of the profit while it held).",
        )
    elif cfg.guard_flip_pct >= 85:
        add(
            "warn",
            f"The guard needs {cfg.guard_flip_pct:g}% of the big steps to alternate, which in "
            "practice means at most one break in 7 pairs: it skips many good entries.",
        )
    values = cfg.values()
    name = cfg.strategy
    if name == "mean_reversion" and values["threshold_pct"] < 2 * friction:
        add(
            "warn",
            f"A dip of {values['threshold_pct']:g}% is within twice the costs "
            f"({2 * friction:g}%): most of it is noise.",
        )
    if name in ("mean_reversion", "bollinger") and values["window"] < 10:
        add(
            "warn",
            f"A window of {values['window']:g} samples is short: signals are noisy.",
        )
    if name == "rsi" and values["oversold"] >= 40:
        add(
            "warn",
            "An RSI limit of 40 or more fires on mild falls, not on a real drop.",
        )
    if name == "sharp_drop" and stats:
        drop = values["drop_pct"]
        if drop < stats["median"] * 0.8:
            add(
                "warn",
                f"A drop of {drop:g}% is smaller than the pool's typical jump "
                f"({stats['median']:.1f}%): it also buys on noise.",
            )
        if stats["p90"] and drop > stats["p90"]:
            add(
                "warn",
                f"A drop of {drop:g}% is bigger than 9 in 10 jumps ({stats['p90']:.1f}%): "
                "it will rarely buy.",
            )
    if name in ("breakout", "ma_cross"):
        add(
            "info",
            "Trend following: it buys after the price has risen. In a pool that swings back and "
            "forth, that is often the top.",
        )
    if result["trades"] == 0:
        add("info", "No trade in this window with these settings.")
    elif result["profit"] + result["open"] < 0:
        total = result["profit"] + result["open"]
        add(
            "warn",
            f"In the replay these settings lose {-total:.2f} over the window "
            f"({result['trades']} trades).",
        )
    return out


def compare(points, cfg, start_ts, cooldown, size, stats=None):
    # one row per strategy, in the order the strategies are declared, with its warnings
    rows = []
    for name, strat in strategy.STRATEGIES.items():
        own = replace(cfg, strategy=name)
        result = replay(points, own, start_ts, cooldown, size)
        rows.append(
            {
                "strategy": name,
                "label": strat.label,
                **result,
                "warnings": advice(own, result, stats),
            }
        )
    return rows
