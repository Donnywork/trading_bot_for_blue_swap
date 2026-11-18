# Phase 2 seven-day pool-flow investigation

The observation window is **2026-11-10 22:09:24 UTC through 2026-11-17 22:09:24 UTC**, blocks **12107506–12157265**. The pinned final hash is `0xd6dcf56a116a1bcf143132a20606bfca855d95570f8cde2b01522da4dfdad820`.

## Coverage and reproducibility

The collector fetched **483 events in 462 successful transactions across 349 blocks**, from the two official Phase 2 pools. These include **410 swaps, 27 Mints, 21 Burns, 21 Collects, two Initializes and two observation-capacity changes**. The start of the window precedes pool initialization, allowing a complete reconstruction of liquidity from creation.

“Transactions” here means transactions emitting an event from either pool, including internal pool calls made through routers or vaults. **Failed/reverted attempts and calls emitting no pool event are outside this dataset.** Collecting logs does not enumerate every unsuccessful attempted trade.

All network work uses the competition's public Sepolia RPC proxy. No `.env`, API key, private key, signing or transaction submission is involved. The public proxy rejects full transaction objects in block requests, so transaction details are fetched individually. The collector is resumable and validates receipt/event/block hashes and the final pinned hash.

Run from the repository root:

```bash
uv run python research/2026-11-17-phase2-flow/collect.py logs
uv run python research/2026-11-17-phase2-flow/collect.py enrich
uv run python research/2026-11-17-phase2-flow/analyze.py
uv run python research/2026-11-17-phase2-flow/analyze.py actors
uv run python research/2026-11-17-phase2-flow/summarize.py
uv run python research/2026-11-17-phase2-flow/ordering.py
uv run python research/2026-11-17-phase2-flow/inspect_calls.py
uv run python research/2026-11-17-phase2-flow/validate_quotes.py
uv run python research/2026-11-17-phase2-flow/probe_rpc.py
```

`collect.py`, `validate_quotes.py`, `probe_rpc.py` and the first account-code lookup in `inspect_calls.py` make read-only public RPC requests. The other analysis commands are offline. Existing collection files preserve their original window when resumed.

## Cross-pool opportunities

The reconstruction applies every Initialize, Mint, Burn and Swap in block/transaction/log order. Each Swap's actual input is replayed against the liquidity that existed immediately before it. All 410 output amounts agree with the model, with maximum error about **2.30 × 10^-9 f564**. Reconstructed prices and liquidityNet match the independently scanned snapshot at block 12157163 exactly.

Cycle analysis evaluates **end-of-block states**, after all pool transactions in the block. It searches f564 input sizes up to **10,000 f564**, in both directions, with both pool fees and price impact included. It assumes full fills, normal token behavior, no new external trade before execution, and no additional executor price-limit/slippage constraint. Native ETH gas is excluded. It does not assume today's liquidity existed throughout the week.

There were 339 evaluable event-block states; 240 had a modeled best cycle exceeding **0.01 f564**. These are correlated observations, not 240 independently collectible profits. Collection-only blocks can repeat the same price state; a single opportunity can span many observations. Your own execution would change both pools and future opportunities. **Do not sum these profits or interpret them as a portfolio backtest.**

Selected archive QuoterV2 validations:

| End-of-block state | Buy fETH pool | Sell fETH pool | f564 input | f564 profit before gas |
|---:|---|---|---:|---:|
| 12108922 | 0.05% | 1% | 3,689.003353 | 91.802449 |
| 12142599 | 1% | 0.05% | 4,036.206075 | 79.373507 |
| 12132053 | 1% | 0.05% | 2,997.915342 | 54.593316 |
| 12157197 | 0.05% | 1% | 2,154.561255 | 17.983538 |
| 12157200 | 0.05% | 1% | 447.307323 | 0.770026 |

All eight selected historical two-leg quotes agreed with the model within **0.000000001 f564**. These calls establish historical pool output, not successful execution of the new vault. The existing planner's default 50-bps slippage allowance and 100-bps price-movement bound may reject opportunities shown here; operating constraints and whole-cycle gas must be incorporated before choosing a size.

The strongest example is size-sensitive: at block 12108922, 1,000 f564 would earn approximately 43.35 f564, 3,689 approximately 91.80, and 10,000 would **lose about 159.90 f564**. The largest allowed trade is not necessarily the best trade.

The competition objective remains aggregate portfolio return measured in f564. A trade's 2.49% return on deployed capital is not a 2.49% portfolio return. The cash gain contributes `gain / portfolio_value_before`, with any marked value change of existing fETH accounted for separately under the competition's scoring formula. Native ETH gas must also have an explicit operating budget even if it is outside that score.

