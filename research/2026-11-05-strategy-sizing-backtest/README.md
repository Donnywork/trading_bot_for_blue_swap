# Strategy and position-size study, 2026-11-05

Question: the live bot loses on runs of rising / falling prices, and it buys with everything the
vault holds. What would be better, and how do the options compare? **Offline: no RPC, no key, nothing
sent.** Data: `logs/prices.csv` (21 h, 2026-11-04 13:05 to 2026-11-05 10:05) and `logs/ledger.json`
(44 closed live trades, +3806 f564). Run everything from the repo root.

| File | What |
|---|---|
| `bt.py` | backtest engine: recorded prices on a 10 s grid, fills through the exact V3 maths of the real tick layout (`../2026-11-04-pool-patterns/v3pool.py`), so impact grows with size; entry rules, sizing hooks, scale-in |
| `audit.py` | takes the 44 **live** entries and asks what each extra entry filter would have blocked |
| `exits.py` | re-runs the 44 live entries with other exits (take profit, stop loss, time stop) |
| `exp2.py` | whole-strategy replay on the recorded prices with 18 sizing rules |
| `mc.py` | sizing under stress: 60 trades per path, win rate 85 / 70 / 50 / 30% |
| `mc_world.py` | entry rules in the earlier study's simulated pools (observed pattern, weaker, none, downtrend, pattern that breaks) |
| `*_results.txt` | the raw output of each |

## What the live losses have in common (7 of 44)

- Five of the 7 entered above 1050; only 6 of the 37 winners did. Winners mostly bought 1020 to 1045.
- Two were "dips" of -0.4 to -0.6% off a fresh spike (1084, 1074). The 15-sample Bollinger band
  (2.5 min at 10 s polling) sees them as 2 sigma, but they are wobbles, not the scripted bot's -3 to -4% sells.
- Two bought the first sell of a two-sell run (1094 to 1054 to 1029; 1075 to 1030 to 1055 to 986) and
  held through the second. Two-in-a-row runs are 22 of ~150 big steps; 3-in-a-row is rare.
- The rest were holds that outlived the next sell.

## Findings

- **Exits are a plateau, do not tune them.** On the 44 live entries, take profit 2 to 4 x stop loss 4 to 7
  gives +117 to +149 (sum of per-trade %) against +134 for 3 / 5 / 60 min. A 3% stop loss costs ~20%,
  time stops under 45 min cost 3 to 18%. Differences between the good cells are inside the noise of n = 44.
- **Entry filters trade tail for trades.** "Price in the lowest 50% of the last 360 samples" blocks 4 of 7
  losers and 4 of 37 winners, sum +94.4% vs +94.6%, worst trade -6.3% to -4.8%. Filters that are
  stricter (below the average by 1.5 to 2%) block 5 to 6 losers but also 12 to 22 winners and cut
  the sum by a third or more. An absolute price cap (<= 1050) looks good and is pure overfit.
- **Size has a cost.** Realised round trip (fee + impact) on the live trades: 0.71% at ~1000,
  1.42% at 5000 to 7500, 1.82% at 7500 to 9000. The exact-V3 table: 0.73% at 1000, 1.26% at 5000,
  1.79% at 9000, 4.5% at 30000. Profit per trade grows less than linearly with size, risk linearly.
- **All-in is the best use of a pattern that holds and the worst use of one that breaks.** On the
  recorded prices it returned +122% vs +66% for 50% of equity (drawdown 7.0 vs 3.5%). Under stress
  (`sizing_stress_results.txt`, median final multiple of the start): 2.18x vs 1.80x if the pattern holds,
  0.52x vs 0.77x at a 50% win rate, 0.08x vs 0.30x in a downtrend.
- **Scale-in (half now, half after a further -3%)** removed the losing trades on the recorded prices
  (100% wins, +84% for the whole stack vs +122%), but that was one price path. Stress-tested later
  (see "Later tests"): it does **not** hold up.
- In the simulated pools `sharp_drop` 12 / 3 made about twice what Bollinger 15 / 2 did (it trades
  twice as often), with about twice the drawdown; on the recorded prices the two were equal per trade.
  Mixed evidence, not a verdict.

## Later tests (same day)

**The scripts for these three were throwaway (`drafts/`, deleted); only the numbers below are kept.**
All use the earlier study's simulated pools (`../2026-11-04-pool-patterns/simulate.py`: exact V3 pool,
our trades move it), entry Bollinger 15 / 2 + guard 60 / 5, exits 3% / 5% / 60 min unless said,
start 5400 f564, compounding. A cell reads "median final multiple | 5th percentile | mean max
drawdown %". Worlds: pattern as observed, weaker (flip 70%), none, downtrend (65% sells), pattern
that turns into a downtrend, pattern that turns random.

### 1. Sizing and scale-in (200 sessions x 8 h per cell)

