# Backtest engine: real recorded prices (logs/prices.csv) on a 10 s grid, fills through the exact
# Uniswap V3 maths of the pool's real liquidity layout (so price impact grows with trade size).
import csv, sys, statistics as st, json
from dataclasses import dataclass, field
sys.path.insert(0, ".")
# last, so it is searched first: the repo root has a v3pool.py of its own now (a different class,
# built from a profile dict); this study's V3Pool is built from the profile file
sys.path.insert(0, "research/2026-11-04-pool-patterns")
from v3pool import V3Pool
import strategy

PROFILE = "research/2026-11-04-pool-patterns/liquidity_profile.json"
GRID = 10
GAP = 600

def load_grid():
    raw = [(int(a), float(b)) for a, b in csv.reader(open("logs/prices.csv"))]
    ts, px = [], []
    t = raw[0][0]; j = 0; last = raw[0][1]
    while t <= raw[-1][0]:
        while j < len(raw) and raw[j][0] <= t:
            last = raw[j][1]; j += 1
        ts.append(t); px.append(last); t += GRID
    # mark samples after a data gap (> GAP s) so history resets there, like the bot would
    reset = [False] * len(ts); prev = raw[0][0]
    gaps = [(raw[i-1][0], raw[i][0]) for i in range(1, len(raw)) if raw[i][0] - raw[i-1][0] > GAP]
    for a, b in gaps:
        k = (b - ts[0]) // GRID
        if 0 <= k < len(ts): reset[k] = True
    return ts, px, reset

def buy_fill(price, quote):
    p = V3Pool(PROFILE, price); return p.buy_with_quote(quote)          # base received
def sell_fill(price, base):
    p = V3Pool(PROFILE, price); return p.sell_base(base)                # quote received

@dataclass
class Plan:
    name: str
    entry: callable                      # (hist) -> bool
    tp: float = 3.0
    sl: float = 5.0
    time_stop_min: float | None = 60.0
    size: callable = None                # (ctx) -> quote amount for the whole planned position
    tranches: tuple = ((0.0, 1.0),)      # (extra drop % from avg entry, share of planned size)
    guard: tuple = (60.0, 5.0)           # (flip, drift); (0,0) = off
    cooldown: int = 300
    hist_len: int = 600
    delay: int = 0                       # seconds between signal and fill (price moves meanwhile)
    exit_extra: callable = None          # (hist, pos, price, held) -> reason or None

