# Monte Carlo test of the entry strategies against a simulated pool. Offline: no RPC, no key, nothing sent.
#
#   uv run python research/2026-11-04-pool-patterns/simulate.py [RUNS] [SWAPS_FILE]
#
# The pool is the real one: exact Uniswap V3 swap maths over the real tick layout
# (liquidity_profile.json, read by scan_liquidity.py; v3pool.py reproduces all 89 recorded swaps to
# the last digit). A scripted "big bot" swaps in random directions and random sizes (24 to 41 fETH,
# the observed range) at random gaps (the observed ones); `p_alt` is the chance that it flips side
# each time (0.93 observed). A few small random swaps are added. Our bot runs the REAL
# strategy.decide every 30 s (including the pattern guard), trades `qty` f564 per buy, and its own
# trades move the pool, so price impact and fees are included. Every number is f564 per session.
#
# Sessions start with a 3 hour warm-up in which the bot only watches (as after a restart with the
# price history preloaded), so the guard has data; "cold start" scenarios skip it.
import json
import random
import statistics
import sys
import time
from collections import Counter

sys.path.insert(0, ".")
sys.path.insert(0, "research/2026-11-04-pool-patterns")
import strategy as S
from v3pool import V3Pool

D = "research/2026-11-04-pool-patterns/"
TICK, COOLDOWN, HOURS, P0, WARM = 30, 300, 8, 1050.0, 3 * 3600
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 400
PATH = sys.argv[2] if len(sys.argv) > 2 else D + "swaps_8h.json"
PROFILE = D + "liquidity_profile.json"
# A trader with finite inventory does not push the price out of this band (above ~1500 only the
# 2.7% full-range liquidity is left, so one swap would send the price to nonsense): such a swap
# goes the other way instead. The real price stayed within 997 to 1103.
BAND = (750.0, 1400.0)

rows = json.load(open(PATH))
BOT = Counter(r["recip"] for r in rows).most_common(1)[0][0]
big = [r for r in rows if r["recip"] == BOT]
GAPS = [b["ts"] - a["ts"] for a, b in zip(big, big[1:])]
SIZE_LOW, SIZE_HIGH = min(r["base"] for r in big), max(r["base"] for r in big)
P_ALT_OBSERVED = sum(a["side"] != b["side"] for a, b in zip(big, big[1:])) / (len(big) - 1)


# ------------------------------------ a random session
def make_events(rng, p_alt, warm=WARM, stop_random=False, p_sell=None, comp_prob=0.0,
                paired=True, break_to=None, bleed=0.0):
    """(time, side, size, is_big, rival_trades_after), sorted by time; times start at 0 = warm-up start.

    break_to: after a random moment 1 to 6 h into trading the bot turns "trend" (sells 65% of the
    time) or "random" (no pattern). bleed: its sells are this much bigger than its buys.
    """
    end = warm + HOURS * 3600
    stop_at = warm + rng.uniform(3600, 7 * 3600) if stop_random else None
    break_at = warm + rng.uniform(3600, 6 * 3600) if break_to else None
    events, t = [], rng.uniform(0, 600)
    side = rng.choice(["buy", "sell"])
    size = rng.uniform(SIZE_LOW, SIZE_HIGH)
    while t < end:
        if stop_at is None or t < stop_at:
            broken = break_at is not None and t >= break_at
            if broken and break_to == "trend":
                side = "sell" if rng.random() < 0.65 else "buy"
            elif broken and break_to == "random":
                side = rng.choice(["buy", "sell"])
            elif p_sell is not None:
                side = "sell" if rng.random() < p_sell else "buy"
            elif rng.random() < p_alt:
                side = "buy" if side == "sell" else "sell"
            # paired: a flip undoes about what the last swap did, like a bot keeping its inventory
            size = (
                min(SIZE_HIGH, max(SIZE_LOW, size * rng.uniform(0.9, 1.1)))
                if paired
                else rng.uniform(SIZE_LOW, SIZE_HIGH)
            )
            shown = size * (1 + bleed) if side == "sell" else size
            events.append((t, side, shown, True, rng.random() < comp_prob))
        t += rng.choice(GAPS)
    for _ in range(rng.randint(5, 14)):  # the small actor
        events.append((rng.uniform(0, end), rng.choice(["buy", "sell"]), rng.uniform(0.3, 3), False, False))
    return sorted(events)


def swap(pool, side, size):
    probe = pool.copy()
    try:
        probe.buy_base(size) if side == "buy" else probe.sell_base(size)
        inside = BAND[0] <= probe.price() <= BAND[1]
    except ValueError:
        inside = False
    if not inside:
        side = "sell" if side == "buy" else "buy"
    try:
        pool.buy_base(size) if side == "buy" else pool.sell_base(size)
    except ValueError:
        pass  # even the other way is impossible: the swap fails
    return side