| Rule | Observed | No pattern | Downtrend | Pattern then downtrend |
|---|---|---|---|---|
| all-in (now) | 1.55 / 1.11 / 5.4 | 1.01 / 0.82 / 11.9 | 1.09 / 0.81 / 13.5 | 1.23 / 0.91 / 11.9 |
| half of equity | 1.23 / 1.04 / 2.8 | 1.01 / 0.91 / 5.7 | 1.06 / 0.92 / 6.4 | 1.11 / 0.95 / 5.9 |
| risk 2% per trade | 1.15 / 1.03 / 1.9 | 1.01 / 0.94 / 4.0 | 1.05 / 0.95 / 4.3 | 1.08 / 0.96 / 4.0 |
| scale-in, all-in, 50/50 at -3% | 1.25 / 1.05 / 3.4 | 1.01 / 0.89 / 7.2 | 1.06 / 0.88 / 7.9 | 1.10 / 0.90 / 8.0 |
| scale-in, half, 25/25 at -3% | 1.10 / 1.01 / 1.7 | 1.01 / 0.94 / 3.7 | 1.03 / 0.95 / 3.9 | 1.05 / 0.95 / 4.1 |

- Scale-in gives fewer losing trades per run (1.0 against 1.4) but each loses more: worst trade -12.6%
  of equity against -8.8% for half. In every world where the pattern breaks its drawdown is above
  half's and its median is no better. **Not recommended.**
- "Half, de-risk to 15% after a 10% drawdown" is identical to half here: 8 hours rarely draw down
  10%. Its benefit shows only in the 60-trade path test (`sizing_stress_results.txt`).

### 2. Splitting the buy or the sell (half of equity in total, 150 sessions x 8 h)

Buying in three slices over two minutes, selling half at +2% and the rest at +4% (with or without a
stop moved to break-even), three exit levels, and two entries plus two exits all ended **no better
than one half-size buy and one sell** (observed world 1.23x; the variants 1.09x to 1.22x). Slicing
the buy changes nothing because the pool does not move back between slices; slicing the sell trades
a little drawdown for a little profit, as a smaller position does. Not tested: slices spread over hours,
more exit levels, mainnet gas.

### 3. Long-term holding, buying small on dips and selling small (24 h simulated, 60 sessions; and the 21 h recorded prices)

Two kinds, no stop loss unless said. **Lots (L):** each buy is its own lot (5 to 10% of equity),
at least 1 to 1.5% below the previous lot, under the 1 h average, each sold at +2 or +3%.
**Band (B):** buy a 5 to 10% chunk 1 to 2.5% under the 1 h (or 3 h) average, sell a chunk the same
distance above it, inventory capped at 40 to 50%.

| | Recorded 21 h | Sim: pattern holds | Sim: no pattern | Sim: downtrend | Recorded prices, steady -30% drift |
|---|---|---|---|---|---|
| swing (half, as now) | 1.30x / 2.6% | 2.11x / 4.3% | 1.05x / 9.9% | 1.28x / 11.3% | 1.21x / 3.8% |
| L1 10% lots, <= 5, +3% | 1.08x / 0.7% | 1.35x / 3.0% | 1.24x / 11.4% | 1.58x / 7.1% | 1.08x / 0.9% |
| L2 same, -10% stop per lot | 1.08x / 0.7% | 1.35x / 2.3% | 1.10x / 8.5% | 1.43x / 5.7% | 1.08x / 0.9% |
| L3 5% lots, <= 10, +2% | 1.04x / 0.1% | 1.18x / 1.3% | 1.16x / 6.9% | 1.27x / 3.7% | 1.03x / 0.3% |
| B1 band +-1.5%, cap 50% | 1.09x / 1.5% | 1.41x / 6.6% | 1.25x / 17.6% | 1.83x / 11.5% | 0.98x / 7.1% |
| B2 band +-2.5%, cap 50% | 1.08x / 2.0% | 1.24x / 7.3% | 1.26x / 16.8% | 1.76x / 11.6% | 0.92x / 11.0% |
| B3 band 5%, +-1%, 3 h avg, cap 40% | 1.02x / 0.6% | 1.11x / 5.1% | 1.17x / 13.1% | 1.30x / 7.7% | 0.96x / 5.1% |

(multiple of the start / mean max drawdown; holding half the money in the base token with no trading
would be 0.85x under the -30% drift.)

- With the pattern intact, or on the recorded prices, swing trading wins by a wide margin: the long-term
  variants keep most of the money idle (average inventory 0 to 15% on the recorded prices).
- Band strategies beat swing in the worlds where the pattern breaks, at a larger drawdown (11 to 18%
  against 10 to 12%) and with ~30% of equity in inventory. **This is probably overstated:** the simulator's
  "downtrend" bounces off the price band, and the pool's liquidity shape favours buying dips (see the
  2026-11-04 README).
- Under a steady -30% drift all three band variants lose (0.92x to 0.98x) and end holding 27 to 57%
  of equity in the base token; swing, which exits fast, still makes 1.21x; lots hold about 10% and stay
  near +8%.
- The bot's ledger records one open position and sells exactly what it bought; lots or bands would
  need a multi-lot ledger, so this is a code change, not a settings change.

## Caveats

- 21 hours, one scripted counterparty, 44 trades. In-sample for everything tuned here.
- The replay is optimistic: it finds 1 loser in 36 trades where the live run had 7 in 44 (settings
  changed during the day, fills lag). Trust the comparisons more than the absolute returns.
- A fixed liquidity layout (from 2026-11-04) is assumed. Our own trades do not move the scripted bot.