## Recurring actors and competition

The pools recorded **247 narrow-pool swaps and 163 wide-pool swaps**, from 11 signers. Frequent participation is evidence of recurring activity, not proof of automation by itself.

| Signer | Swaps | Narrow / wide | Observed behavior |
|---|---:|---:|---|
| `0xd067640b2e032b1a878bd87b4d85cb5a87ef64ef` | 226 | 130 / 96 | Trades through `0x7e0c07c6d5262727b598e299a14b75acb3e5666f`; 89 same-block cross-pool pairs with positive f564 pool flow and near-zero net fETH |
| `0x6011139d5f16689bde1d245c6247b088f9582026` | 104 | 46 / 58 | Named market bot; signer and recipient, calls SwapRouter02 multicall; median swap value about 4,065 f564 |
| `0x21b1a1e9f20377f4cbf6d874cfcf3df37699c7b7` | 53 | 52 / 1 | Recurring narrow-pool participant; many 24-second gaps between its own trades |
| `0x2885f394be3a5c36a0ce67701bfb6c49bb1fb8db` | 14 | 9 / 5 | Less frequent participant across both pools |

The named bot has no account code at the ending block. Its 104 matching transactions are signed directly by that address and sent to SwapRouter02. Its observed median gap is **5,388 seconds**, with gaps from **1,800 to 33,300 seconds**. A configured 2,700-second schedule does not describe every successful observed swap. Per-pool direction flips occur in about 46% of successive observations, so a naive alternating buy/sell predictor is not established.

After 59 of the named bot's swaps, the next swap by a different signer occurred within 36 seconds: 47 by `0xd067…64ef`, 12 by `0x21b1…c7b7`. Median delay to a subsequent different signer is 36 seconds. These are mined ordering associations; they do not prove when another actor first saw the market bot's transaction.

There are **zero observed single transactions containing swaps in both pools**, but **89 same-block two-pool groups** by `0xd067…64ef`. Each group is base-neutral to 1e-12 fETH and has positive f564 flow from the pools. This is strong evidence of cross-pool arbitrage through separate transactions, not an atomic two-swap contract call. Net pool flows exclude other transfers, LP changes and operating costs; they are not complete account-level P&L.

A recent concrete race:

1. **2026-11-17 21:55:36 UTC, block 12157197**: the named market bot buys in the wide pool, moving its price to approximately 1,036.123 f564/fETH while the narrow price is approximately 1,008.346. The best reconstructed cycle is **+17.983538 on 2,154.561255 f564**.
2. **21:56:12 UTC, block 12157200**: `0xd067…64ef` buys in the narrow pool and sells in the wide pool in two transactions, producing approximately **+17.213512 f564**, with zero net fETH. Its recorded gas costs total approximately **0.004497 native ETH**.
3. The remaining best cycle is approximately **+0.770026 f564**. Both initial and remaining states are validated with historical QuoterV2 calls.

