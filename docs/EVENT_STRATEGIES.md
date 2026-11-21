# Phase 2 front-runs, backruns and cross-pool arbitrage

## Objective and current scope

Maximize competition portfolio return measured in f564. The strategies share a transaction monitor, decoder, two-pool state simulator, and execution service:

1. **Backrun:** prepare an atomic f564 → fETH → f564 cycle against the predicted state after a market-bot transaction. After confirmed successful inclusion, refresh both pools and revalidate before the configured execution adapter can submit.
2. **Front-run:** evaluate buying fETH before a market-bot buy and selling after it. Phase2TradingBot holds the recorded entry until the target nonce is mined, the exit timer expires, or an exit is requested. Selling before a market-bot sell requires separately managed fETH inventory and remains unsupported.
3. **Independent cross-pool scan:** optionally evaluate both cycle directions on each new snapshot, using the same contract and balance without waiting for a target transaction.

All connected strategies share one Phase2TradingBot. On startup the bot pauses entries in Simulation with a vault, or Paper without one. Live inventory resumes exit management. Backruns default on, front-runs and independent scanning default off. Simulation uses quotes and eth_call. Live can submit transactions after the operator enables it.

## Event flow

```text
Alchemy pending/mined notifications
  → normalize signer, hash, nonce, calldata, receipt outcome
  → decode supported market-bot swaps
  → apply each proposed trade to a copy of the two-pool state
  → front-run candidate / backrun preparation
  → replace, expire, invalidate, or revalidate candidate
  → paper journal / exact simulation / configured live execution
  → public status and execution ledger → dashboard
```

The pending monitor follows transaction signers, including the named market bot and recurring competitors. Filtering only transactions sent to pool addresses misses router and vault calls. Seeing a transaction in Alchemy's feed establishes provider visibility, not its original submission endpoint.

## Ordering and execution boundaries

Pending preparation does not establish transaction order. The default backrun path waits for a successful receipt and a fresh state snapshot after inclusion. A mined notification alone is insufficient: a transaction may have reverted. A lower priority fee is not an ordering guarantee.

Ordered same-block backruns cannot be guaranteed. The atomic executor checks the whole-cycle f564 profit but cannot insert another account's transaction between its own legs. Front-running uses separate entry and exit transactions and cannot guarantee ordering or profit. The target can be replaced, revert, or be mined before our entry; the service exits recorded inventory even in those cases.

With `EVENT_PENDING_BACKRUN` enabled, a prepared backrun plan may additionally offer the atomic cycle while the target is still pending, using `backrun_pending:` attempt keys. No exact quote or simulation exists at that moment, so the integer rung nearest the paper optimum is submitted with `minQuoteOut = amount + min_profit` and snapshot-derived price limits; the contract's on-chain profit floor is the guard and rolls back both legs when the target is replaced, reverts, or the state moves, costing gas only. Freshness is a bounded window, not strict pinned==latest equality: the head is expected to advance while the target sits pending, so the submission requires a fresh head, the pinned snapshot within two blocks behind it, and the pinned block still canonical. Gas is the configured ceiling without an estimate, and every send guard (chain, caps, affordability, deadline) still applies. The pending cycle carries its OWN tip (`EVENT_PENDING_PRIORITY_FEE_WEI`, default 0): the market bot's targets pay 0 gwei and builders order by tip descending, so any positive tip sorts the cycle BEFORE the target (observed block 12178067: a 10 gwei cycle at index 1 reverted while the 0-gwei target sat at index 135), while the 0 tip joins the target's fee group and our later broadcast places us right behind it by arrival order; the max fee stays at the decided ceiling and every other send path keeps `EVENT_PRIORITY_FEE_WEI`. Inclusion after the target in the same block remains unguaranteed; the receipt-confirmed `backrun:` path is unchanged and still runs when the pending offer did not happen or failed pre-send.

Cycle execution creates one attempt-local quote context at the candidate's block/hash. Size rungs reuse the pool prices and route identity from that context, with fresh canonical validation before using the result. Owner caps and the profit floor are checked for the attempt, not cached across attempts. The whole-cycle `eth_call` uses the quote block; the send path performs one current-state gas estimate and retains final feed/control/target/snapshot checks. Moving state can still reject a candidate before broadcast.

