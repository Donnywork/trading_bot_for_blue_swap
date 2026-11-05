# Entry-rule comparison in simulated worlds (the research's generator + exact V3 pool), pluggable entries.
import sys,random,statistics as st,time
sys.argv=[sys.argv[0]]+sys.argv[1:]
sys.path.insert(0,"."); sys.path.insert(0,"research/2026-11-05-strategy-sizing-backtest"); sys.path.insert(0,"research/2026-11-04-pool-patterns")
import simulate as SIM
import strategy as S
import bt
from v3pool import V3Pool
RUNS=int(sys.argv[1]) if len(sys.argv)>1 else 120
TICK,COOLDOWN,WARM,HOURS,P0=SIM.TICK,SIM.COOLDOWN,SIM.WARM,SIM.HOURS,SIM.P0
def last_two_steps_ok(h):
    # block a buy when the last two big steps (>=2%) were both DOWN (a sell streak, not a one-off dip)
    s=S.big_steps(h)
    return not(len(s)>=2 and s[-1]==-1 and s[-2]==-1)
def alternating3(h):
    s=S.big_steps(h)
    return len(s)>=3 and s[-1]==-1 and s[-2]==1 and s[-3]==-1
ENT={
 "A bollinger15/2 (now)":bt.bollinger(15,2),
 "B +range: lowest 50% of 360":bt.range_pos(bt.bollinger(15,2),360,50),
 "C +below 1% of 360-avg":bt.level_filter(bt.bollinger(15,2),360,1.0),
 "D +block after 2 sells in a row":lambda h:bt.bollinger(15,2)(h) and last_two_steps_ok(h),
 "E +range50 & no 2-sell streak":lambda h:bt.range_pos(bt.bollinger(15,2),360,50)(h) and last_two_steps_ok(h),
 "F sharp_drop12/3":bt.sharp_drop(12,3),
}
def session(entry,events,guard,tp=3.0,sl=5.0,tsm=60,qty=1000.0):
    cfg=S.Config(guard_flip_pct=guard[0],guard_drift_pct=guard[1])
    pool=V3Pool(SIM.PROFILE,P0); prices=[P0]*40; held=0.0; ent=None; opened=None; last=-10**9
    closed=[];ei=0;t=TICK;end=WARM+HOURS*3600
    while t<=end:
        while ei<len(events) and events[ei][0]<=t:
            _,side,size,_b,rival=events[ei]; side=SIM.swap(pool,side,size)
            if rival: SIM.swap(pool,"sell" if side=="buy" else "buy",20.0)
            ei+=1
        p=pool.price(); prices.append(p); del prices[:-600]
        if t>=WARM:
            if ent is None:
                if t-last>=COOLDOWN and entry(prices) and not(sum(guard) and S.guard_status(prices,cfg)["reason"]):
                    held,ent,opened,last=pool.buy_with_quote(qty),p,t,t
            else:
                chg=(p/ent-1)*100
                if chg>=tp or chg<=-sl or t-opened>=tsm*60:
                    closed.append(pool.sell_base(held)-qty); held=0.0;ent=None;last=t
        t+=TICK
    mark=pool.copy().sell_base(held)-qty if held else 0.0
    eq=pk=dd=0.0
    for c in closed:
        eq+=c;pk=max(pk,eq);dd=max(dd,pk-eq)
    return dict(pnl=sum(closed)+mark,n=len(closed),w=sum(c>0 for c in closed),dd=dd,worst=min(closed,default=0))
worlds=[("observed pattern",dict(p_alt=SIM.P_ALT_OBSERVED)),("weaker 70%",dict(p_alt=0.7)),("no pattern",dict(p_alt=0.5)),
        ("downtrend 65% sells",dict(p_alt=0.5,p_sell=0.65)),("pattern->downtrend",dict(p_alt=SIM.P_ALT_OBSERVED,break_to="trend")),
        ("pattern->random",dict(p_alt=SIM.P_ALT_OBSERVED,break_to="random"))]
if __name__=="__main__":
    t0=time.time()
    for guard in ((60,5),(0,0)):
        print(f"\n######## guard {'60/5' if guard[0] else 'OFF'}  ({RUNS} sessions x 8h per cell, 1000 f564/buy; mean PnL | p5 | win% | mean maxDD | trades/run)")
        print(f"{'':32s}"+"".join(f"{w[0]:>24s}" for w in worlds))
        sess=[[SIM.make_events(random.Random(1000+i),**kw) for i in range(RUNS)] for _,kw in worlds]
        for name,e in ENT.items():
            cells=[]
            for ws in sess:
                r=[session(e,ev,guard) for ev in ws]
                pn=sorted(x["pnl"] for x in r);n=sum(x["n"] for x in r);w=sum(x["w"] for x in r)
                cells.append(f"{st.mean(pn):+6.0f}|{pn[len(pn)//20]:+5.0f}|{100*w/max(n,1):3.0f}|{st.mean(x['dd'] for x in r):4.0f}|{n/len(r):4.1f}")
            print(f"{name:32s}"+"".join(f"{c:>24s}" for c in cells),flush=True)
    print(f"({time.time()-t0:.0f}s)")
