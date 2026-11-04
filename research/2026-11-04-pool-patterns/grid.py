# Take profit x stop loss grid for the Sharp drop rule, over several simulated pool behaviours.
#   uv run python research/2026-11-04-pool-patterns/grid.py [RUNS]
# Mean f564 profit per 8 h session at 1000 f564 per buy, guard off. Uses simulate.py's pool model.
import random
import statistics
import sys

sys.argv = [sys.argv[0], sys.argv[1] if len(sys.argv) > 1 else "600"]
sys.path.insert(0, "research/2026-11-04-pool-patterns")
import simulate as sim  # noqa: E402

TPS, SLS = (2, 3, 4, 5), (2, 3, 4, 6)
WORLDS = {
    "A2 pattern as observed": dict(p_alt=sim.P_ALT_OBSERVED, paired=True),
    "C70 weaker pattern (flip 70%)": dict(p_alt=0.7, paired=True),
    "B no pattern (flip 50%)": dict(p_alt=0.5, paired=True),
    "D downtrend (65% sells)": dict(p_alt=0.5, p_sell=0.65),
    "G rival takes the move (50%)": dict(p_alt=sim.P_ALT_OBSERVED, paired=True, comp_prob=0.5),
}
sessions = {
    name: [sim.make_events(random.Random(5000 + i), **kw) for i in range(sim.RUNS)]
    for name, kw in WORLDS.items()
}
for name, ev in sessions.items():
    print(f"\n{name}   (rows: take profit %, columns: stop loss %)")
    print("        " + "".join(f"SL{sl:<8}" for sl in SLS))
    for tp in TPS:
        cells = []
        for sl in SLS:
            cfg = sim.S.Config(strategy="sharp_drop", take_profit_pct=tp, stop_loss_pct=sl, guard_flip_pct=0, guard_drift_pct=0)
            cells.append(statistics.mean(sim.run_session(cfg, e, 1000.0)["pnl"] for e in ev))
        print(f"TP{tp:<5} " + "".join(f"{c:+9.0f} " for c in cells))