For atomic cycles, the quote minimum is the greater of the configured slippage minimum and `amount_in + min_profit`. Exact quoted output must reach that minimum. This tightens tolerated slippage when the profit floor binds, so a small profitable cycle need not be rejected merely because spending the entire configured slippage allowance would miss the floor. It does not change single-swap risk or guarantee front-run round-trip profit.

The execution adapter preserves Sepolia-only checks, owner limits, whole-cycle simulation, `SWAP_GAS_BUFFER=2.0`, and before-send skip versus post-send halt classifications. It never rebuilds and retries an uncertain send. Actual live submission is not authorized by this implementation work.

## Competitive fee preparation

All execution paths share the opt-in [Phase 2 fee policy](EVENT_FEES.md). Default `fixed` preserves `EVENT_PRIORITY_FEE_WEI` / `EVENT_MAX_FEE_WEI`. `competitive` uses a 20 gwei baseline, cached eight-block network p90 evidence and supported configured-competitor effective-tip estimates, with integer 1.25×/1.5× multipliers and separate 100/200 gwei ceilings. Dynamic maximum headroom is `2 * current_base + selected_tip`, bounded by the total ceiling. Missing evidence falls back deterministically; a fresh base-fee check and affordability remain mandatory. Exits use the same policy independently of entry pause/feed restrictions.

Fee evidence never revalidates an invalidated candidate. Unsupported competitor transactions still trigger the engine's existing invalidation rules even when they cannot supply a fee sample. Public attempts distinguish desired and selected bids, evidence ages/sources, gas estimate/buffer, monotonic submission durations, provider acceptance and receipt inclusion/order. The worker makes no synchronous fee-history request during submission. The policy implements no replacement, cancellation, relay, bundle or parallel broadcast; identical-byte failover and unknown-send halts are preserved. Higher tips cannot guarantee ordering.

## Pool state and simulation

Both Phase 2 pools have token0=f564 and token1=fETH, with 18 decimals. Each snapshot carries chain ID, block number/hash, pool/token identity, fee, and the complete initialized-tick liquidity layout. Model swaps on independent copies; never change a canonical snapshot while evaluating a hypothetical sequence.

Candidate sizing searches a bounded f564 range and includes both fees and liquidity-dependent impact. Model estimates use the existing offline `v3pool` math; raw on-chain amounts and decoded limits remain integers. Floating-point simulation is research evidence, not a replacement for exact final QuoterV2 / whole-cycle eth_call validation.

Unsupported calldata, token pairs, ambiguous multicalls, nonzero pool price limits, expired deadlines, insufficient liquidity, and target minimum-output failures fail closed in the first implementation. Expanding support requires tests for the added semantics.

## Candidate lifecycle

`PaperEngine` owns the lifecycle in both replay and the connected service: expiry, retained candidates, pending receipt work, deduplication and nonce settlement. `Service` supplies snapshots, receipts and account nonce reads at the snapshot block through the engine's methods. It never edits the engine's collections. Receipt polling uses copies from `pending_transactions()`; a superseded hash remains tracked because the original may still be mined. Terminal candidates stop polling, expired candidates stay available for 120 seconds, and nonce-history compaction invalidates predictions before dropping sequencing history. Reorgs, invalid snapshots and service recovery clear pending receipt work and nonce settlement together.

- Pending events deduplicate by hash. A different hash with the same signer and nonce supersedes the previous candidate, including a replacement that is not a supported swap.
- Market-bot transactions generate hypothetical preparations. Competitor activity marks preparations stale; it does not prove execution or change canonical pool state.
- Inclusion must provide receipt success and a fresh matching snapshot. A newer snapshot additionally requires canonical checks of both its block hash and the older receipt's block hash; a receipt alone does not prove the target survived a reorg. Recompute the actual post-trade opportunity rather than reusing the prediction.
- Removed/reorg notifications invalidate affected candidates. Expiry and snapshot age prevent an old observation becoming a fresh trade signal.
- Front-run paper estimates do not become backrun-ready instructions. They describe an assumed sequence and cannot claim actual inventory or profit.
- Decisions record reasons, assumptions, trigger hash, nonce, snapshot block/hash, route, input, predicted f564 gain and validation state. No endpoint, key, or provider exception body belongs in a journal.

