import sys,json,bisect,statistics as st;sys.path.insert(0,'research/2026-11-05-strategy-sizing-backtest')
import bt
ts,px,reset=bt.load_grid()
T=json.load(open('logs/ledger.json'))['closed']
entries=[(t['opened'],t['entry_price']) for t in T]
SIZE=1000.0
def sim(o,e,tp,sl,tsm,be=None,stepx=None,maxh=None):
    i=bisect.bisect_left(ts,o); base=bt.buy_fill(e,SIZE)
    peak=e
    for k in range(i,len(ts)):
        p=px[k]; chg=(p/e-1)*100; held=ts[k]-o
        peak=max(peak,p)
        why=None
        if chg>=tp: why="tp"
        elif chg<=-sl: why="sl"
        elif tsm and held>=tsm*60: why="ts"
        elif be and (peak/e-1)*100>=be[0] and chg<=be[1]: why="be"
        elif stepx and k>i and (p/px[k-1]-1)*100<=-stepx and chg<0.0: why="step"
        if why:
            q=bt.sell_fill(p,base); return (q/SIZE-1)*100,why,held/60
    q=bt.sell_fill(px[-1],base); return (q/SIZE-1)*100,"open",(ts[-1]-o)/60
def report(label,**kw):
    r=[sim(o,e,**kw) for o,e in entries]
    rets=[x[0] for x in r]; w=[x for x in rets if x>0]
    from collections import Counter
    c=Counter(x[1] for x in r)
    # equity growth if each trade put 100% of equity (compounded), a view of what all-in does
    eq=1.0
    for x in rets: eq*=1+x/100
    # worst drawdown on compounded
    pk=1;dd=0;e2=1
    for x in rets:
        e2*=1+x/100;pk=max(pk,e2);dd=max(dd,(pk-e2)/pk*100)
    print(f"{label:34s} win {len(w)/len(rets)*100:3.0f}% avg {st.mean(rets):+5.2f}% sum {sum(rets):+6.1f}% worst {min(rets):+5.1f}%  compounded x{eq:5.2f} maxDD {dd:4.1f}%  exits {dict(c)}  medhold {st.median([x[2] for x in r]):4.1f}m")
print("live entries (44), alternative exits, 1000 f564 per leg through exact V3 maths")
report("current TP3 SL5 TS60",tp=3,sl=5,tsm=60)
for tp in (2,2.5,3,3.5,4):
    for sl in (3,4,5,7):
        report(f"TP{tp} SL{sl} TS60",tp=tp,sl=sl,tsm=60)
print()
for tsm in (10,15,20,30,45,60,90,None):
    report(f"TP3 SL5 TS{tsm}",tp=3,sl=5,tsm=tsm)
print()
for tp,sl,tsm in ((2.5,4,30),(2.5,5,30),(3,4,30),(3,5,20),(2.5,4,20)):
    report(f"TP{tp} SL{sl} TS{tsm}",tp=tp,sl=sl,tsm=tsm)
print()
report("TP3 SL5 TS30 + breakeven(+1.5->+0.3)",tp=3,sl=5,tsm=30,be=(1.5,0.3))
report("TP3 SL5 TS30 + exit on -2% step if red",tp=3,sl=5,tsm=30,stepx=2.0)
report("TP3 SL5 TS30 + exit on -3% step if red",tp=3,sl=5,tsm=30,stepx=3.0)