Transactions: market bot [`0xb27590…1462`](https://sepolia.etherscan.io/tx/0xb27590cacf5a3ec65ab2b1cb7fe9a767aa182e84d6af91677b4db446ae2e1462); competitor [`0x29a039…a8ebf`](https://sepolia.etherscan.io/tx/0x29a039fa700f354b916e4cd7544fab1b60f9876e78e3500222232d022a0a8ebf) and [`0x94d9aa…5c2df7`](https://sepolia.etherscan.io/tx/0x94d9aab7cc28ea668c33102b440fd63fa9ce1db9f2948032a1729a490a5c2df7).

The competitor's median effective priority fee is about **5.777 gwei**, versus **2 wei** for the named market bot. These are observed choices, not a recommended fee or proof that raising a fee guarantees ordering. The narrow-pool recurring participant's median is 10 gwei.

The detector found no same-pool, same-block pattern where one signer trades in the target's direction immediately before it and reverses immediately after it. This does not rule out other forms of front-running or multi-block inventory strategies.

Positive modeled edge above 0.01 f564 spans 35 continuous episodes, including tiny residual opportunities. The median episode duration of 7,572 seconds is **not** the lifetime of the maximum profit: most value can disappear within seconds while a small residual remains.

## Alchemy pending observer

**Completed live observation:** the [pending watch report](pending_watch_report.md) records five pending transactions matched to successful mined transactions. The named market bot's notification lead was 1.228 seconds; the arbitrage competitor's four transactions led by 5.182–8.390 seconds. The requested additional 20-minute window was quiet, and the observer has stopped. This later run supersedes the initial unconnected status described below.

The user supplied the information that the named market bot submits through Alchemy. This is external context, not attribution inferred from chain records.

`pending.py` subscribes to `alchemy_pendingTransactions` and `alchemy_minedTransactions` for **all 11 observed swap signers**. It requests full transaction objects so nonce, calldata, destination and fee fields are available before inclusion if Alchemy exposes them. Filtering only pool destinations would miss router and vault calls.

The observer verifies Sepolia chain ID, records UTC and monotonic arrival times, identifies same-nonce replacement hashes, preserves removed/reorg notifications, and records whether a mined transaction was observed pending first. `notification_lead_seconds` measures local pending-to-mined **notification arrival**, not true submission-to-inclusion latency. A mined notification can arrive after actual inclusion.

It uses `ALCHEMY_WS_URL`, `RPC_URL`, or an Alchemy entry in `RPC_FALLBACK_URLS` from the **process environment**, as explicitly chosen by the user. HTTPS Alchemy Sepolia URLs are converted to WSS privately. It never reads `.env`. Endpoint values and provider exception bodies are never printed or persisted. The script has no signer, private-key use, send method or trading control.

```bash
# With an Alchemy Sepolia connection already exported into the process environment:
uv run python research/2026-11-17-phase2-flow/pending.py --probe
uv run python research/2026-11-17-phase2-flow/pending.py --seconds 7200
```

On macOS zsh, enter the WebSocket URL without echoing it or placing it in shell history, then run the observer in the same terminal:

```zsh
read -rs 'ALCHEMY_WS_URL?Alchemy Sepolia WebSocket URL (hidden): '
export ALCHEMY_WS_URL
uv run python research/2026-11-17-phase2-flow/pending.py --seconds 7200
```

An export in a separate terminal does not update an already-running agent's environment. Launch the observer in that terminal, or start a new agent session inheriting the exported variable. Do not paste the credential into chat.

Status goes to `pending_status.json`; observations go to uniquely named `pending_observations-*.jsonl` files. A disconnected session stops and records the gap. It does not silently reconnect and claim uninterrupted coverage. A short empty observation cannot establish invisibility given the target's long, irregular historical intervals.

Initial connection check: **no Alchemy endpoint was present in the session's process environment**, so no subscription was established and no `.env` was read. Pending visibility remains unmeasured until a configured session is launched.

## Submission-node attribution and trading ahead

A signed Ethereum transaction records the signer, destination, nonce, input, value, gas fields and signature. **It does not record the RPC URL, provider account, IP address or originating node.** Receipts and block metadata do not add that attribution. The block fee recipient or block extraData identifies aspects of block production, not the trader's submission endpoint. Shared gas settings or timing may suggest common software; they are not proof of a common node.

The public competition proxy rejected `web3_clientVersion` and `txpool_status` as unavailable. It accepted a `pending` block request, but returned a populated block hash; this is not evidence of a usable pre-inclusion transaction feed. No historical pending observations were collected, and no WebSocket pending subscription was established.

A future read-only visibility experiment should record first-seen transaction hashes and local monotonic times from independently accessible Sepolia pending feeds, decode the observed signers' router/vault calls, and compare against new-block arrival times and eventual receipts. Record missed transactions, replacements, reconnect gaps and lead-time distributions. A provider seeing a transaction first does not prove it received the original submission: propagation, peering and measurement location affect arrival time. Alchemy explicitly limits its pending subscription to transactions visible in its mempool.

Trading before another transaction requires enough observed lead time, a correct prediction of its effect, and favorable block ordering. A higher priority fee alone does not guarantee order. A scheduled mined timestamp is not a submission timestamp. The present dataset establishes historical post-block arbitrage opportunities; it does not establish reliable front-running profitability or a specific submission node.

## Data files

- `manifest.json`, `pool_events.json`: pinned collection bounds and raw pool logs.
- `blocks.json`, `transactions.json`, `receipts.json`: block headers/order and matching transaction details/receipts.
- `decoded_events.json`, `liquidity_changes.json`: decoded events and full historical liquidity changes.
- `swaps.csv`, `swaps.json`, `actors.json`, `target_swaps.json`: per-swap flow, signer profiles and the named target's activity.
- `opportunities.json`, `opportunity_episodes.json`, `summary.json`: end-of-block cycle estimates and observed timing summaries.
- `archive_quotes.json`: raw integer historical QuoterV2 validations.
- `observed_cross_pool.json`, `following_trades.json`: observed cross-pool transactions and ordering associations.
- `rpc_capabilities.json`: public proxy capability-check results.

References: [Ethereum transaction fields](https://ethereum.org/en/developers/docs/transactions/), [Geth subscriptions and connection limitations](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub), [Alchemy pending visibility](https://www.alchemy.com/docs/reference/newpendingtransactions).