## Evaluation

Compare f564 portfolio contribution, native ETH gas budget, latency, stale-candidate frequency, target reverts, and execution success. Do not sum independent historical opportunities into a portfolio backtest; our trades alter future states. Pending-to-mined notification lead is not guaranteed time remaining before inclusion.

Historical evidence: the seven-day study reconstructed 410 swaps; a live observation captured one market-bot pending transaction with 1.228 seconds of notification lead and four competitor transactions with 5.182–8.390 seconds. A competitor captured approximately 46.75 f564 in the block after the market bot. These samples motivate the design but do not establish a reliable front-running edge.

## Implementation milestones

1. Shared offline decoder and two-pool simulator; backrun preparation and front-run paper evaluation.
2. Event lifecycle with replacement, competitor invalidation, receipt gating, freshness and reorg handling; deterministic offline replay and a decision journal.
3. Connected feed and coherent snapshot adapter: implemented and tested with fake transports and recorded snapshots; live latency observation remains to be run.
4. Execution adapters and dashboard: implemented and tested with in-memory chains. Deployment, funding, operational review and explicitly authorized bounded live trials remain outstanding.

## Offline planner and replay

The first two milestones now have an offline implementation:

| File | Responsibility |
|---|---|
| `event_market.py` | Offline SwapRouter02 decoder, official Phase 2 snapshot validation, copied-state target simulation |
| `backrun.py` | Bounded two-direction cross-pool sizing and hypothetical post-target preparation |
| `frontrun.py` | Paper buy-before-buy / sell-after-target evaluation across entry and exit pools |
| `event_engine.py` | Candidate lifecycle, replacements, competitor invalidation, expiry, receipt and snapshot gating |
| `event_replay.py` | Offline JSONL input and append-order paper decision journal in a new output file |
| `tests/test_event_strategies.py` | Recorded-data and adversarial lifecycle regressions, plus CLI integration |

Importing these offline modules never starts a monitor. `pending.py` remains a standalone research recorder. `event_service.py` is the connected runtime described below; it inserts coherent snapshots and verified receipts.

### Supported transaction semantics

The decoder accepts an explicit Sepolia transaction signed by a configured market bot, sent to the official SwapRouter02, with zero native value. It supports direct `exactInputSingle` or a deadline-bearing `multicall(uint256,bytes[])` containing exactly one `exactInputSingle`. The pair must be Phase 2 f564/fETH, fee 500 or 10000, recipient the signer, positive input and output minimum, and zero sqrt price limit. Contract-balance sentinels, exact-output swaps, arbitrary vault calls, nested/multiple calls and partial-fill limits are refused.

Competitor transactions do not need decoded calldata to invalidate a preparation. The default market-bot allowlist contains `0x6011139d5f16689bde1d245c6247b088f9582026`; additional market-bot signers are explicit configuration, not inferred from frequency. The competitor allowlist is also explicit. An unresolved lower nonce from a market bot blocks independently simulating its higher nonce. This includes an unsupported earlier transaction.

Snapshot validation requires the two official pools, matching block identity, reversed Phase 2 token order, 18 decimals, consistent active liquidity and price, and a balanced initialized-tick layout. States ambiguous at a tick boundary fail closed. The model does not verify the target's balance or allowance; target success is a conditional assumption until a receipt proves otherwise.

### Offline replay

```bash
uv run python event_replay.py \
  --events research/my-events.jsonl \
  --output research/my-paper-decisions.jsonl \
  --quote-cap 1000 --base-cap 10 --min-profit 1 \
  --competitor 0xd067640b2e032b1a878bd87b4d85cb5a87ef64ef \
  --competitor 0x21b1a1e9f20377f4cbf6d874cfcf3df37699c7b7

uv run pytest tests/test_event_strategies.py -q
```

