import sys
sys.path.insert(0, "research/2026-11-05-strategy-sizing-backtest")
import bt

ts, px, reset = bt.load_grid()
n = len(ts)
# split by TIME (the 6 h outage makes an index split uneven)
mid = next(i for i, t in enumerate(ts) if t >= (ts[0] + ts[-1]) // 2)
SPLITS = {"all": (0, n), "1st half": (0, mid), "2nd half": (mid, n)}
print("covered hours: all %.1f, 1st %.1f, 2nd %.1f (grid samples * 10 s)" % (n / 360, mid / 360, (n - mid) / 360))


def run(entry, tp, sl, tsm, size, delay=10, cooldown=300):
    plan = bt.Plan("x", entry, tp=tp, sl=sl, time_stop_min=tsm, size=bt.fixed(size), tranches=((0.0, 1.0),),
                   guard=(0.0, 0.0), cooldown=cooldown, delay=delay)
    return {k: bt.run(plan, ts, px, reset, lo=lo, hi=hi, equity0=9700.0) for k, (lo, hi) in SPLITS.items()}


def show(label, res):
    cells = ""
    for k in SPLITS:
        r = res[k]
        cells += f"{r['ret']:+7.1f}% dd {r['maxdd']:4.1f}% n={r['n']:3d} w={r['win']:3.0f}%   "
    print(f"{label:44s}{cells}")


mr = bt.mean_rev(20, 2.0)
print("--- what the settings in force do now (TP4 SL5 TS40, cooldown 300, size 3200) at different fill delays")
for dl in (0, 10, 20, 30):
    show(f"now, delay {dl}s", run(mr, 4.0, 5.0, 40, 3200, dl, 300))
print("--- proposed (TP2.5 SL5 TS30, cooldown 60)")
for dl in (0, 10, 20, 30):
    show(f"proposed, delay {dl}s", run(mr, 2.5, 5.0, 30, 3200, dl, 60))
print("--- proposed at delay 20 with larger buys")
for s in (3200, 5000, 7000, 9000):
    show(f"proposed size {s}", run(mr, 2.5, 5.0, 30, s, 20, 60))
