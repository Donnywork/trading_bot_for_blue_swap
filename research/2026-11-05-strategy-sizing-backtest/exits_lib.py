import sys,json,bisect;sys.path.insert(0,'research/2026-11-05-strategy-sizing-backtest')
import bt
ts,px,reset=bt.load_grid()
T=json.load(open('logs/ledger.json'))['closed']
entries=[(t['opened'],t['entry_price']) for t in T]
def gross_outcomes(tp=3,sl=5,tsm=60):
    out=[]
    for o,e in entries:
        i=bisect.bisect_left(ts,o)
        for k in range(i,len(ts)):
            p=px[k];chg=(p/e-1)*100;held=ts[k]-o
            if chg>=tp or chg<=-sl or held>=tsm*60:
                out.append(chg);break
    return out