def run_session(cfg, events, qty, depth_scale=1.0, warm=WARM, comp_size=20.0):
    pool = V3Pool(PROFILE, P0, depth_scale=depth_scale)
    prices, held, entry, last = [P0] * 40, 0.0, None, -(10**9)
    closed, ei, t = [], 0, TICK
    low = high = P0
    end = warm + HOURS * 3600
    while t <= end:
        while ei < len(events) and events[ei][0] <= t:
            _, side, size, _big, rival = events[ei]
            side = swap(pool, side, size)
            if rival:  # a rival trades the reversal before we can
                swap(pool, "sell" if side == "buy" else "buy", comp_size)
            ei += 1
        p = pool.price()
        prices.append(p)
        del prices[: -S.HISTORY]
        if t >= warm:
            low, high = min(low, p), max(high, p)
            decision = S.decide(prices, S.Position(held, 1.0, entry), cfg)
            if decision == "buy" and t - last >= COOLDOWN:
                held, entry, last = pool.buy_with_quote(qty), p, t
            elif decision == "sell":
                closed.append(pool.sell_base(held) - qty)
                held, entry, last = 0.0, None, t
        t += TICK
    open_mark = pool.copy().sell_base(held) - qty if held else 0.0
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for pnl in closed:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(pnl=sum(closed) + open_mark, trades=len(closed), wins=sum(c > 0 for c in closed),
                drawdown=drawdown, stuck=held > 0, low=low, high=high)


# ------------------------------------ report
def quantile(values, q):
    return sorted(values)[int(q * (len(values) - 1))]


def report(label, results):
    pnl = [r["pnl"] for r in results]
    trades = sum(r["trades"] for r in results)
    wins = sum(r["wins"] for r in results)
    print(
        f"{label:<30} mean {statistics.mean(pnl):+8.1f}  median {statistics.median(pnl):+8.1f}  "
        f"p5 {quantile(pnl, .05):+8.1f}  p95 {quantile(pnl, .95):+8.1f}  worst {min(pnl):+8.1f}  "
        f"profit {100 * sum(p > 0 for p in pnl) / len(pnl):3.0f}%  trades/run {trades / len(pnl):4.1f}  "
        f"win {100 * wins / max(trades, 1):3.0f}%  maxDD {statistics.mean(r['drawdown'] for r in results):6.1f}  "
        f"stuck {100 * sum(r['stuck'] for r in results) / len(results):3.0f}%",
        flush=True,
    )
    return statistics.mean(pnl)


def variants(flip=None, drift=None):
    # the same entry rule with the pattern guard off and on (the default, or the given numbers)
    on = S.Config(strategy="sharp_drop")
    if flip is not None:
        on = S.Config(strategy="sharp_drop", guard_flip_pct=flip, guard_drift_pct=drift)
    return {
        "sharp_drop, guard OFF": S.Config(strategy="sharp_drop", guard_flip_pct=0, guard_drift_pct=0),
        f"sharp_drop, guard ON {on.guard_flip_pct:g}/{on.guard_drift_pct:g}": on,
    }


def scenario(name, strategies, qty=1000.0, depth_scale=1.0, warm=WARM, **kw):
    print(f"\n--- {name}", flush=True)
    sessions = [make_events(random.Random(1000 + i), warm=warm, **kw) for i in range(RUNS)]
    return {label: report(label, [run_session(cfg, ev, qty, depth_scale, warm) for ev in sessions])
            for label, cfg in strategies.items()}


if __name__ == "__main__":
    started = time.time()
    print(f"exact V3 pool from the real tick layout; the big bot flips side with p={P_ALT_OBSERVED:.2f} (observed), "
          f"sizes {SIZE_LOW:.0f}-{SIZE_HIGH:.0f} fETH, gaps {min(GAPS)}-{max(GAPS)} s (median {statistics.median(GAPS):.0f}); "
          f"{RUNS} sessions of {HOURS} h per line after a {WARM // 3600} h warm-up; 1000 f564 per buy unless stated")
    check = [run_session(S.Config(strategy="sharp_drop", take_profit_pct=100, guard_flip_pct=0, guard_drift_pct=0),
                         make_events(random.Random(i), P_ALT_OBSERVED), 1e-6) for i in range(RUNS)]
    print(f"model check: simulated {HOURS} h price range {statistics.median(r['low'] for r in check):.0f}-"
          f"{statistics.median(r['high'] for r in check):.0f} (median of runs); real: "
          f"{min(r['price'] for r in rows):.0f}-{max(r['price'] for r in rows):.0f}")
    V = variants()
    scenario("A. pattern as observed (flip 93%)", V, p_alt=P_ALT_OBSERVED)
    scenario("A-cold. same, but a cold start (no warm-up history)", V, warm=0, p_alt=P_ALT_OBSERVED)
    scenario("B. no pattern: random direction", V, p_alt=0.5)
    scenario("C70. weaker pattern, flip 70%", V, p_alt=0.7)
    scenario("D. downtrend from the start (65% sells)", V, p_alt=0.5, p_sell=0.65)
    scenario("BREAK-trend. pattern, then a downtrend at a random time", V, p_alt=P_ALT_OBSERVED, break_to="trend")
    scenario("BREAK-random. pattern, then no pattern", V, p_alt=P_ALT_OBSERVED, break_to="random")
    scenario("BLEED. it still alternates but its sells are 10% bigger", V, p_alt=P_ALT_OBSERVED, bleed=0.10)
    scenario("E. the bot stops at a random time", V, p_alt=P_ALT_OBSERVED, stop_random=True)
    scenario("G. a rival trades the reversal first (50%)", V, p_alt=P_ALT_OBSERVED, comp_prob=0.5)
    scenario("H. we trade 10,000 f564 per buy", V, qty=10_000.0, p_alt=P_ALT_OBSERVED)
    print(f"\n({time.time() - started:.0f} s)")
