import sys
sys.path.insert(0, "research/2026-11-05-strategy-sizing-backtest")
import bt

ts, px, reset = bt.load_grid()
n = len(ts)
mid = n // 2
SPLITS = {"all": (0, n), "first half": (0, mid), "second half": (mid, n)}


def up_step(pct=2.0, floor=0.5):
    def f(h, pos, price, held):
        if len(h) < 4:
            return None
        if h[-1] / min(h[-4:-1]) - 1 >= pct / 100 and price > pos["tr"][0][2] * (1 + floor / 100):
            return "up_step"
        return None
    return f


def run(entry, tp, sl, tsm, size, extra=None, delay=10, cooldown=300):
    plan = bt.Plan("x", entry, tp=tp, sl=sl, time_stop_min=tsm, size=bt.fixed(size), tranches=((0.0, 1.0),),
                   guard=(0.0, 0.0), cooldown=cooldown, delay=delay, exit_extra=extra)
    return {k: bt.run(plan, ts, px, reset, lo=lo, hi=hi, equity0=9700.0) for k, (lo, hi) in SPLITS.items()}


def show(label, res):
    cells = ""
    for k in SPLITS:
        r = res[k]
        cells += f"{r['ret']:+7.1f}% n={r['n']:3d} w={r['win']:3.0f}% worst {r['worst']:+5.1f}   "
    print(f"{label:46s}{cells}")


print("=== cooldown x delay (entry meanrev20/2, TP2.5 SL5 TS40, size 3200)")
for cd in (300, 120, 60, 30):
    for dl in (10, 20, 30):
        show(f"cooldown {cd:3d}s delay {dl:2d}s", run(bt.mean_rev(20, 2.0), 2.5, 5.0, 40, 3200, delay=dl, cooldown=cd))
print("=== TP sweep at cooldown 60, delay 20, size 3200")
for tp in (1.5, 2.0, 2.5, 3.0, 3.5, 4.0):
    show(f"TP {tp}", run(bt.mean_rev(20, 2.0), tp, 5.0, 40, 3200, delay=20, cooldown=60))
print("=== stop loss / time stop at TP2.5 cooldown 60 delay 20")
for sl, tsm in ((3, 40), (5, 40), (8, 40), (5, 20), (5, 15), (5, 30), (5, 60)):
    show(f"SL {sl} TS {tsm}", run(bt.mean_rev(20, 2.0), 2.5, sl, tsm, 3200, delay=20, cooldown=60))
print("=== entry rules at TP2.5 cooldown 60 delay 20 size 3200")
for name, ef in (("meanrev20/2", bt.mean_rev(20, 2.0)), ("meanrev20/2.5", bt.mean_rev(20, 2.5)), ("meanrev30/2.5", bt.mean_rev(30, 2.5)),
                 ("meanrev20/3", bt.mean_rev(20, 3.0)), ("meanrev60/2.5", bt.mean_rev(60, 2.5)),
                 ("sharp2/2.5", bt.sharp_drop(2, 2.5)), ("sharp12/3", bt.sharp_drop(12, 3.0)), ("boll15/2", bt.bollinger(15, 2.0))):
    show(name, run(ef, 2.5, 5.0, 40, 3200, delay=20, cooldown=60))
print("=== size at TP2.5 cooldown 60 delay 20 entry meanrev20/2")
for s in (1000, 3200, 5000, 7000, 9000):
    show(f"size {s}", run(bt.mean_rev(20, 2.0), 2.5, 5.0, 40, s, delay=20, cooldown=60))
    show(f"size {s} TP3", run(bt.mean_rev(20, 2.0), 3.0, 5.0, 40, s, delay=20, cooldown=60))
