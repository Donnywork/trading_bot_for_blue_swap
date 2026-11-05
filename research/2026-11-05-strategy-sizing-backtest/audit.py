import sys,json,bisect;sys.path.insert(0,'research/2026-11-05-strategy-sizing-backtest')
import bt
ts,px,reset=bt.load_grid()
T=json.load(open('logs/ledger.json'))['closed']
def hist_at(t,n=1080):
    i=bisect.bisect_right(ts,t)-1
    return px[max(0,i-n+1):i+1]
rows=[]
for t in T:
    sp=float(t['quote_spent']);r=(float(t['quote_received'])/sp-1)*100
    rows.append((t['opened'],r,t['reason']))
def audit(name,f):
    bl=[r for o,r,_ in rows if not f(hist_at(o-1))]   # o-1: signal state just before fill
    keep=[r for o,r,_ in rows if f(hist_at(o-1))]
    L=[r for r in bl if r<=0];W=[r for r in bl if r>0]
    kl=[r for r in keep if r<=0]
    print(f"{name:42s} blocks {len(L)}/7 losers, {len(W)}/37 winners | kept: {len(keep)} trades, {len(kl)} losers, sum {sum(keep):+6.1f}% (all 44: {sum(r for _,r,_ in rows):+.1f}%) worst {min(keep):+.1f}%")
audit("current (no extra filter)",lambda h:True)
for w in (180,360,600,1080):
    for below in (0.5,1.0,1.5,2.0):
        audit(f"price <= {w}-sample avg -{below}%",lambda h,w=w,b=below: len(h)>=w and h[-1]<=sum(h[-w:])/w*(1-b/100))
for w in (360,600,1080):
    for pct in (30,40,50):
        audit(f"lowest {pct}% of {w}-range",lambda h,w=w,p=pct:len(h)>=w and max(h[-w:])>min(h[-w:]) and (h[-1]-min(h[-w:]))/(max(h[-w:])-min(h[-w:]))*100<=p)
# absolute level (cheap, but overfit risk)
for lvl in (1040,1050,1060):
    audit(f"absolute price <= {lvl}",lambda h,l=lvl:h[-1]<=l)
# last big drop recent: a >=3% single-step drop within the last N samples (swing entry, not wobble)
def recent_drop(h,n,pct):
    h=h[-n-1:]
    return any((b/a-1)*100<=-pct for a,b in zip(h,h[1:]))
for n in (6,12,30):
    for pct in (2.5,3.0):
        audit(f"a single -{pct}% step within last {n} samples",lambda h,n=n,p=pct:recent_drop(h,n,p))