def run(plan, ts, px, reset, lo=0, hi=None, equity0=5400.0):
    hi = hi or len(ts)
    cfg = strategy.Config(guard_flip_pct=plan.guard[0], guard_drift_pct=plan.guard[1])
    cash, base = equity0, 0.0
    pos = None   # dict(tr=[(base, quote, price)], opened, n_done, planned)
    h0 = lo; last_trade = None
    trades = []; eq_curve = []; peak = equity0; maxdd = 0.0; losses_run = 0
    def mark(i):
        return cash + base * px[i] * 0.997
    for i in range(lo, hi):
        if reset[i]: h0 = i
        t, p = ts[i], px[i]
        j = min(hi - 1, i + plan.delay // GRID)            # fill price after the delay
        fp, ft = px[j], ts[j]
        if pos is not None:
            spent = sum(q for _, q, _ in pos["tr"]); bs = sum(b for b, _, _ in pos["tr"])
            avg = sum(pr * q for _, q, pr in pos["tr"]) / spent
            held = t - pos["opened"]
            chg = (p / avg - 1) * 100
            reason = None
            if chg >= plan.tp: reason = "take_profit"
            elif chg <= -plan.sl: reason = "stop_loss"
            elif plan.time_stop_min and held >= plan.time_stop_min * 60: reason = "time_stop"
            elif plan.exit_extra:
                reason = plan.exit_extra(px[max(h0, i - plan.hist_len + 1): i + 1], pos, p, held)
            if reason:
                got = sell_fill(fp, base)
                cash += got; base = 0.0
                pnl = got - spent
                trades.append(dict(open=pos["opened"], close=ft, avg=avg, exit=fp, pnl=pnl, ret=pnl/spent*100,
                                   spent=spent, reason=reason, hold=ft - pos["opened"], tr=len(pos["tr"])))
                last_trade = t; pos = None
                eq = mark(i); 
            else:
                # scale-in tranche?
                k = pos["n_done"]
                if k < len(plan.tranches):
                    drop, share = plan.tranches[k]
                    if chg <= -drop:
                        q = min(cash, pos["planned"] * share)
                        if q > 1:
                            b = buy_fill(fp, q); cash -= q; base += b
                            pos["tr"].append((b, q, fp)); pos["n_done"] += 1
        elif i - h0 >= 1 and not (last_trade is not None and t - last_trade < plan.cooldown):
            hist = px[max(h0, i - plan.hist_len + 1): i + 1]
            if plan.entry(hist) and not (sum(plan.guard) and strategy.guard_status(hist, cfg)["reason"]):
                ctx = dict(equity=cash, cash=cash, trades=trades, hist=hist)
                planned = min(cash, plan.size(ctx))
                q = planned * plan.tranches[0][1]
                if q > 1:
                    b = buy_fill(fp, q); cash -= q; base += b
                    pos = dict(tr=[(b, q, fp)], opened=ft, n_done=1, planned=planned)
                    last_trade = t
        eq = mark(i); eq_curve.append(eq)
        peak = max(peak, eq); maxdd = max(maxdd, (peak - eq) / peak * 100)
    final = mark(hi - 1)
    wins = [x for x in trades if x["pnl"] > 0]
    rets = [x["ret"] for x in trades]
    gp = sum(x["pnl"] for x in wins); gl = -sum(x["pnl"] for x in trades if x["pnl"] <= 0)
    return dict(name=plan.name, final=final, ret=(final / equity0 - 1) * 100, maxdd=maxdd, n=len(trades),
                win=len(wins) / len(trades) * 100 if trades else 0, worst=min(rets) if rets else 0,
                avg=st.mean(rets) if rets else 0, pf=gp / gl if gl else float("inf"),
                tp=sum(x["reason"] == "take_profit" for x in trades), sl=sum(x["reason"] == "stop_loss" for x in trades),
                ts=sum(x["reason"] == "time_stop" for x in trades), holding=pos is not None, trades=trades,
                med_hold=st.median([x["hold"] for x in trades]) / 60 if trades else 0)

# ---------- entry rules ----------
def bollinger(window=15, dev=2.0, min_dip=1.0):
    def f(h):
        if len(h) < window: return False
        r = h[-window:]; m = sum(r) / window
        return h[-1] < m - dev * st.pstdev(r) and (m - h[-1]) / m * 100 >= min_dip
    return f
def sharp_drop(lookback=12, drop=3.0):
    def f(h):
        if len(h) < lookback + 1: return False
        return h[-1] <= max(h[-(lookback + 1):]) * (1 - drop / 100)
    return f
def mean_rev(window=20, thr=2.0):
    def f(h):
        if len(h) < window: return False
        m = sum(h[-window:]) / window
        return (h[-1] - m) / m * 100 <= -thr
    return f
def level_filter(inner, window=360, below=1.0):
    # also require price at least `below`% under the long average (a swing LOW, not a wobble off a high)
    def f(h):
        if len(h) < window: return False
        m = sum(h[-window:]) / window
        return inner(h) and h[-1] <= m * (1 - below / 100)
    return f
def range_pos(inner, window=360, pct=35):
    # require price in the lowest `pct`% of the recent min..max range
    def f(h):
        if len(h) < window: return False
        r = h[-window:]; lo, hi = min(r), max(r)
        return inner(h) and hi > lo and (h[-1] - lo) / (hi - lo) * 100 <= pct
    return f
def fixed(q): return lambda ctx: q
def frac(f): return lambda ctx: ctx["equity"] * f