CLI token amounts are decimal strings in human units; they are converted exactly to raw integer limits. Defaults are a **1,000 f564 input cap**, **10 fETH intermediate cap**, and **1 f564 minimum modeled profit before gas**. These are paper-research bounds, not vault permissions or recommended live settings. The CLI refuses to overwrite an existing output file. It does not load `.env` or access the network.

Input is one JSON object per line, ordered by nondecreasing `now` Unix seconds. Instead of `now`, recorded notifications may supply an ISO `received_utc` timestamp. Supported events:

- `snapshot`: `{"kind":"snapshot","now":...,"snapshot":...}`. Snapshot format matches the full tick scan: `chain_id`, `block`, `block_hash`, `timestamp`, and two `profiles`, each with matching `block`, pool/token identities, fee, decimals, raw sqrt price, tick, active liquidity and ticks.
- `pending`: `{"kind":"pending","now":...,"transaction":...}` with the public RPC transaction object.
- `mined`: the same transaction envelope, plus `receipt` when available. `removed:true` invalidates state. Receipt must identify the same hash/signer and have status 1 before a backrun is revalidated.
- `reorg`: explicit canonical-chain invalidation; the next prediction requires a new snapshot.
- `tick`: advances the clock to expire preparations without a new transaction.

Before processing a successful mined event, insert a fresh **end-of-inclusion-block** snapshot with the same block number/hash as the receipt. This first implementation deliberately requires exact block matching rather than assuming later-block ancestry. It computes the opportunity from that actual state; it never applies the target twice. If a competitor already consumed the spread in that block, the result can be `no_opportunity`.

Core statuses are `prepared`, `stale`, `superseded`, `expired`, `awaiting_snapshot`, `reverted`, `invalidated`, `no_opportunity`, and `paper_revalidated`. Every output remains paper-only. Even `paper_revalidated` requires exact final quoting, whole-cycle eth_call, current owner limits and execution safeguards before any future send.

Front-run paper output also records `profit_without_target_f564` and `target_contribution_f564`, separating the assumed target's effect from an existing spread. Its assumed order is our buy → target buy → our sell, with no intervening competitor. Paper output creates no actual position. Live fills are accounted separately by the execution adapter. Front-running a market-bot sell and the enter-before-target/exit-after-competitor strategy remain future work.

## Running the connected service

For exact constructor inputs, deployment, owner permissions, registration and funding, follow [Deploy one Phase 2 contract](EVENT_CONTRACT_SETUP.md) before configuring simulation/live execution. Paper analysis needs no deployed contract.

From the repository root, after configuring `.env`:

```bash
uv sync
uv run python main.py --seconds 1200
```

The service automatically loads the repository's `.env`; exported values take precedence. For exported configuration only:

```bash
uv run python event_service.py --no-env
```

`--seconds` is optional. Ctrl-C/SIGTERM finishes the current operation and stops; an open position remains in the ledger and needs the live service running to exit. `make events` and the systemd unit use the same automatic file loading. The old `--load-env` flag is accepted for compatibility but is unnecessary. Imports, `--help` and replay do not load `.env`; tests disable real env-file loading.

The service requires `RPC_URL` for HTTP reads and an Alchemy **Sepolia** endpoint. It selects `ALCHEMY_WS_URL`, then `RPC_URL`, then `RPC_FALLBACK_URLS`, accepting only `eth-sepolia.g.alchemy.com/v2/...` HTTPS/WSS endpoints and converting HTTPS to WSS internally. Credentials never enter status or journals. `MAX_RPC_PER_SECOND` and HTTP fallback behavior are shared with the legacy runner. The event service uses the fixed official Phase 2 pools/router/quoter and checks tokens, decimals and factory identity.

Startup first validates `logs/events/snapshot.json`, then falls back to the saved `research/2026-11-17-phase2-arbitrage/snapshot.json` if the runtime cache is missing or invalid. Both require matching chain/pool/token identities and a canonical block hash/time. The saved layout is brought forward by replaying ordered Mint/Burn/Swap logs in bounded ranges, checking the replayed price/tick and active liquidity against pinned chain reads, and rechecking the destination block hash before caching the result. The research file stays unchanged.

