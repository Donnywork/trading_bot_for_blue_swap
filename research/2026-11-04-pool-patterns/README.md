# Pool pattern study, 2026-11-04

What the fETH/f564 pool looked like over the last 8 hours, and whether the placeholder strategies
could trade it. **A snapshot, not a forecast**: one bot's behaviour can change at any time.

## Files

| File | What |
|---|---|
| `swaps_8h.json` | 86 swaps, 8 hours up to block 12062699 (ts 1793769996 to 1793798736). Addresses are shortened to their last 6 characters. `impact` is the % price move of that swap. |
| `swaps_next_24min.json` | 4 more swaps (blocks 12062729 to 12062810) that were **not** used for tuning |
| `scan_swaps.py` / `scan_swaps_from_block.py` | read-only fetch of the swaps above (`.env` for RPC and pool; no key, errors go through `redact()`) |
| `replay.py` | replays a swaps file through the real `strategy.decide` (30 s ticks, 5 min cooldown, 0.3% fee per leg) |
| `forward.py` | live paper test: reads the pool price every 30 s for N minutes, sends nothing |
| `scan_liquidity.py` / `liquidity_profile.json` | the pool's whole liquidity layout: every initialised tick and its net liquidity (read-only) |
| `v3pool.py` / `validate_v3.py` | exact Uniswap V3 swap maths over that layout, and the check that it reproduces 89 of the 90 recorded swaps (the first has no earlier price to start from) |
| `simulate.py` / `grid.py` | the Monte Carlo and the take-profit x stop-loss grid; raw output in `simulation_results.txt` and `grid_results.txt` |

```bash
uv run python research/2026-11-04-pool-patterns/replay.py research/2026-11-04-pool-patterns/swaps_8h.json
```

## Findings

- **One scripted bot makes the market.** Recipient `...582026` made 77 of the 86 swaps. It
  alternates sell and buy (71 of 76 times), about 30 fETH each (24 to 41), moving the price
  3 to 5% per swap, every 2 to 30 minutes (median 6). The only other actor was tiny (about
  1 fETH, 0.14% impact) and last traded 5.3 hours before the scan. Neither is the bot's own vault.
- **The price oscillates.** In the last hours the price sat near 1027 to 1030 (fETH in f564) after its sells and
  1070 to 1075 after its buys; over the whole 8 hours the medians were 1037 and 1082 (ranges
  997 to 1062 and 1025 to 1102). The up-leg (buy after its sell, sell after its next buy) was
  +4.03% at the median over 35 cycles, 32 of 35 at least +3%, only 3 at least +5%. After the 0.6%
  round-trip fee that is about +3.4% per cycle, in f564.
- **Gas.** Its transactions pay almost no gas (shown as 0.000 gwei); the small actor paid
  1.5 gwei. Same-block swaps: 0.
- **Pool:** fee 0.3% (3000), tick spacing 60. **Not full range**: only 2.7% of the liquidity is
  full range. The rest sits in a handful of concentrated positions between 648 and 1500, so the
  active liquidity depends on the price: about 4.6e22 above 1034 (thin), 1.15e23 between 1028 and
  1034, 7.5e22 below 1028. The "liquidity changed" readings during this study (1.15e23, then 7.5e22,
  then 4.6e22) were the price moving across those boundaries, **not liquidity being removed**; an
  earlier note here saying someone removed a third of it was wrong. The exact model in `v3pool.py`
  reproduces 89 of the 90 recorded swaps (the first has no earlier price to start from) to the last digit with this static layout, so no position
  changed during the window.

## Replay results (in f564, per 1 f564 spent, sum of per-trade %; guard off)

Tuned on the 8 hours, so **in-sample and optimistic**: fills at the observed price, no price
impact from the bot's own trade, no competitors.

| Entry rule | TP / SL | Trades | Net |
|---|---|---|---|
| mean reversion, window 20, dip 2% (the old default) | 5 / 3 | 6 | +12% |
| mean reversion, window 20, dip 2% | 3 / 4 | 25 | +94% |
| mean reversion, window 10, dip 2% | 3 / 4 | 33 | +119% |
| sharp drop, look back 4, drop 3% | 3 / 4 | 34 | +123% |

Fill delays of 30 and 60 seconds barely changed this, so faster polling is not needed against
one bot.

## Out of sample

The 4 swaps in `swaps_next_24min.json` kept the pattern (buy, sell -3.44%, buy +4.13%, sell
-3.87%). Mean reversion (window 10) and sharp drop each made one trade, **+3.51%** net of fees.
One trade: it agrees with the replay, it does not confirm it.

## Live forward test (paper, 20 minutes, 08:56 to 09:16 local, `forward.py`)

Only two swaps happened, the first after 10.5 minutes of silence: a buy (+3.45%, 1051.57 to
1087.86) and a sell 4 minutes later (-4.11%, to 1043.15). All three rules (the old default, mean
reversion window 10, sharp drop) bought at 1043.15 within the same 30 s tick as the sell, and were
still holding at the end, marked -0.60% (the two fees). **No trade closed, so the test says
nothing about profit.** It only confirms that the entry fires right after the scripted bot's sell.
The old default's +5% take profit needs about 1095, above the bot's recent buy levels (1070 to 1088),
while +3% needs about 1074, which they reached.

## Monte Carlo on the exact pool (`simulate.py`, `grid.py`)

