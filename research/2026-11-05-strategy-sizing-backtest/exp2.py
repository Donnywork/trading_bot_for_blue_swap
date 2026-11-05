import sys;sys.path.insert(0,'research/2026-11-05-strategy-sizing-backtest')
import bt
ts,px,reset=bt.load_grid()
n=len(ts);mid=n//2
E0=5400.0
entry=bt.bollinger(15,2)
lvl=bt.range_pos(entry,360,50)

def kelly(ctx):
    tr=ctx["trades"][-20:]
    if len(tr)<8: return ctx["equity"]*0.25
    w=[t["ret"] for t in tr if t["ret"]>0]; l=[-t["ret"] for t in tr if t["ret"]<=0]
    p=len(w)/len(tr)
    if not l: return ctx["equity"]*0.5
    b=(sum(w)/len(w))/(sum(l)/len(l)); f=p-(1-p)/b
    return ctx["equity"]*max(0.05,min(0.5,f/2))      # half Kelly, 5..50%
def throttle(base_frac=1.0):
    def f(ctx):
        k=0
        for t in reversed(ctx["trades"]):
            if t["pnl"]<=0: k+=1
            else: break
        return ctx["equity"]*base_frac*(0.5**k)
    return f
def risk(r,sl=5.0):
    return lambda ctx: ctx["equity"]*r/100/((sl+0.75)/100)
def impact_cap(max_buy_slip=0.5):
    # biggest buy whose price impact (beyond the 0.3% fee) stays under max_buy_slip %
    def f(ctx):
        h=ctx["hist"][-1]; lo,hi=100.0,ctx["equity"]
        def slip(q):
            b=bt.buy_fill(h,q); eff=q/b; return (eff/h-1)*100-0.3
        if slip(hi)<=max_buy_slip: return hi
        for _ in range(30):
            m=(lo+hi)/2
            if slip(m)<=max_buy_slip: lo=m
            else: hi=m
        return lo
    return f
def runall(label,entry_fn,sizes,guard=(60,5),tr=((0.0,1.0),),tp=3,sl=5,ts_=60,delay=10):
    print(f"\n### {label}")
    print(f"{'size rule':30s} {'final':>8s} {'ret%':>7s} {'maxDD%':>7s} {'n':>3s} {'win%':>5s} {'worstTrade%':>11s} {'worst$':>8s} | H1 ret/dd | H2 ret/dd")
    for name,sz,tranches in sizes:
        plan=bt.Plan(name,entry_fn,size=sz,tp=tp,sl=sl,time_stop_min=ts_,guard=guard,delay=delay,tranches=tranches)
        r=bt.run(plan,ts,px,reset,equity0=E0)
        a=bt.run(plan,ts,px,reset,hi=mid,equity0=E0); b=bt.run(plan,ts,px,reset,lo=mid,equity0=E0)
        w=min((t["pnl"] for t in r["trades"]),default=0)
        print(f"{name:30s} {r['final']:8.0f} {r['ret']:7.1f} {r['maxdd']:7.1f} {r['n']:3d} {r['win']:5.0f} {r['worst']:11.2f} {w:8.0f} | {a['ret']:5.1f}/{a['maxdd']:4.1f} | {b['ret']:5.1f}/{b['maxdd']:4.1f}")
sizes=[("all-in (now)",lambda c:1e12,((0.0,1.0),)),
 ("fixed 1000",bt.fixed(1000),((0.0,1.0),)),("fixed 3000",bt.fixed(3000),((0.0,1.0),)),
 ("fixed 5000",bt.fixed(5000),((0.0,1.0),)),
 ("25% of equity",bt.frac(0.25),((0.0,1.0),)),("50% of equity",bt.frac(0.5),((0.0,1.0),)),("75% of equity",bt.frac(0.75),((0.0,1.0),)),
 ("risk 1%/trade",risk(1),((0.0,1.0),)),("risk 2%/trade",risk(2),((0.0,1.0),)),("risk 3%/trade",risk(3),((0.0,1.0),)),
 ("impact cap 0.25%",impact_cap(0.25),((0.0,1.0),)),("impact cap 0.5%",impact_cap(0.5),((0.0,1.0),)),("impact cap 1.0%",impact_cap(1.0),((0.0,1.0),)),
 ("half-Kelly (last 20)",kelly,((0.0,1.0),)),
 ("all-in, halve after each loss",throttle(1.0),((0.0,1.0),)),
 ("50% eq, halve after each loss",throttle(0.5),((0.0,1.0),)),
 ("scale-in 50%+50% @-3%",lambda c:1e12,((0.0,0.5),(3.0,0.5))),
 ("50% eq scale-in 50/50 @-3%",bt.frac(0.5),((0.0,0.5),(3.0,0.5))),
]
runall("baseline entry bollinger15/2 + guard60/5, TP3 SL5 TS60",entry,sizes)
runall("same, entry + 'lowest 50% of last 360 samples' filter",lvl,[s for s in sizes if s[0] in("all-in (now)","50% of equity","risk 2%/trade","impact cap 0.5%","half-Kelly (last 20)","all-in, halve after each loss","scale-in 50%+50% @-3%")])