**Scan Pool Liquidity** requests one full scan. With an unusable layout, the service reports `awaiting_scan`, withholds entries and continues managing recorded exits. A scan reads every bitmap word and initialized tick for both pools at one block, validates exact active liquidity, then rechecks the block hash. Scans defer while inventory is open. The service saves the acknowledgement before scanning and requires another click after failure. Disconnects and queue overflow invalidate pending work. Pending messages older than two seconds when processed are discarded. HTTP receipt checks recover missed mined notifications for prepared candidates.

Scan acknowledgements live in `logs/events/scan.json` and migrate older acknowledgements from execution ledgers, so changing paper/simulate/live mode cannot repeat a request. Catch-up reduces rejected log ranges to accommodate provider limits. Subscription-buffered messages retain their actual arrival time; reconnects discard the old queue. Read/validation failures clear predictions, report exception type/source location and back off up to 60 seconds. Unexpected bugs exit nonzero instead of looping indefinitely. Provider response bodies are suppressed. Simulation checks the final entry controls, feed generation, snapshot and target nonce just as live execution does. Any exception during broadcast requires manual reconciliation, including “already known” and “nonce too low.”

Run the existing dashboard in another terminal:

```bash
uv run python dashboard.py
```

Open `http://127.0.0.1:8765` (or the existing SSH tunnel). The Phase 2 overview shows active mode, entry status, open inventory, pool liquidity and session RPC totals. **Simulation** and **Live (Sepolia)** request a mode change. Wait for the active mode, then click **Start Trading**. **Stop Trading** leaves receipt checks and exits running. **Scan Pool Liquidity** requests a one-time scan of both pools. Expand the secondary sections for actors, limits, candidates and results. The dashboard writes commands to `logs/control.json`. Bot startup consumes old mode and start request numbers so an earlier session cannot authorize new entries.

## Configuration and execution modes

### Offline request measurements

The counting transport serves both saved pool layouts through the installed Web3 middleware. The fixture uses one quiet block of catch-up, the complete bitmap range of each pool, no retries and no rate cap. The resulting tick lists match every saved tick, gross liquidity and net liquidity value.

| HTTP workload | RPC items before | RPC items after | HTTP attempts before | HTTP attempts after |
|---|---:|---:|---:|---:|
| Startup market checks and saved snapshot catch-up | 77 | 56 | 77 | 56 |
| Next quiet block | 21 | 17 | 21 | 17 |
| Refresh the same reconciled block | 15 | 1 | 15 | 1 |
| Manual scan of both pools | 2,294 | 778 | 2,294 | 65 |

The earlier scan scheduled 152 seconds of fixed sleeps alone. The new scan took about 0.2 seconds with the in-process fake HTTP transport and no sleeps. On Sepolia, network latency and `MAX_RPC_PER_SECOND` determine elapsed time. Scan batches contain up to 20 bitmap or tick calls, reduced to fit the configured per-provider rate. Rate-limited providers pace batches and individual attempts.

Startup checks each shared token's decimals and the router and quoter factories once. Each pool still gets its own token, fee and factory checks. Catch-up queries all three event topics together for each pool and range. A repeat refresh verifies the latest hash before reusing a previously reconciled layout. A scan reuses its own pinned slot and liquidity reads. The final block-hash check and exact active-liquidity comparison guard publication.

The table isolates market and snapshot HTTP work. Feed setup adds four WebSocket requests per connection. The service also checks one account nonce per watched market bot at each new snapshot block. Live receipt checks run once per maintenance pass, including when inventory is open. Vault checks, final quotes, nonce and receipt polling, sends and transport retries depend on execution and feed activity. The dashboard includes them in its session total and counts each batch item individually. Its refreshes read local files and generate zero chain requests.

Reproduce the counts with `uv run pytest -qs tests/test_phase2_rpc.py`.

### Operator settings

