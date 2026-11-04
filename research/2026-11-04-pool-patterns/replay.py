# offline replay of the saved swaps through the real strategy.decide (no network, no keys)
import bisect
import json
import sys

sys.path.insert(0, ".")
import strategy as S


def guardless(**kw):
    # these studies were made before the pattern guard existed: keep it off so they reproduce
    return S.Config(guard_flip_pct=0, guard_drift_pct=0, **kw)


rows = json.load(open(sys.argv[1]))
ts = [r["ts"] for r in rows]; px = [r["price"] for r in rows]
def price_at(t):
    i = bisect.bisect_right(ts, t) - 1
    return px[max(i, 0)]
FEE = 0.003
def run(cfg, tick=30, cooldown=300, lag=0, history_start=None):
    t0, t1 = ts[0], ts[-1]
    prices, pos, last_trade, out = [], None, -10**9, []
    t = t0
    while t <= t1:
        p = price_at(t); prices.append(p); prices[:] = prices[-S.HISTORY:]
        position = S.Position(0, 1.0, pos) if pos is None else S.Position(1.0, 0.0, pos)
        d = S.decide(prices, position, cfg)
        cooling = t - last_trade < cooldown
        if d == "buy" and not cooling:
            pos = price_at(t + lag); last_trade = t
        elif d == "sell":
            sell = price_at(t + lag)
            out.append(((sell / pos) * (1 - FEE) ** 2 - 1) * 100); pos = None
        t += tick
    return out, pos
print("%-34s %6s %6s %9s %9s"%("config","trades","wins","net %","open"))
for lag in (0, 30, 60):
    print("--- fill lag %ds ---"%lag)
    for name, cfg in [
        ("old default mr w20 th2 tp5 sl3", guardless(take_profit_pct=5, stop_loss_pct=3)),
        ("mr w20 th2 tp3 sl3", guardless(take_profit_pct=3, stop_loss_pct=3)),
        ("mr w20 th2 tp3 sl4", guardless(take_profit_pct=3, stop_loss_pct=4)),
        ("mr w20 th2 tp3.5 sl4", guardless(take_profit_pct=3.5, stop_loss_pct=4)),
        ("mr w10 th1.5 tp3 sl3", guardless(take_profit_pct=3, stop_loss_pct=3, params={"mean_reversion": {"window": 10, "threshold_pct": 1.5}})),
        ("mr w10 th2 tp3 sl4", guardless(take_profit_pct=3, stop_loss_pct=4, params={"mean_reversion": {"window": 10, "threshold_pct": 2}})),
        ("mr w5 th2 tp3 sl4", guardless(take_profit_pct=3, stop_loss_pct=4, params={"mean_reversion": {"window": 5, "threshold_pct": 2}})),
        ("rsi tp3 sl4", guardless(strategy="rsi", take_profit_pct=3, stop_loss_pct=4)),
        ("sharp_drop lb4 3% tp3 sl4", guardless(strategy="sharp_drop", take_profit_pct=3, stop_loss_pct=4)),
    ]:
        o, op = run(cfg, lag=lag)
        print("%-34s %6d %6d %+9.2f %9s"%(name, len(o), sum(x>0 for x in o), sum(o), "yes" if op else "no"))
