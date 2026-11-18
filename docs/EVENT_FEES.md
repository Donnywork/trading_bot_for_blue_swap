# Phase 2 competitive fees

`EVENT_FEE_POLICY=fixed` is the default. It preserves the exact Phase 2 `EVENT_PRIORITY_FEE_WEI` and `EVENT_MAX_FEE_WEI` transaction fields (2 and 20 gwei by default), including refusal when the maximum cannot cover the current base fee plus priority. It performs no background fee-history requests. Legacy fee selection and `DRY_RUN` defaults are unchanged.

`competitive` prioritizes timely inclusion and competitive bidding under explicit technical limits. Native Sepolia ETH is assumed plentiful. It uses one policy for front-run entries, atomic backruns, independent arbitrage and recorded-position exits. Paying more does **not** guarantee inclusion, ordering, a front-run, or profit. A receipt-triggered backrun still waits for the target receipt and exact inclusion-block state; it cannot reserve a place next to the target.

## Configuration

The operator edits configuration privately and applies it through the normal [service handoff](DEPLOY.md#phase-2-on-ubuntu). The dashboard cannot change fees, limits or credentials. This illustrative opt-in uses the defaults for all competitive controls:

```dotenv
EVENT_FEE_POLICY=competitive
EVENT_FEE_BASELINE_WEI=20000000000
EVENT_FEE_NETWORK_BPS=12500
EVENT_FEE_COMPETITOR_BPS=15000
EVENT_FEE_TIP_CAP_WEI=100000000000
EVENT_FEE_MAX_CAP_WEI=200000000000
```

The 20 gwei baseline and 1.25× / 1.5× multipliers are experimental starting values, not measured optimal bids. They have been tested offline; no live ordering or latency improvement is established.

| Setting | Default | Validation and meaning |
|---|---:|---|
| `EVENT_FEE_POLICY` | `fixed` | Exactly `fixed` or `competitive` |
| `EVENT_FEE_BASELINE_WEI` | `20000000000` | Competitive minimum desired tip; integer 1–100,000,000,000 wei/gas |
| `EVENT_FEE_NETWORK_BPS` | `12500` | Integer 10,000–50,000 basis points; 10,000 = 1× |
| `EVENT_FEE_COMPETITOR_BPS` | `15000` | Integer 10,000–50,000 basis points |
| `EVENT_FEE_TIP_CAP_WEI` | `100000000000` | Competitive priority ceiling; baseline ≤ cap ≤ 100,000,000,000 |
| `EVENT_FEE_MAX_CAP_WEI` | `200000000000` | Competitive total fee ceiling; priority cap ≤ total cap ≤ 200,000,000,000 |
| `EVENT_PRIORITY_FEE_WEI` | `2000000000` | **Fixed mode only**, exact priority field; integer 1–100,000,000,000 |
| `EVENT_MAX_FEE_WEI` | `20000000000` | **Fixed mode only**, exact maximum field; priority ≤ maximum ≤ 200,000,000,000 |
| `EVENT_COMPETITORS` | Existing two signers | Existing comma-separated signer allowlist; may be empty |
| `EVENT_MAX_GAS` | `1500000` | Existing gas-unit ceiling, including the unchanged `SWAP_GAS_BUFFER=2.0` |

All supplied settings are validated even when inactive. The old fixed fields do not double as competitive ceilings. Amounts are integer **wei per gas**, not gwei or ETH; 1 gwei = 1,000,000,000 wei. Multiplication uses integer basis points, rounded **up** to the next wei. The fee policy never changes gas-unit allowances, converts trading profit into gas, or optimizes daily spending.

## Selection and ceilings

At the final fresh block reference:

```text
desired_tip = max(baseline,
                  ceil(network_tip * network_bps / 10000),
                  ceil(competitor_tip * competitor_bps / 10000))
selected_tip = min(desired_tip, tip_cap, max(0, max_cap - base_fee))
desired_max_fee = 2 * base_fee + selected_tip
selected_max_fee = min(desired_max_fee, max_cap)
```

Missing inputs do not contribute to the maximum. `desired_priority_wei` records the unconstrained desired tip; `desired_max_fee_wei` records the desired headroom for the **selected** tip, matching the formula above. Selected fields are frozen before signing. A permitted bid must have a positive selected tip and cover `base_fee + selected_tip`. The decision exposes each ceiling that reduced priority or headroom, including when the baseline is unavailable. If the base fee consumes the entire maximum, the attempt fails before signing; normal pre-send recovery applies. The next definite attempt may use a new decision. Submitted transactions are never repriced or replaced.

For example, a 10 gwei base fee, 24 gwei network input and competitor fields of 30 gwei priority / 35 gwei max yield a competitor estimate of **25 gwei**. Desired and selected tip are 37.5 gwei; selected maximum is 57.5 gwei. A deliberately extreme competitor sample cannot exceed either ceiling. If the desired tip is 300 gwei at a 120 gwei base, selection is limited to 80 gwei tip / 200 gwei max and reports both constraints. It does not claim to outbid that competitor.

Exits use the same aggressive baseline whenever ceilings allow it, even with paused entries, a disconnected feed or missing evidence. Fee evidence never authorizes an entry or disables exits. Operator native ETH must cover `EVENT_MAX_GAS * selected_max_fee` at each attempt; entering Live also checks the configured policy ceiling (`0.3 ETH` at competitive defaults; `0.03 ETH` at fixed defaults). Insufficient balance is a pre-send refusal, not a reason to invent a cheaper bid. Owner caps, contract simulation and snapshot/target/control guards still apply.

## Bounded evidence and deterministic fallback

One session-wide, thread-safe cache survives execution-mode changes. Its optional worker is scheduled on heads and maintenance, starts at most once every **12 seconds**, and never overlaps itself. It uses the shared RPC provider, pacing and session counters under **Fee evidence refresh**. No cache lock is held across HTTP waits. A pass reads a fresh latest header, requests `eth_feeHistory(8, reference_block, [90])`, then checks the reference hash again: at most three logical RPC reads. If the last successful sample has the same block hash, it only reads the latest header, without refreshing the sample age. Provider retries/failover can add transport attempts; these remain counted. Halted/stopped services do not schedule new work; an already-running bounded read can finish.

The network input is the **maximum of the eight block-level, gas-weighted 90th-percentile priority rewards**. It is not the percentile of all transactions pooled across the window. Positive rewards are used; all-zero, missing, inconsistent or malformed windows are unavailable. Evidence is usable for at most 36 seconds by both block time and monotonic sample age, and at most three blocks behind the final reference. Same-height hash conflicts, future references and stale reference blocks are refused. Slow responses cannot publish a fresh sample against an old block. Failures preserve the last successful sample's original age, with a fixed public error; they do not crash execution or block exit recovery.

Competitor evidence reuses pending/mined transaction objects already arriving through the configured signer feed. It adds **no** transaction/receipt lookups, scans or backfills. Eligibility uses the existing decoder: official SwapRouter02, Phase 2 f564/fETH, fee 500 or 10000, direct `exactInputSingle` or one supported deadline multicall, zero native value, recipient equal to signer, positive input/output minimum and zero sqrt-price limit. Unsupported vault calls, other routers, nested/multiple calls, other pairs and unconfigured signers provide no bid evidence. Pool-log `sender` or router attribution is never used to identify competitors. Competitor activity still invalidates modeled candidates under the existing engine rules, including unsupported transactions.

The cache keeps at most **256 signer/nonce observations** and **512 first-seen hash records**, each expiring after **60 seconds**. One current bid exists per signer/nonce. A new hash replaces the previous bid, including an unsupported/malformed replacement which clears its eligibility. Delayed duplicate pending hashes cannot restore superseded bids. An observed mined original can consume the nonce even if a replacement was seen. Pending deadlines expire bids sooner; mined bids must be within five blocks of the decision reference. Removed notifications, recovery and invalid snapshots clear cached observations and invalidate in-flight refresh results. Memory bounds can evict evidence sooner.

For EIP-1559 transactions, the estimate is `max(0, min(maxPriorityFeePerGas, maxFeePerGas - reference_base_fee))`. For legacy/type-1 transactions, it is `max(0, gasPrice - reference_base_fee)`. The largest eligible current estimate is selected. `maxFeePerGas` is **never** treated as the tip. These estimates use our decision's base fee; even a mined notification's bid fields are not its confirmed execution price. Already-fetched receipts matching the reconciled snapshot can enrich the block/index provenance without extra RPC calls.

Fallback is deterministic:

- Missing competitor evidence: use fresh network evidence and the baseline.
- Missing, zero, invalid or stale network evidence: use the baseline and any eligible competitor estimate, with the normal **fresh final base-fee check**.
- Both missing: use the baseline and dynamic base-fee headroom, subject to ceilings and affordability.
- Fresh base fee unavailable: fail before signing. Cached tips cannot substitute for chain freshness.

Feed visibility is limited to configured signers and what this provider sees. It does not reveal private order flow, unknown wallets, unsupported contract routes, original submission times or all replacements. A high historical percentile is not a prediction of the next proposer/builder's ordering.

## Measurements and submission path

`logs/events/execution_<mode>.json` retains up to 200 result rows plus any unresolved durable intent. `state.json` includes the latest attempt and recent results; rejected attempts also carry diagnostics in the existing rotating `decisions.jsonl`. The dashboard remains file-only and adds a compact **Latest fee and submission evidence** section inside execution results. Older rows and evidence that was never observed remain unavailable, not zero.

Each decision records policy/action, decision wall time, reference block/hash/time/base fee, baseline and multipliers, network and competitor sources/ages, desired and selected fields, ceilings, fallback reasons and whether the selected tip exceeds the observed **estimate**. `ordering_guaranteed` is always false. The raw gas estimate and final buffered gas limit are retained.

Timing records include first feed observation and its pending/mined source when available, validation start/completion, broadcast start and return, plus monotonic validation, first-seen-to-broadcast, validation-to-broadcast and broadcast-response durations in milliseconds. Validation timing starts before exact entry/cycle/exit preparation. The interval after validation includes durable intent recording and signing. Broadcast duration includes transport pacing and identical-byte failover. Provider acceptance is separate from inclusion; a timeout/error can leave acceptance unknown. Wall-clock fields report timestamps only; local durations use monotonic time. A crash can leave post-intent timing unavailable. On caught uncertain sends the service attempts to retain timing before halting; the original intent already contains the frozen fees and nonce.

Confirmed receipts record inclusion block/index/time, gas used, effective gas price, and effective priority (`effectiveGasPrice - inclusion_base_fee`) when derivable. Reverts retain gas and available block metadata too. Target/selected-competitor block and index differences are reported only from known evidence, with provenance; a different block hash at the same height cannot establish order. Positive deltas mean our transaction came later. Mined-feed relationships are labeled unverified; a matching target/competitor receipt is labeled `receipt_matching_snapshot`. The evidence is a snapshot of what was known during accounting; the service does not backfill later target/competitor outcomes while waiting for its own receipt. Reorg checks still govern the retained execution accounting.

No synchronous fee-history or suggested-priority query is added to an attempt. The final entry/cycle snapshot-check header supplies the fee reference; exits use their final latest header after contract simulation. The separate fee-only latest-block read is removed for entries/cycles. The shared sender now fills nonce and chain ID only when missing; Phase 2 supplies both after its existing nonce and Sepolia checks. It preserves the exact selected fee fields and applies the unchanged gas buffer once. All exact quotes, simulations, target nonce/deadline checks, final controls/feed checks, owner/vault limits, sender lock, pre-broadcast intent and receipt/reorg checks remain in place.

An offline comparison executed the pre-change `perform`/`send_tx` from commit `3bffd32` and the new implementations against the same counting fake RPC, real Web3 contract transaction builder, fake signer and synthetic successful cycle receipt:

| Common cycle submission stage | Before | After |
|---|---:|---:|
| `eth_chainId` | 3 | 1 |
| `eth_getBlockByNumber` | 3 | 2 |
| Operator `eth_getTransactionCount` | 2 | 1 |
| `eth_estimateGas` | 1 | 1 |
| `eth_getBalance` | 1 | 1 |
| Fake broadcast invocation | 1 | 1 |
| Fake receipt-wait invocation | 1 | 1 |

The remaining block reads are final freshness and receipt validation. Phase 2 now supplies its already-checked chain ID to the transaction builder as well as the sender. Entry additionally checks the target nonce. Outer strategy quoting/validation reads are outside these counts; network evidence is prepared separately. The maintained fake-signer regressions in `tests/test_event_fees.py` verify final fees, gas buffer, block/nonce counts and unchanged safety gates across all four actions. Virtual-clock tests verify timing arithmetic; these are not live latency benchmarks. Receipt waiting and real RPC latency have not been measured for this feature.

No replacement, post-submission fee bump, cancellation, relay, bundle or parallel broadcast is implemented. Transport failover continues with identical signed bytes. Unknown submission outcomes halt for manual reconciliation and cannot create a new fee decision/send for that intent.
