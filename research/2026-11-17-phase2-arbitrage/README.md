# Phase 2 liquidity and two-pool cycle snapshot

Observation: **2026-11-17 21:48:24 UTC**, Sepolia block **12157163**, hash `0x042815617d14269b9725cfeb738d475eaf43c6710de824dc3e1a07f2bfbef701`.

The user asked for careful investigation of concentrated liquidity while building an atomic arbitrage executor. All chain work was read-only, through the competition site's public RPC proxy. No `.env`, user API key, signing or transaction was used. This is one snapshot, not a profitability backtest or forecast.

## Method

`inspect_pools.py` reads token order, fee, spacing, slot0, liquidity and **all tick bitmap words** for both official pools, then reads every initialized tick. Multicall batches 40 reads per request. Every call uses the same block. The sum of liquidityNet at/below the current tick matches `liquidity()` exactly; the total sum across all ticks is zero. The block hash is checked again after the scan and quotes.

The 0.05% pool has spacing 10: 694 bitmap words and 16 initialized ticks. The 1% pool has spacing 200: 36 words and 6 initialized ticks. `snapshot.json` preserves raw profiles, derived price intervals and actual QuoterV2 results. `verify_snapshot.py` independently checks the router/quoter factory, both `getPool()` registrations and both tokens' 18 decimals at that block; see `verification.json`.

Both pools have token0=f564 and token1=fETH. Human prices below are **f564 per fETH**, so they invert raw token1/token0 prices. Raw sqrt price limits must respect that inversion.

## Where liquidity actually sits

| Pool | Spot price | Current interval between initialized ticks | Other important ranges |
|---|---:|---|---|
| 0.05%, `0x68D8bC396df8e791EFd45A9b214CB07fe4300f48` | 1008.345956 | 984.029054–1050.112073 | broader core approximately 940–1060, shoulders/tails approximately 650–1501, plus full-range backstop |
| 1%, `0xC67Fcb8459C792aE1E271f24CE212Ba90eCC2920` | 1017.775295 | 934.169615–1053.266981 | broad range approximately 99.461–10092.411, plus full-range backstop |

An initialized-tick interval is an aggregate depth segment, not proof of one LP position. The published [phase configuration](https://fin564.vercel.app/api/competition/phases) describes an initial narrow 940–1060 core and wide 100–10000 band. The actual ticks show additional overlaps. This scan cannot attribute them to a participant or establish that the planned day-7 recentering has occurred; that requires Mint/Burn history.

Current narrow active liquidity is **56.63 times** its full-range-only depth; wide active liquidity is **90.03 times** its full-range-only depth. In the narrow pool, crossing above 1050.112 reduces active liquidity from about 28609 to 16916 human liquidity units. Above 1060.665 it drops to about 2750. Those values are liquidity parameters, not balances of either token.

At this snapshot, a buy consuming approximately **18,633 f564** reaches the narrow pool's next upper boundary; selling approximately **11.070 fETH** reaches its next lower boundary. The corresponding wide-pool amounts are approximately **6,344 f564** and **15.789 fETH**. These calculations include the input fee, assume no intervening trades or LP changes, and are boundary estimates, **not profitable position sizes**. Full-range liquidity means trading can continue beyond the main bands, but with much worse impact. Hard-coding a range or sizing from total pool balances would be wrong.

## Executable cycle results

Fees alone require a favorable price ratio greater than `1 / (0.9995 × 0.99)`, a **1.060631%** gap, before price impact. The observed favorable spot gap was only **0.935129%**.

Each quote starts in f564, buys fETH in one pool, then quotes selling exactly that received amount in the other pool at the same block. These distinct pools make the two read-only quote calls a valid deterministic snapshot of the route. Fees and price impact are already in output; native ETH gas is excluded.

| f564 input | Buy 0.05%, sell 1%: profit f564 | Buy 1%, sell 0.05%: profit f564 |
|---:|---:|---:|
| 1 | -0.001246 | -0.019666 |
| 100 | -0.162615 | -2.003521 |
| 1,000 | -5.071630 | -23.377598 |
| 5,000 | -100.507430 | -189.804544 |
| 10,000 | -382.620675 | -582.131346 |

All 18 sampled routes/sizes lost f564 before gas. They should not be executed. A visible price discrepancy is insufficient evidence of an opportunity.

`analyze_snapshot.py` reconstructs the same cycles with the saved liquidity and `v3pool.py`, reflecting tick direction for token0=f564. Maximum absolute disagreement with QuoterV2 was **2.03 × 10^-9 f564**, including sampled swaps crossing initialized ticks. `analysis.json` records these calculations. The full-range backstop plus all tick changes matter to that agreement.

## Executor implications

`contracts/ArbitrageVault.sol` fixes the token pair, router and two canonical pools; routes can run in either direction. It applies limits to both legs, requires nonzero directional sqrt price limits and output minima, refuses partial input consumption, preserves deposited fETH, clears approvals and checks final f564 profit atomically. Range failures or insufficient profit revert the whole cycle, though a mined revert still burns gas.

The contract has no assumptions about a constant pool price, constant depth, or the published initial ranges. It is an execution primitive, not an opportunity detector or a proven competition strategy. The next investigation needs observations over time, LP changes, executable sizing under owner caps, total gas estimates, and aggregate portfolio return under the competition's scoring rules.

## Reproduce

From the repo root, the first and third commands make public read-only RPC calls; the second is offline:

```bash
uv run python research/2026-11-17-phase2-arbitrage/inspect_pools.py
uv run python research/2026-11-17-phase2-arbitrage/analyze_snapshot.py
uv run python research/2026-11-17-phase2-arbitrage/verify_snapshot.py
```

The scan command overwrites `snapshot.json` with a new block. Keep this study's original snapshot if comparing dates. The regression test intentionally uses the recorded JSON offline.

References: [Uniswap concentrated liquidity](https://docs.uniswap.org/concepts/protocol/concentrated-liquidity), [SwapRouter02 V3SwapRouter source](https://github.com/Uniswap/swap-router-contracts/blob/main/contracts/V3SwapRouter.sol). Its `exactInputSingle` struct has no deadline, so the vault enforces its own deadline. A sqrt price limit may leave input unspent; the vault's exact-consumption checks deliberately reject that outcome.
