import csv, json, bisect, statistics as st, datetime as dt, collections

def T(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")

rows = [(int(a), float(b)) for a, b in csv.reader(open("logs/prices.csv"))]
ts = [r[0] for r in rows]
px = [r[1] for r in rows]
led = json.load(open("logs/ledger.json"))["closed"]
# own trade times: ledger opened/closed are the receipt-time stamps; allow 40s before for the tx to be mined
own = sorted([c["opened"] for c in led] + [c["closed"] for c in led])

def own_in(a, b):
    i = bisect.bisect_left(own, a - 40)
    return i < len(own) and own[i] <= b + 5

ev = []
last_t, last_p = rows[0]
for t, p in rows[1:]:
    if p != last_p:
        ev.append(dict(t=t, a=last_p, b=p, pct=(p / last_p - 1) * 100))
        last_p = p
for e in ev:
    e["own"] = own_in(e["t"] - 1, e["t"] + 1)

def px_at(t):
    return px[bisect.bisect_right(ts, t) - 1]

big = [e for e in ev if abs(e["pct"]) >= 2.0 and not e["own"]]
print("big non-own steps >=2%:", len(big))
for name, sgn in (("UP", 1), ("DOWN", -1)):
    S = [e for e in big if e["pct"] * sgn > 0]
    print("\nafter", name, "step (no own trade in horizon; gaps excluded)")
    for H in (20, 40, 60, 120, 300, 600, 1200):
        vals = []
        for e in S:
            if e["t"] + H > ts[-1]:
                continue
            i = bisect.bisect_right(ts, e["t"] + H) - 1
            if ts[i] < e["t"] + H - 60:
                continue
            if own_in(e["t"] + 2, e["t"] + H):
                continue
            vals.append((px_at(e["t"] + H) / e["b"] - 1) * 100)
        if vals:
            print(f"  +{H:>4}s n={len(vals):3d} median {st.median(vals):+.2f}%  mean {st.mean(vals):+.2f}%  P(>0) {sum(v>0 for v in vals)/len(vals):.2f}")

# small steps (0.3-2%) non-own: when do they occur relative to the last big step?
small = [e for e in ev if 0.3 <= abs(e["pct"]) < 2.0 and not e["own"]]
print("\nsmall non-own steps:", len(small))
bt_ = [e["t"] for e in big]
lag = []
for e in small:
    i = bisect.bisect_right(bt_, e["t"]) - 1
    if i >= 0:
        lag.append((e["t"] - bt_[i], e["pct"], big[i]["pct"]))
print("lag-from-last-big-step buckets (s) / sign agrees with the big step?")
b = collections.Counter()
for l, p, bp in lag:
    k = "<30" if l < 30 else "<60" if l < 60 else "<120" if l < 120 else "<300" if l < 300 else ">=300"
    b[(k, "same" if p * bp > 0 else "opposite")] += 1
for k in ("<30", "<60", "<120", "<300", ">=300"):
    print(k, "same", b[(k, "same")], "opposite", b[(k, "opposite")])

# second-of-minute and minute parity of big steps (swap time is earlier than poll time by up to ~10s)
print("minute parity (even/odd):", collections.Counter((e["t"] // 60) % 2 for e in big))
print("minute mod 4:", sorted(collections.Counter((e["t"] // 60) % 4 for e in big).items()))
print("seconds 5s buckets:", sorted(collections.Counter(e["t"] % 60 // 5 * 5 for e in big).items()))
# last day sample with exact times
for e in big[-30:]:
    print(T(e["t"]), f'{e["a"]:.1f}->{e["b"]:.1f} {e["pct"]:+.2f}%')