| Environment variable | Default | Meaning |
|---|---|---|
| `EVENT_EXECUTION_MODE` | optional | Compatibility input (`paper`, `simulate`, `live`). A fresh process pauses in Simulation with a vault or Paper without one. Live inventory resumes exit management. |
| `EVENT_QUOTE_CAP` | `1000` | Maximum f564 per entry/cycle |
| `EVENT_BASE_CAP` | `10` | Maximum modeled/quoted fETH entry or cycle intermediate |
| `EVENT_MIN_PROFIT` | `1` | f564 modeled/exact cycle floor before native ETH gas |
| `EVENT_SLIPPAGE_BPS` | `20` | Minimum-output allowance, 0–500 bps |
| `EVENT_MOVE_BPS` | `100` | Single-swap and atomic cycle pool price limits, 1–500 bps |
| `EVENT_PENDING_BACKRUN` | `true` | Offer the atomic backrun cycle while the target is still pending (no exact quote; on-chain floor is the guard, failed cycles cost gas). Set `0`/`false` to restrict backruns to the receipt-confirmed path |
| `EVENT_PENDING_PRIORITY_FEE_WEI` | `0` | Tip used ONLY by the pending backrun cycle. The market bot's targets pay 0 gwei and builders order by tip descending, so 0 (default) joins the target's fee group and arrival order places the cycle right behind it; any positive tip sorts it BEFORE the target (pre-target revert). Max fee stays `EVENT_MAX_FEE_WEI`; other paths keep `EVENT_PRIORITY_FEE_WEI` |
| `EVENT_EXIT_SECONDS` | `60` | Front-run inventory exit timer, 5–300 seconds |
| `EVENT_COOLDOWN_SECONDS` | `10` | Delay after execution attempts/failures, 1–300 seconds |
| `EVENT_OBSERVER_SECONDS` | `15` | Diagnostic-worker cadence, 5–600 seconds; slower saves compute units |
| `MAX_COMPUTE_UNITS_PER_SECOND` | `0` | Per-provider compute-unit pacing (Alchemy free tier ceiling: 300 CU/s; try 250), 0 = off |
| `EVENT_MAX_GAS` | `1500000` | Maximum gas limit including the 2× estimate buffer |
| `EVENT_PRIORITY_FEE_WEI` | `2000000000` | Fixed-mode exact priority fee, integer wei/gas |
| `EVENT_MAX_FEE_WEI` | `20000000000` | Fixed-mode exact maximum fee, integer wei/gas |
| `EVENT_FEE_POLICY` | `fixed` | Opt-in `competitive`; [all six policy controls](EVENT_FEES.md#configuration) |
| `BOT_CONTRACT_ADDRESS` | empty | One Phase2TradingBot holding all strategy funds |
| `OWNER_ADDRESS` | empty | Enrolled owner, verified against that contract |
| `EVENT_MARKET_BOTS` | named market bot | Comma-separated signal signer allowlist |
| `EVENT_COMPETITORS` | two observed recurring signers | Comma-separated invalidating signer allowlist; supported official-pool transactions also supply competitive fee evidence |

These defaults are bounded research settings, not demonstrated profitable live parameters. A higher priority fee does not guarantee a front-run, and public backruns after receipt confirmation can lose the race to competitors.

**Paper** evaluates models and records candidates, without loading a key or claiming fills. **Simulate** additionally validates enabled paths using exact quotes, contract calls and gas estimates; it creates no position because no entry happened. Execution obeys the event pause/module controls. Simulation/live startup requires the unified contract, verifies its version, owner, operator, router, tokens and pools, and refuses obsolete nonempty split-vault variables.

**Live** additionally requires `DRY_RUN=false`, an operator `PRIVATE_KEY`, the matching enrolled owner, and the deployed/funded Phase2TradingBot. The owner's key is never used. The operator must hold native ETH for the configured maximum transaction cost (0.03 ETH at defaults). Owner caps must allow the full recorded exit amount. Single entries cap received fETH accordingly. One service serializes all strategies on the shared balance and closes a recorded front-run position before opening another entry or cycle.

Follow the [contract walkthrough](EVENT_CONTRACT_SETUP.md) to prepare the deployed vault. The runtime ABI ships in `abis/phase2_trading_bot.json`; the server needs no build artifact or compiler to run it. Start Phase 2 with `main.py`. For existing polling positions, use `legacy_main.py` and the legacy single-swap CLI.

Optional `event_arbitrage` in `control.json` evaluates the current cross-pool spread once per snapshot hash, independent of a target transaction. It obeys entry pause, feed readiness, cooldown and open-position gating. Exact quote/simulation validation and atomic execution are shared with backruns, with distinct `arbitrage:` attempt keys. Receipt-triggered backruns use `backrun:` keys; pending-target offers use `backrun_pending:` keys. The contract enforces a positive f564 gain for each cycle before native ETH gas; separate front-run swaps have no round-trip profit guarantee.

The adapter searches a bounded grid of integer configuration-derived sizes (twelve rungs from 1/64 of the cap up to the cap itself, adjacent rungs within 1.5x except the smallest 2x step, so a narrow low-size optimum is bracketed). Floating-point optimizer sizes never become calldata; the paper optimum only orders which rungs are quoted first. Time-critical actions quote fast: a front-run entry exact-quotes only the three rungs whose modeled profit ranks highest (the model pass itself is free), and both cycle actions quote the three-rung bracket around the paper optimum first, sending a profitable bracket quote immediately; the remaining rungs are quoted only when the bracket found nothing, which costs nothing in a quiet market. Backruns require exact profitable quotes, owner limits and whole-cycle eth_call. Front-runs require a still-pending target nonce, fresh snapshot, target minimum-output modeling, exact entry quote and single-swap eth_call. The future target/exit sequence remains conditional; the service does not claim an exact simulation of another sender's future ordering. Quotes and submission involve sequential RPC calls, so latency must be measured against real pending lead before enabling live entries.

## Inventory, failures and restart behavior

- Event entries default to paused; backruns default enabled, front-runs and independent cross-pool scanning disabled. Controls are independent from legacy runner controls. On-chain contract pause blocks exits too; the dashboard entry pause does not.
- Live entry fills persist raw receipt-derived fETH and actual f564 input. Exits sell exactly that recorded fETH, preserving deposited inventory. A restart restores the position. An exit follows target nonce inclusion (including replacement/revert), timeout, or the event exit button. Pause and module-disable controls never block exits. There is no independent price stop-loss in this service.
- A successful entry does not impose a cooldown on its exit. Read failures or confirmed failed attempts wait the configured cooldown. Failed exits remain open for a later definite attempt; unknown outcomes halt.
- A local `logs/operator.lock` excludes simultaneous legacy/event live senders. Service switching also refuses the other service's open inventory or unresolved intent. `logs/event_service.lock` excludes multiple event service instances. These locks do not coordinate other computers or manual transactions.
- Mode-specific execution ledgers bind pool/token/vault/owner identity. A live open position or intent prevents switching to paper/simulate. Never clear a ledger to bypass identity checks.
- Intent, operator and nonce are saved before broadcast, with filesystem flushes. Unknown send/receipt outcomes or accounting failures write `logs/events/HALTED` and exit zero. Restart refuses an unfinished intent. Reconcile on-chain receipt and inventory manually before correcting the ledger and clearing the marker; no automatic rebroadcast or recovery trade occurs.
- Startup/recovery validates every retained mined trade block before applying the 64-block steady-state cutoff. The entry backing an open position is checked on every maintenance pass, including if its trade row has been trimmed. A missing or changed canonical block halts before an exit; read failures defer execution and keep recovery pending. Native gas is recorded from successful and reverted receipts; older records without gas metadata leave the retained gas total unavailable.
- Decision journals rotate at about 5 MB and public status contains bounded recent history. A stopped/stale status remains visible in the dashboard.

For Ubuntu use `deploy/trading-bot.service` and the separate dashboard service. Stop and disable the old event unit before upgrading. [Service handoff](DEPLOY.md#phase-2-on-ubuntu)

Validation uses fake feeds, recorded snapshots and in-memory chains. Live provider compatibility, latency, competition registration, and execution ordering still require a separately run paper observation and bounded live trial after deployment.

See [EVENT_CONTRACT_SETUP.md](EVENT_CONTRACT_SETUP.md) for deployment and [the live observation report](../research/2026-11-17-phase2-flow/pending_watch_report.md) for measured timing.