The real `strategy.decide` (30 s ticks, 5 min cooldown, the pattern guard) against 600 random
sessions of 8 hours per line, after a 3 hour warm-up in which the bot only watches. The pool is the
real tick layout with exact V3 maths, so price impact and fees come out as on the chain. The
scripted bot swaps 24 to 41 fETH at the observed random gaps and flips side with probability `p`
(0.93 observed). A swap that would push the price outside 750 to 1400 goes the other way instead
(a bot with finite inventory; the real range was 997 to 1103). f564 per session at 1000 f564 per
buy, mark-to-market for a position still open (about 45% of sessions end holding one).

| World | Guard off: mean (5th to 95th percentile) | Guard on (80% / 5%): mean | Mean of each session's largest drawdown, off / on |
|---|---|---|---|
| Pattern as observed (flip 93%) | +922 (+46 to +2311) | +843 | 49 / 47 |
| Same, cold start (no history) | +848 | +710 | 45 / 41 |
| Weaker pattern, flip 70% | +442 (-10 to +1054) | +164 | 137 / 88 |
| No pattern, random direction | +147 (-281 to +702) | +33 | 231 / 65 |
| Downtrend from the start (65% sells) | +403 (-184 to +1114) | +193 | 222 / 97 |
| Pattern, then a downtrend at a random time | +451 (-139 to +1281) | +395 | 220 / 80 |
| Pattern, then no pattern | +462 (-95 to +1259) | +366 | 174 / 70 |
| Its sells 10% bigger than its buys | +783 | +686 | 60 / 58 |
| The bot stops at a random time | +448 | +407 | 27 / 26 |
| A rival trades the reversal first (50%) | +302 | +183 | 81 / 53 |
| 10,000 f564 per buy | +8710 | +5948 | 492 / 395 |

- **The edge is largest while the pattern holds**: about +920 per 8 hours at 1000 f564 per buy,
  about 21 trades, 95% winners. Treat the size with care. It moves with the price band I assumed
  for the simulated traders (750 to 1400): with the band at 650 to 1800 it was +1016, at 850 to
  1250 +678, and at 950 to 1150 (close to the real range) +465. The simulated swaps are also
  rougher than the real ones (price move per swap 2.3 to 7.4% at the 10th to 90th percentile,
  real 3.4 to 4.8%; 71% of simulated up-legs reach 3%, real 91%), because the model flips side at
  random while the real bot looks like it steers the price back after an overshoot. No row has
  a standard error: two different seed sets gave +922 and +1010 for the same world.
- **Without the pattern the unguarded strategy still averaged a profit in this model** (+147 for
  random direction, +403 for a downtrend). This comes from the pool's *shape*, not from the
  strategy. A control with flat liquidity of similar depth gave -82 for random direction, and
  buying at random times in the real layout gave -44; the real layout is thin above 1034 and
  thick below, so the same fETH moves the price more upwards than downwards, which favours
  buying dips. It holds only while the liquidity providers leave their ranges alone. Do not
  read these rows as "the strategy is safe without the pattern".
- **What the guard does: less drawdown, and in this model less profit.** Mean largest drawdown
  fell by 56 to 72% when the pattern broke (no pattern 231 to 65, downtrend 222 to 97, pattern then
  downtrend 220 to 80, pattern then no pattern 174 to 70), partly just because it trades far less
  (2.8 trades per run against 19.3 with no pattern). It cost profit in every world: 8% with the
  pattern intact, 16% on a cold start, 12% when the bot's sells are 10% larger, about 60% when
  the pattern is only weaker (flip 70%), 40% with a rival, 32% at 10,000 f564 per buy. It did not
  improve the worst or the 5th-percentile session in the weaker-pattern world (-268 to -392 and
  -10 to -107). Whether it also costs profit without the pattern depends on the pool shape: on
  the flat-liquidity control the guard *added* profit (random direction -82 to +2, downtrend -296
  to -19), so the sign of that effect in the real pool is unknown. On the real recorded swaps
  replayed with the guard it made 26 trades and +94% against 34 trades and +122% without.
- **Guard thresholds** (a grid over flip 50 to 80% and slide 0 to 5% run once, plus a reviewer's
  re-run on fresh seeds): the step pairs come in sevenths, so the "80%" rule really means "at
  most one non-alternation in the last eight steps" (any threshold from 72 to 85% behaves the
  same). A looser flip rule of 70% earned more in all five worlds but with 30 to 120% more
  drawdown: a trade-off, not a clear win for 80%. The slide check at 5% added nothing measurable
  in any world; it is kept as cheap insurance against slow slides the model does not produce.
- **Take profit / stop loss** (`grid_results.txt`, guard off, mean f564): flat for TP 2 to 3 and
  SL 2 to 4 when the pattern holds; TP 4 and TP 5 are worse (+868 and +712 against +1010 at
  SL 4). TP 2 and TP 3 cannot be told apart in the downtrend world because exits happen on
  jumps of 3.4% or more. SL 2 was best in the pattern and downtrend worlds, SL 6 with no pattern.
- **Sharp drop and mean reversion (window 10)** gave similar results (within about 10% on the
  mean; they chose identical trades in only 9 to 23% of sessions).
- **Caveats:** the pattern is built into the model, so the "as observed" rows say the mechanics
  survive random sizes, timing, fees and our own impact, not that the pattern will last. The
  layout is static (liquidity providers can change it at any time). Fills happen at the instant
  of the read: no latency, slippage limit, failed swap, `tokenCap` or vault size. A rival is one
  fixed counter-trade. The 20-minute live paper test (`forward.py`) has no saved log, so its
  numbers can be checked arithmetically but not replayed. Run every script from the repository
  root: the paths are relative to it.

## Not done yet

A multi-day out-of-sample test on real data (including the guard thresholds), and a longer live paper or dry-run test.
