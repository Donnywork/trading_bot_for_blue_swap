# Fade after big swaps, and the exits, 2026-11-06

Question: the bot's policy looks slow next to others who profit right after the pool's big swaps.
What does the whole log show, and what would be better? **Offline: no RPC, no key, nothing sent.**
Data: `logs/prices.csv` (43 h of grid, 2026-11-04 13:05 to 2026-11-06 08:07, with outages) and
`logs/ledger.json` (56 closed live trades, +4326 f564). Run from the repo root. Same backtest engine
as `../2026-11-05-strategy-sizing-backtest/bt.py` (exact V3 fills, so impact grows with size).

| File | What |
|---|---|
| `pattern_stats.py` | big steps (>= 2%) not caused by our own trades: timing, direction, and where the price goes after them |
| `entry_slip.py` | where our 56 live buys and sells filled against the pool's first print after the step |
| `exit_grid.py` | cooldown x fill delay, take profit, stop loss / time stop, entry rule, size |
| `delay_backtest.py` | the settings in force against the proposed ones at fill delays 0 to 30 s; sizes |

## The pattern

- **One clock.** 84% of the gaps between big steps are multiples of 2 minutes (within 20 s); the swaps
  print at seconds 35 to 50 of a minute (as polled). Gaps: 2, 4, 6, 8 minutes, 10th to 90th percentile 2.1 to 18.8.
- **It alternates**: up then down 78%, down then up 87.5% (runs of 1: 158, of 2: 32, of 3: 5). Median step
  +3.2% up, -3.4% down; median level after an up step 1081, after a down step 1036.
- **Every big step is faded within seconds, by someone else.** Non-own small steps (0.3 to 2%) in the
  opposite direction follow within 30 s of the big one in 95 of 96 cases. Price after an UP step, vs right after it:
  -0.36% at 20 s, -0.60% at 40 s, -0.74% at 2 min, never higher in the first 2 minutes (P(>0) = 0.00, n = 77). After a
  DOWN step: +0.33% at 40 s, +0.38% at 2 min, +2.1% at 5 min, 92% of the time higher.
- So "profit after the big buy" is real, but it is on the **sell** side (the price falls back after a big buy);
  our bot only holds quote and only buys the dip, so it cannot take it.

## What the live trades show

- Our buy fills a median **+0.65%** over the first print after the down step (fee 0.3% + impact + the bounce
  that starts within 10 to 20 s). The lag from the step to our buy is already short (median 10 s), so being late is
  not the main loss; **each extra 10 s of fill delay costs about 10 points of the 43 h return** in the backtest below.
- A sell at the take profit fills -0.7% under the peak print.
- The time stops came from buys of "dips" to 1060 to 1071, mid-band: the next up step reaches +2.6 to +3.7% from there,
  never the 4% take profit.

## Backtest (mean reversion 20 / 2%, 9700 start, size 3200, exact V3 fills, in-sample)

Return over the 43 h, then first / second half by time. Raw output: `delay_backtest_results.txt`, `exit_grid_results.txt`.

| Settings | delay 0 s | 10 s | 20 s | 30 s |
|---|---|---|---|---|
| in force: TP 4, SL 5, time stop 40 min, cooldown 300 s | +54.5% | +39.9% | +29.2% | +25.3% |
| proposed: TP 2.5, SL 5, time stop 30 min, cooldown 60 s | +84.5% | +74.5% | +59.0% | +48.3% |

- The proposal beats the settings in force in both halves at every delay (delay 20 s: +48.5 / +9.6 against +25.2 / +3.2).
  Win rate 94 to 99% against 69 to 85%.
- Take profit (delay 20, cooldown 60): 1.5 to 2.5 best (+53 to +56%), 3.0 +50%, 3.5 +38%, 4.0 +30%. The earlier
  note called exits a plateau for 2 to 4; on this longer, newer data above 3 clearly loses.
- Cooldown 300 to 60 s: +4 to +5 points (swaps come every 2 to 4 minutes, a 5 minute wait skips cycles). 30 s adds nothing.
- Time stop 30 min best of 15 to 60 (+59% against +53% at 40), stop loss 3 to 8 makes little difference (SL 3 is worse in the worst trade).
- Entry rule hardly matters: mean reversion 20 / 2, sharp drop 2 / 2.5 and Bollinger 15 / 2 are within 3 points;
  stricter dips (3%, window 60) lose a quarter to a third of the trades and profit.
- Size is the biggest lever and the drawdown stays small: 3200 +59% (dd 1.4%), 5000 +82% (2.0%), 7000 +99% (2.7%),
  9000 +108% (3.4%), all at delay 20. The win rate falls from 94% to 90%. This is the pattern holding; the
  2026-11-05 stress tests still apply if it breaks.

## Two-sided rotation (hold fETH too, sell after a big buy, buy back after a big sell, liquidate to f564 at the end)

`two_sided.py`, raw output `two_sided_results.txt`. 9700 start, everything sold back to f564 at the end, 43 h.

| | delay 10 s | delay 20 s |
|---|---|---|
| long-only, proposed settings, all-in 9000 | +152% | +107% |
| two-sided rotation, start all f564 | +94% | +43% |
| two-sided rotation, start 50% fETH | +94% | +43% |
| two-sided rotation, start all fETH | +94% | +42% |
| hold 50% fETH, no trading | -0.1% | -0.1% |

- **Starting with fETH inventory changes nothing** (94.1 / 93.8 / 93.5%). Valued in f564 a round trip earns the swing once:
  buy at the trough, sell at the peak. The fall from the peak back to the trough is what makes the next buy cheap, it is
  not a second profit. Selling base at the peak and buying it back at the trough only reshuffles the same capital.
- The crude two-sided rule (trade every +/-2% print, no take profit) earns less than the long-only one, with twice the legs.
- No "short" exists in this pool, so the fall can only be sold by someone who already holds base, and that only
  matters if the score is counted in fETH, not f564.

## Caveats

In-sample: 43 h, one scripted counterparty, three knobs tuned on the same data (they agree in both halves, which is
the only out-of-sample evidence here). Fills take the recorded price (which includes our own earlier trades) plus the
exact V3 impact; the bounce after a step is modelled only through the delay. Not tested: a sell side (selling base after a big
buy needs base inventory, which the ledger and the "only sells what it bought" rule do not have).
