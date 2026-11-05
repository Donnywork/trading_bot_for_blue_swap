# Why four live swaps reverted, 2026-11-05

Four swaps sent by the bot were mined and refused (`Transaction reverted: 0x…`): a sell at 07:52, a sell
at 08:31, a buy at 10:08 and a sell at 12:25. **Read-only**: public Sepolia data through a public node, no
key, nothing sent. Run both scripts from the repo root with `uv run python research/2026-11-05-reverted-swaps/<file>`.

| File | What |
|---|---|
| `trace.py` | the call tree of a failed transaction (`trace_transaction`, which `ethereum-sepolia-rpc.publicnode.com` answers), next to a successful swap of the same contract |
| `gas_need.py` | for each failed transaction: the gas limit it was sent with, the gas the same call needs in the state it was sent against and in the state it was mined in, and whether it runs with its own limit and with twice that |

## What it was

**Not slippage. Out of gas, inside the pool.**

- Replaying each call in the state it was mined in gave an output above `amountOutMinimum` every time
  (for example 3313.89 against a minimum of 3301.12 at 12:25), and in two of the four the price had moved in
  our favour.
- The call tree of the 12:25 sell ends in `pool.swap ... ERROR Out of gas`. A successful swap of the same
  contract used about 177k gas in total; the failed one had used about 314k when it ran out.
- The gas limit was the estimate times `GAS_BUFFER` (1.2). A swap's gas depends on how many initialised price
  ticks it crosses, which depends on where the price is when it is mined. In each block another swap (the
  scripted counterparty) landed before ours and moved the price, so ours crossed more ticks:

| Swap | Gas limit sent | Needed in the state it was sent against | Needed in the state it was mined in | With its own limit | With twice the limit |
|---|---|---|---|---|---|
| 07:52 sell | 336,366 | 281,838 | 341,274 | fails | succeeds |
| 08:31 sell | 336,340 | 281,796 | 341,353 | fails | succeeds |
| 10:08 buy | 336,555 | 279,578 | 387,117 | fails | succeeds |
| 12:25 sell | 336,255 | 281,661 | 339,141 | fails | succeeds |

The need rose by 21 to 38%, against 20% of headroom.

## The fix

`bot_contract.SWAP_GAS_BUFFER = 2.0` for swaps (`GAS_BUFFER = 1.2` stays for deploys and owner transactions).
Unused gas is refunded, so the larger limit costs nothing on the gas actually burnt.

## What it also showed

- The pool changed since the liquidity snapshot of 2026-11-04: in-range liquidity jumped from 7.48e22 to
  7.99e22 when the price crossed a tick near 1023, so there are initialised ticks that the saved layout does
  not have. The dashboard's "Scan pool liquidity" button refreshes it.
- After a failed attempt an exit waits the cooldown (300 s) like any trade, and three failures in a row halt
  the bot. Both are still as before.
