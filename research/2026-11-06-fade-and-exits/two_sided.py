import sys
sys.path.insert(0, "research/2026-11-05-strategy-sizing-backtest")
import bt

ts, px, reset = bt.load_grid()
n = len(ts)
mid = next(i for i, t in enumerate(ts) if t >= (ts[0] + ts[-1]) // 2)
SPLITS = {"all": (0, n), "1st half": (0, mid), "2nd half": (mid, n)}
V0 = 9700.0


def rotation(lo, hi, start_base_share, delay=10, step=2.0, cooldown=60, frac_buy=1.0, frac_sell=1.0,
             time_stop_min=None, stop_pct=None):
    p0 = px[lo]
    cash = V0 * (1 - start_base_share)
    base = V0 * start_base_share / p0 * 0.997  # base bought at the start price, outside the pool (deposit)
    base = V0 * start_base_share / p0
    last = -10**9
    buy_t = None
    buy_px = None
    trades = 0
    h0 = lo
    for i in range(lo, hi):
        if reset[i]:
            h0 = i
        if i - h0 < 3 or ts[i] - last < cooldown:
            continue
        j = min(hi - 1, i + delay // bt.GRID)
        fp = px[j]
        prior = px[i - 3:i]
        dn = px[i] <= max(prior) * (1 - step / 100)
        up = px[i] >= min(prior) * (1 + step / 100)
        forced = False
        if base > 0 and buy_t is not None:
            if time_stop_min and ts[i] - buy_t >= time_stop_min * 60:
                forced = True
            if stop_pct and px[i] <= buy_px * (1 - stop_pct / 100):
                forced = True
        if dn and cash > 1:
            q = cash * frac_buy
            try:
                b = bt.buy_fill(fp, q)
            except ValueError:
                continue
            cash -= q
            base += b
            last = ts[i]
            buy_t, buy_px = ts[i], px[i]
            trades += 1
        elif (up or forced) and base > 0:
            s = base * frac_sell
            try:
                got = bt.sell_fill(fp, s)
            except ValueError:
                continue
            base -= s
            cash += got
            last = ts[i]
            trades += 1
    # the "liquidate" button: everything back to quote at the last price
    end = cash + (bt.sell_fill(px[hi - 1], base) if base > 0 else 0.0)
    return (end / V0 - 1) * 100, trades, (base * px[hi - 1] / (cash + base * px[hi - 1]) * 100)


def hold(lo, hi, share):
    p0 = px[lo]
    base = V0 * share / p0
    cash = V0 * (1 - share)
    return ((cash + (bt.sell_fill(px[hi - 1], base) if base > 0 else 0)) / V0 - 1) * 100


def long_only(delay, size, tp=2.5, tsm=30):
    plan = bt.Plan("lo", bt.mean_rev(20, 2.0), tp=tp, sl=5.0, time_stop_min=tsm, size=bt.fixed(size),
                   guard=(0.0, 0.0), cooldown=60, delay=delay)
    return {k: bt.run(plan, ts, px, reset, lo=lo, hi=hi, equity0=V0) for k, (lo, hi) in SPLITS.items()}


def line(label, vals):
    print(f"{label:58s}" + "".join(f"{v:>24s}" for v in vals))


print(f"start value {V0:.0f} f564 (base valued at the first price); final = everything sold back to f564 (the liquidate button)")
line("", list(SPLITS))
for dl in (10, 20):
    print(f"--- fill delay {dl}s")
    r = long_only(dl, 9000)
    line("long-only (proposed settings), all-in 9000, from quote", [f"{r[k]['ret']:+.1f}% n={r[k]['n']}" for k in SPLITS])
    for share, name in ((0.0, "all quote"), (0.5, "50% base"), (1.0, "all base")):
        vals = []
        for k, (lo, hi) in SPLITS.items():
            ret, t, endshare = rotation(lo, hi, share, delay=dl)
            vals.append(f"{ret:+.1f}% n={t}")
        line(f"two-sided rotation 100%/100%, start {name}", vals)
    for share, name in ((0.0, "all quote"), (0.5, "50% base")):
        vals = []
        for k, (lo, hi) in SPLITS.items():
            ret, t, _ = rotation(lo, hi, share, delay=dl, time_stop_min=30)
            vals.append(f"{ret:+.1f}% n={t}")
        line(f"two-sided rotation + 30 min time stop, start {name}", vals)
    vals = [f"{hold(lo, hi, 0.5):+.1f}%" for k, (lo, hi) in SPLITS.items()]
    line("baseline: just hold 50% base, no trading", vals)
    vals = [f"{hold(lo, hi, 1.0):+.1f}%" for k, (lo, hi) in SPLITS.items()]
    line("baseline: just hold 100% base, no trading", vals)
print("--- upper bound: two-sided rotation, fill at the signal price (delay 0)")
for share, name in ((0.0, "all quote"), (0.5, "50% base")):
    vals = []
    for k, (lo, hi) in SPLITS.items():
        ret, t, _ = rotation(lo, hi, share, delay=0)
        vals.append(f"{ret:+.1f}% n={t}")
    line(f"two-sided rotation, start {name}", vals)
