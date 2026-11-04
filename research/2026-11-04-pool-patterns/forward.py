# live forward test, 1 cheap read (slot0) every 30s for N minutes; paper trades only, nothing is sent.
# P&L is in f564: quote -> base (buy) -> quote (sell), 0.3% pool fee each leg.
import bisect
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
import bot_contract
import config
import strategy as S
from main import redact


def _quiet_crash(exc_type, exc, tb):
    # an HTTP error message contains the full RPC URL (and its API key): print it redacted only
    print(redact(f"{exc_type.__name__}: {exc}"), file=sys.stderr)
    os._exit(1)


sys.excepthook = _quiet_crash


def guardless(**kw):
    # this live test was run before the pattern guard existed: keep it off so it reproduces
    return S.Config(guard_flip_pct=0, guard_drift_pct=0, **kw)


MINUTES = int(sys.argv[1]); TICK = 30; FEE = 0.003; COOLDOWN = 300; Q0 = 1.0
w3 = bot_contract.connect_read_only()
pool = w3.eth.contract(address=w3.to_checksum_address(os.environ["POOL_ADDRESS"]), abi=config.pool_abi)
_, d0 = config.read_token(w3, pool.functions.token0().call()); _, d1 = config.read_token(w3, pool.functions.token1().call())
def read_price():
    for i in range(6):
        try:
            sq = pool.functions.slot0().call()[0]; return (sq / 2**96) ** 2 * 10 ** (d0 - d1)
        except Exception as e:
            print("read failed, retry:", redact(str(e))[:120], flush=True); time.sleep(2 ** i)
    return None
old = json.load(open(sys.argv[2])); ts = [r["ts"] for r in old]; px = [r["price"] for r in old]
now = time.time(); hist = [px[max(bisect.bisect_right(ts, now - TICK * i) - 1, 0)] for i in range(40, 0, -1)]
cfgs = {
 "old default(mr w20 th2 tp5 sl3)": guardless(take_profit_pct=5, stop_loss_pct=3),
 "mr(w10 th2 tp3 sl4)": guardless(take_profit_pct=3, stop_loss_pct=4, params={"mean_reversion": {"window": 10, "threshold_pct": 2}}),
 "sharp(lb4 3% tp3 sl4)": guardless(strategy="sharp_drop", take_profit_pct=3, stop_loss_pct=4),
}
st = {n: dict(quote=Q0, base=0.0, entry=None, last=-10**9, trades=[]) for n in cfgs}
prices = list(hist); last_price = None; end = time.time() + MINUTES * 60; t0 = time.time()
print("start", time.strftime("%H:%M:%S"), flush=True)
while time.time() < end:
    p = read_price()
    if p is None: print("giving up this tick", flush=True); time.sleep(TICK); continue
    el = int(time.time() - t0)
    if last_price is not None and abs(p / last_price - 1) > 1e-6:
        print("[+%4ds] SWAP seen: price %.2f -> %.2f (%+.2f%%)" % (el, last_price, p, (p / last_price - 1) * 100), flush=True)
    last_price = p; prices.append(p); prices[:] = prices[-600:]
    for n, c in cfgs.items():
        s = st[n]
        pos = S.Position(s["base"], s["quote"], s["entry"])
        d = S.decide(prices, pos, c)
        if d == "buy" and time.time() - s["last"] >= COOLDOWN:
            s["base"] = s["quote"] / p * (1 - FEE); s["entry"] = p; s["quote"] = 0.0; s["last"] = time.time(); s["spent"] = Q0 if not s["trades"] else s.get("spent", Q0)
            s["cost"] = s["base"] * p / (1 - FEE)
            print("[+%4ds] %-28s BUY  at %.2f" % (el, n, p), flush=True)
        elif d == "sell":
            q = s["base"] * p * (1 - FEE); s["trades"].append(q / s["cost"] - 1); s["quote"] = q; s["base"] = 0.0; s["entry"] = None
            print("[+%4ds] %-28s SELL at %.2f  -> %+.4f f564 per 1 f564 spent (%+.2f%%)" % (el, n, p, q - s["cost"], s["trades"][-1] * 100), flush=True)
    time.sleep(TICK)
p = last_price
print("=== end %s, price %.2f ===" % (time.strftime("%H:%M:%S"), p), flush=True)
for n, s in st.items():
    open_val = s["base"] * p * (1 - FEE) if s["base"] else 0.0
    closed = sum(t for t in s["trades"])
    print("%-28s closed trades %d %s | open %s | f564 profit per 1 f564 per trade: %s" % (n, len(s["trades"]), ["%+.2f%%" % (t * 100) for t in s["trades"]], "yes (marked %+.2f%%)" % ((open_val / s["cost"] - 1) * 100) if s["base"] else "no", ["%+.4f" % (t) for t in s["trades"]] or "-"), flush=True)
