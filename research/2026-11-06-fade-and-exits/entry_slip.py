import csv, json, bisect, statistics as st, datetime as dt

def T(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")

rows = [(int(a), float(b)) for a, b in csv.reader(open("logs/prices.csv"))]
ts = [r[0] for r in rows]
px = [r[1] for r in rows]
led = json.load(open("logs/ledger.json"))["closed"]

# first print of each price change = the pool price right after a swap
ch = [rows[0]]
for t, p in rows[1:]:
    if p != ch[-1][1]:
        ch.append((t, p))
cts = [c[0] for c in ch]

out = []
for c in led:
    o = c["opened"]
    fill_in = float(c["quote_spent"]) / float(c["base"])
    # last big down-step (<= -2%) before the buy
    i = bisect.bisect_right(cts, o) - 1
    j = i
    while j > 0 and not (ch[j][1] / ch[j - 1][1] - 1) * 100 <= -2.0:
        j -= 1
        if o - cts[j] > 1800:
            j = None
            break
    if j is None or j <= 0:
        continue
    trough = ch[j][1]
    lag = o - cts[j]
    # the next big up-step after the buy
    k = bisect.bisect_right(cts, c["closed"]) - 1
    m = k
    while m > 0 and cts[m] > o and not (ch[m][1] / ch[m - 1][1] - 1) * 100 >= 2.0:
        m -= 1
    peak = ch[m][1] if cts[m] > o else None
    fill_out = float(c["quote_received"]) / float(c["base"])
    out.append(dict(o=o, lag=lag, trough=trough, fill_in=fill_in, peak=peak, fill_out=fill_out, reason=c["reason"],
                    entry=c["entry_price"], size=float(c["quote_spent"]), pnl=float(c["quote_received"]) - float(c["quote_spent"])))
print("trades matched", len(out), "of", len(led))
slip = [(x["fill_in"] / x["trough"] - 1) * 100 for x in out]
print("buy fill vs trough print: median %+.2f%%  mean %+.2f%%  (the cost of being late + fee + impact)" % (st.median(slip), st.mean(slip)))
print("lag from the down-step to our buy (s): quantiles", [round(x) for x in st.quantiles([x["lag"] for x in out], n=4)])
for lo, hi in ((0, 30), (30, 60), (60, 120), (120, 400), (400, 2000)):
    s = [(x["fill_in"] / x["trough"] - 1) * 100 for x in out if lo <= x["lag"] < hi]
    if s:
        print(f" lag {lo:>4}-{hi:<4}s n={len(s):2d} fill vs trough median {st.median(s):+.2f}%")
pk = [x for x in out if x["peak"] and x["reason"] == "take_profit"]
if pk:
    s = [(x["fill_out"] / x["peak"] - 1) * 100 for x in pk]
    print("TP sells: fill vs peak print median %+.2f%%" % st.median(s))
# entry price relative to the trough as function of 'entry' column (pool price read)
e = [(x["entry"] / x["trough"] - 1) * 100 for x in out]
print("pool price read at decision vs trough: median %+.2f%%, max %+.2f%%" % (st.median(e), max(e)))
print("\nlast 20 live trades (pool read vs trough, lag, size, reason, pnl)")
for x in out[-20:]:
    print(T(x["o"]), f'trough {x["trough"]:.0f} read {x["entry"]:.0f} fill {x["fill_in"]:.0f} lag {x["lag"]:.0f}s size {x["size"]:.0f}', x["reason"], f'{x["pnl"]:+.1f}')
