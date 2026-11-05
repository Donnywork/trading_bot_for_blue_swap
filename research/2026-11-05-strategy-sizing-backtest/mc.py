import sys,random,statistics as st;sys.path.insert(0,'research/2026-11-05-strategy-sizing-backtest')
import bt,exits_lib as ex
g=ex.gross_outcomes()
wins=[x for x in g if x>0];loss=[x for x in g if x<=0]
print(f"{len(wins)} gross winners (mean {st.mean(wins):+.2f}%), {len(loss)} gross losers (mean {st.mean(loss):+.2f}%, min {min(loss):.2f})")
# round-trip multiplier by size, from the exact V3 maths: buy at 1040, sell after the gross move
sizes=list(range(0,40001,500))
def rt(s,gm):
    if s<=0: return 1.0
    b=bt.buy_fill(1040.0,s); q=bt.sell_fill(1040.0*(1+gm/100),b); return q/s/(1+gm/100)
tabW=[rt(s,3.5) for s in sizes]; tabL=[rt(s,-4.5) for s in sizes]
def mult(s,gm):
    tab=tabW if gm>0 else tabL
    k=min(int(s//500),len(sizes)-2);f=(s-sizes[k])/500
    return tab[k]*(1-f)+tab[k+1]*f
print("round-trip cost vs size (win leg): "+", ".join(f"{s}:{(1-tabW[s//500])*100:.2f}%" for s in (500,1000,3000,5000,9000,15000,30000)))
def sizer(kind,arg=None):
    def f(eq,peak,hist):
        if kind=="all": return eq
        if kind=="frac": return eq*arg
        if kind=="risk": return eq*arg/100/0.0575
        if kind=="cap": return min(eq,arg)                 # fixed ceiling in f564
        if kind=="dd":                                     # all-in, but 25% size while >10% under the peak
            return eq*(0.25 if eq<0.9*peak else 1.0)
        if kind=="dd50":                                   # 50% of equity, 25% while >10% under the peak
            return eq*(0.5 if eq>=0.9*peak else 0.15)
    return f
schemes=[("all-in (now)",sizer("all")),("75% of equity",sizer("frac",.75)),("50% of equity",sizer("frac",.5)),("25% of equity",sizer("frac",.25)),
 ("risk 2% per trade",sizer("risk",2)),("risk 3% per trade",sizer("risk",3)),("ceiling 3000 f564",sizer("cap",3000)),("ceiling 5000 f564",sizer("cap",5000)),
 ("all-in + de-risk after 10% drawdown",sizer("dd")),("50% + de-risk after 10% drawdown",sizer("dd50"))]
worlds=[("as observed (85% win)",0.85,1.0),("degraded (70% win)",0.70,1.0),("pattern broken (50% win)",0.50,1.0),("downtrend (30% win, losses x1.5)",0.30,1.5)]
N=3000;TR=60;E0=5400.0
random.seed(7)
print(f"\n{TR} trades per path, {N} paths, start {E0:.0f} f564. median final x / 5th pct x / P(lose >20%) / mean maxDD% / P(lose >50%)")
print(f"{'':36s}"+"".join(f"{w[0]:>34s}" for w in worlds))
res={}
for name,fn in schemes:
    row=[]
    for wn,pw,lm in worlds:
        random.seed(11)
        fin=[];dds=[]
        for _ in range(N):
            eq=E0;peak=E0;dd=0
            for _ in range(TR):
                s=min(fn(eq,peak,None),eq)
                gm=random.choice(wins) if random.random()<pw else random.choice(loss)*lm
                eq+=s*(mult(s,gm)*(1+gm/100)-1)
                peak=max(peak,eq);dd=max(dd,(peak-eq)/peak*100)
            fin.append(eq/E0);dds.append(dd)
        fin.sort()
        row.append(f"{fin[N//2]:5.2f}x {fin[N//20]:5.2f}x  {sum(f<0.8 for f in fin)/N*100:4.0f}% {st.mean(dds):5.1f}% {sum(f<0.5 for f in fin)/N*100:4.0f}%")
    print(f"{name:36s}"+"".join(f"{r:>34s}" for r in row))
