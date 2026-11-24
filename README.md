# Uniswap V3 Sepolia trading bot

**Phase 3 is underway.** Run `uv run python phase3_monitor.py` for the current single-pool market. It tracks every confirmed pool, token and credit-facility event, including previously unseen competitors. Read the [Phase 3 investigation](research/2026-11-24-phase3/README.md) and [monitor runbook](docs/DEPLOY.md#phase-3-monitor). The trading commands below target Phase 2.

The [Phase 3 vault walkthrough](docs/PHASE3_VAULT.md) covers deployment, owner registration, funding, credit limits, and operator calls. Borrowing starts disabled. Phase 3 has its own verified address record and command line.

`phase3_auto.py --live` watches for eligible borrowers and executes atomic liquidation-and-close trades under the vault owner's per-token permissions. See [automatic liquidation setup](docs/PHASE3_VAULT.md#automatic-liquidations).

A Python trading bot for **Sepolia (testnet only)**. The Phase 2 event service monitors two Uniswap V3 pools and trades through one `Phase2TradingBot` contract you own. The competition objective is portfolio return measured in f564. It can run on your laptop or under systemd on Ubuntu 24.04.

> Testnet only. Nothing here has the safeguards real money would need. The strategies in `strategy.py` are placeholders, not trading advice.

## Start here

**Follow [the Phase 2 operator walkthrough](docs/EVENT_CONTRACT_SETUP.md): paper observation → one contract → simulation → live trading.** It is the main setup guide, including wallets, limits, verification, funding and owner actions. [GETTING_STARTED](docs/GETTING_STARTED.md) points to that workflow; [DEPLOY](docs/DEPLOY.md) covers running it on a server.

Already running the legacy `main.py` bot? Read [the migration notes](docs/PHASE2.md) and reconcile its positions before switching. Its `TradingBot` contract has a different interface; deploying it does not set up the event service.

Start the Phase 2 bot with `uv run python main.py` or `make run`. In a second terminal run `make dashboard` and open <http://127.0.0.1:8765>. With a configured vault the bot starts in **Simulation**, otherwise in **Paper**. The primary row has exactly two buttons: **Mode: Simulation / Mode: Live (Sepolia)** requests the other mode; **Start Trading / Stop Trading** toggles permission to open new trades. A mode change pauses entries, and the active label changes only after runtime acknowledgement. Live still requires preconfigured `DRY_RUN=false`, signer/vault validation, and a separate fresh Start request. Stop preserves monitoring, receipt checks and inventory exits, and remains available with stale status. An existing live position starts in exit recovery with entries paused. See [the service handoff](docs/DEPLOY.md#phase-2-on-ubuntu) before upgrading a running installation.

What you get:

- one shared contract balance for front-run entries/exits, atomic backruns, and optional cross-pool arbitrage;
- paper, simulation and live modes, with entries paused by default;
- a Phase 2 dashboard with entry-blocking reasons, feed/snapshot freshness, strategy switches, recent evaluations and execution results;
- two vertically stacked interactive charts in **f564 per fETH**, with synchronized cursors, zoom/pan, execution annotations and actor markers;
- actual contract holdings and pinned spot valuation, separate position accounting, retained live trading P/L and native ETH gas costs. Missing baselines remain unavailable. Legacy polling recovery has a separate page at `/legacy`.

## How it fits together

Phase 2 fees default to **`EVENT_FEE_POLICY=fixed`**, preserving the existing 2 gwei priority / 20 gwei maximum fields. Opt-in `competitive` starts at 20 gwei, adjusts from cached recent network and supported competitor bids, and chooses dynamic base-fee headroom under separate 100/200 gwei ceilings. Every entry, cycle and exit shares it. The dashboard's execution details show selected fees, fallback/ceiling reasons, submission timing and available inclusion evidence. Higher tips cannot guarantee ordering. See [the fee policy reference](docs/EVENT_FEES.md) for the six controls, formulas, visibility limits and offline request measurements. Installing this update does not opt in or enable trading.

If a provider rejects the `eth_getLogs` range at startup, catch-up can reduce it. JSON-RPC quota/authentication failures use backoff and preserve the range; diagnostics retain HTTP status codes without provider bodies. [Phase 2 troubleshooting](docs/GETTING_STARTED.md#phase-2-troubleshooting)

**One Phase 2 contract:** [docs/EVENT_CONTRACT_SETUP.md](docs/EVENT_CONTRACT_SETUP.md) walks through building, preparing and deploying `Phase2TradingBot`, registering/funding one address, and simulation-to-live setup. One `BOT_CONTRACT_ADDRESS` holds the shared balance for front-run entry/exit, receipt-triggered atomic backruns, and optional independent cross-pool arbitrage. Build offline with `uv run python phase2_contract.py build`. The Standard JSON includes inherited cycle logic and interface source dependencies; only Phase2TradingBot is deployed. The obsolete standalone arbitrage builder/artifacts have been removed. Preserve the exact build and plan for verifying any existing deployment.

The event service automatically loads project configuration. Exported variables take precedence. Use `main.py --no-env` for exported configuration only. Startup validates the runtime or research snapshot and catches up through pool logs. **Scan Pool Liquidity** requests a full scan of both pools. An invalid layout shows **Scan required**. Scan acknowledgements survive restarts and mode changes. The dashboard shows the snapshot block and time, and HTTP and WebSocket request counts for this process. [Event strategy reference](docs/EVENT_STRATEGIES.md)

**Running but no trades:** check the entry explanation first. A connected feed and `ready` snapshot do not grant entry permission. Every restart or mode switch requires a fresh **Start Trading** request after acknowledgement. Backruns need a prepared target and a profitable inclusion-block quote; front-runs and independent cross-pool arbitrage are off by default. Paper never executes; Simulation never broadcasts. Rejections include the failing local stage/reason without provider response bodies. Cycle validation shares one block-pinned quote context across size rungs, simulates at that block, and estimates gas once before the final freshness checks. An atomic cycle can tighten its slippage tolerance to preserve the configured profit floor; a quote below that floor is still rejected. These changes reduce validation work but do not guarantee inclusion or profit from separate front-run entry/exit transactions.

The event runtime avoids redundant Web3 chain-ID lookups for contract reads that do not declare a chain ID, while keeping explicit Sepolia checks and declared-chain validation. Snapshot catch-up queries both official pools' logs together. Final submission checks the swap deadline again after preparation; fewer reads do not guarantee inclusion ahead of competitors.

The event runtime records bounded prices in `logs/events/prices.jsonl` from reconciled snapshots and public swaps in `logs/events/activity.json` from already-fetched pool logs and confirmed receipts. A background diagnostic worker makes bounded, throttled balance and metadata reads at most once per 15 seconds; its HTTP waits do not block the execution loop. `portfolio.json` retains the last successful pinned holdings observation with its original timestamp on read failure. Dashboard requests are file-only, including every chart interaction and additional tab. Reconciled trades missing timestamps still contribute valid realized profit and gas totals; their date range is marked unavailable. Verified flat inventory displays “No open position.” There is no automatic backfill or liquidity scan. After installing this update, restart both processes through the normal operator handoff and reload the browser. Preserve all event history and execution files. [Dashboard operation and accounting coverage](docs/GETTING_STARTED.md#phase-2-dashboard-operation)

Developer verification: `make check` and `make test` run offline checks. Install isolated Chromium with `uv run playwright install chromium`, then run `make test-browser` for synthetic browser controls, charts, mobile layout and pagination. Browser tests never open the operator's browser or use real controls.

Recovery validates retained execution receipts before applying the 64-block age cutoff, and always checks the entry supporting an open position before exits. Rejected price history is withheld after snapshot invalidation, including when a later scan recovers at a higher block. Holdings diagnostics continue while awaiting a scan. Open-entry chart references require verified execution and activity; recorded positions remain available for reconciliation when verification fails.

Runtime failures report their exception type and source location without provider response bodies. Read failures clear pending predictions and back off; unexpected programming errors exit nonzero for systemd. Any broadcast exception requires reconciliation. Offline build/prepare/replay commands do not load `.env`; prepare and deploy validation never overwrite saved verification artifacts. Tests disable loading the operator's real environment file.

**Phase 2 flow research:** the [seven-day study](research/2026-11-17-phase2-flow/README.md) reconstructs both pools' liquidity and identifies recurring actors and historical arbitrage. Its read-only Alchemy observer, `uv run python research/2026-11-17-phase2-flow/pending.py --seconds 7200`, monitors all observed swap signers using an Alchemy Sepolia connection already exported in the process environment. It records pending/mined arrivals, never reads `.env`, and never sends trades.

**Atomic cycle implementation:** [docs/ARBITRAGE.md](docs/ARBITRAGE.md) describes the cycle code inherited by Phase2TradingBot and the read-only planner. A complete f564 → fETH → f564 cycle enforces profit or reverts, preserving existing fETH. Front-run single swaps have no atomic round-trip profit guarantee. Native ETH gas is accounted separately. The [Phase 2 liquidity study](research/2026-11-17-phase2-arbitrage/README.md) records both pools' full tick layouts and 18 same-block quotes; none was profitable at the observed block.

```
 owner wallet (MetaMask)         bot wallet (key in .env)           bot contract
 withdraws, sets limits   <---   pays gas, calls swap()   ---->    holds f564 / fETH,
 key never on the server         inside the owner's limits          swaps via SwapRouter02
```

- **Phase 2 contract/service:** `contracts/Phase2TradingBot.sol` and `event_service.py` execute single swaps and atomic cycles using one balance. `owner()` returns the enrolled wallet; only the owner can withdraw to itself or change limits/operator. The operator trades within the caps, fixed pools, deadlines and price/output limits.
- **Legacy polling runner** (`legacy_main.py`) manages positions in `logs/ledger.json` for the original `contracts/TradingBot.sol`. Its swap CLI uses a different interface from Phase2TradingBot.
- A contract never acts on its own: it only runs when called, and receiving tokens triggers nothing.

## Layout

| Path | What |
|---|---|
| `event_service.py` | feed, snapshot and execution coordination |
| `event_history.py`, `event_diagnostics.py` | bounded Phase 2 price observations and read-only entry explanations |
| `event_activity.py`, `event_observer.py`, `event_portfolio.py` | receipt/Swap markers, bounded background diagnostics, pinned holdings and Decimal display accounting |
| `event_engine.py` | candidate lifecycle, pending receipt work, expiry and nonce settlement; offline |
| `event_execution.py`, `event_store.py` | exact execution checks and durable intent/position accounting |
| `event_fees.py`, `event_submission.py` | bounded background fee evidence, integer selection, local timing and inclusion/order diagnostics |
| `operator_lock.py` | shared local live-sender lock, halt checks and service handoff rules |
| `phase2_contract.py`, `contracts/Phase2TradingBot.sol` | unified Phase 2 build/deployment and owner calldata |
| `main.py` / `legacy_main.py` | Phase 2 entry point / original polling runner and reconciliation tools |
| `strategy.py` | the six entry strategies, the shared take profit / stop loss, settings validation: pure functions |
| `v3pool.py`, `pool_profile.json` | exact Uniswap V3 swap maths over the pool's liquidity layout (a saved snapshot, scaled by one read of the pool's liquidity when a buy is sized, or a fresh scan the dashboard's button makes the bot do, saved in `logs/pool_profile.json`; the maths itself is offline): what a trade of a given size costs, used to size buys by risk and for the dashboard's estimates |
| `compare.py` | paper replay of every strategy on the recorded prices (the dashboard's comparison table); pure |
| `ledger.py` | the bot's open position and closed trades (`logs/ledger.json`), with the profit maths |
| `control.py`, `history.py`, `state.py` | the runtime settings file the dashboard writes, the price history for the chart, and the price and balances the bot last read (`logs/state.json`, what the dashboard shows) |
| `dashboard.py`, `dashboard.html`, `dashboard_legacy.html` | Phase 2 dashboard and separate `/legacy` recovery view (loopback only, no chain reads) |
| `status.py` | read-only status in the terminal: position, profit and loss, bot health |
| `bot_contract.py` | compile / deploy / status / swap / owner-calldata CLI |
| `live_check.py` | read-only check of the contract you deployed on Sepolia (no transactions) |
| `pool_monitor.py` | read-only pool status: price, swap count, who is trading, new actors (no key needed) |
| `config.py`, `rpc_validation.py` | shared RPC pacing/failover, event-only validation without redundant chain reads, pool and quoter helpers (also a read-only CLI) |
| `contracts/TradingBot.sol` | the bot contract (`mocks/` is test-only) |
| `drafts/` | local scratch space for throwaway scripts and notes; gitignored, so it only exists on your machine (empty it when you are done). Agents work in it with the Read / Write / Edit tools directly, not with shell heredocs or `python -c` |
| `abis/` | Includes the tracked `phase2_trading_bot.json` for Phase 2 runtime and owner calldata. Legacy `trading_bot.json` is deployment-specific and gitignored. |
| `deploy/*.service` | systemd units for Ubuntu 24.04 (the bot, and the optional dashboard) |
| `docs/GETTING_STARTED.md` | **start here**: install, first run, dashboard, strategies, going live |
| `docs/DEPLOY.md` | full deploy and 24/7 reference, owner operations, troubleshooting |
| `tests/` | contract tests (in-memory chain) and bot-loop tests |
| `research/` | dated studies of the pool (saved swaps, replay, simulations); a record to learn from, not part of the bot |
| `PLAN.md`, `info.md` | original build plan and notes |
| `AGENTS.md` | instructions for coding agents working in this repo |

## Quick start: Phase 2 paper observation

```bash
uv sync                       # Python 3.13 and dependencies
cp -n .env.example .env       # new installations only; preserve an existing .env
uv run pytest                 # tests; no network, no transactions
```

Configure `RPC_URL` and an Alchemy Sepolia endpoint (`ALCHEMY_WS_URL` or an Alchemy URL in `RPC_URL` / `RPC_FALLBACK_URLS`). For paper observation leave `BOT_CONTRACT_ADDRESS` empty and keep `DRY_RUN=true`. Compiler installation for contract tests is covered in the [operator walkthrough](docs/EVENT_CONTRACT_SETUP.md).

```bash
uv run python main.py
# In another terminal:
uv run python dashboard.py
```

On a server, reach the dashboard from your laptop with `ssh -L 8765:127.0.0.1:8765 user@server`, then open <http://localhost:8765>. It only listens on the server's loopback address and has no login, so never expose its port.

Open the **Phase 2 trading** overview. `awaiting_scan` asks for **Scan Pool Liquidity**. For deployment, funding and simulation, continue with [EVENT_CONTRACT_SETUP](docs/EVENT_CONTRACT_SETUP.md). Runtime simulation/live and owner calldata use the bundled `abis/phase2_trading_bot.json`; a server checkout needs no compiler or copied build artifact to run an existing contract.

`legacy_main.py`, `bot_contract.py deploy/swap`, `live_check.py`, and the dashboard's polling-strategy controls belong to the [legacy workflow](docs/GETTING_STARTED.md#legacy-polling-bot-reference).

On the Ubuntu server use the venv's Python (`source .venv/bin/activate`, then `python …`) and the systemd unit. **See [docs/DEPLOY.md](docs/DEPLOY.md) for every step**, including RPC provider setup (Infura or Alchemy), server hardening, Etherscan verification, owner operations and troubleshooting.

## Configuration (`.env`)

See `.env.example` for all variables. The important ones:

| Variable | Meaning |
|---|---|
| `RPC_URL` | Infura **or** Alchemy Sepolia URL (secret) |
| `MAX_RPC_PER_SECOND` | optional cap on JSON-RPC requests per second, any provider (`0` = off). It sits in the RPC layer, so it covers the loop and every read-only script |
| `RPC_FALLBACK_URLS` | optional comma-separated fallback RPC URLs. On a rate limit or outage the same request is retried on the next provider (a swap re-broadcast sends the identical signed bytes, so it cannot double-send) and the failed one is skipped for a minute |
| `PRIVATE_KEY` | bot wallet key (the contract's operator). Never the owner's key |
| `POOL_ADDRESS`, `POOL_FEE` | loop's execution pool and expected fee (`500` = 0.05%); actual token addresses, fee and factory are checked |
| `SECONDARY_POOL_ADDRESS`, `SECONDARY_POOL_FEE` | optional comparison pool (`10000` = 1%); does not enable automatic routing or arbitrage |
| `F564_ADDRESS`, `FETH_ADDRESS` | current phase's quote/base contracts; Phase 1 and Phase 2 use different addresses |
| `OWNER_ADDRESS` | enrolled wallet that owns the contract (checksummed) |
| `BOT_CONTRACT_ADDRESS` | set after deploying |
| `EVENT_EXECUTION_MODE` | Legacy values `paper`, `simulate`, `live` are accepted for compatibility. Vault configuration selects the safe startup mode. Every new live session requires a dashboard mode request. |
| `EVENT_QUOTE_CAP`, `EVENT_BASE_CAP`, `EVENT_MIN_PROFIT` | event-service caps and profit floor in human token units; see the operator walkthrough |
| `MAX_TRADE_AMOUNT` | size of one **buy** (in f564, the quote token); a sell closes exactly what the buy delivered. Also the on-chain per-swap cap set at deploy |
| `RISK_PER_TRADE_PCT` | optional, `0` = off (every buy is `MAX_TRADE_AMOUNT`). Above 0, a buy is sized so that a stop loss costs about this share of the f564 in the vault, pool fee and price impact included (worked out on the liquidity saved in `pool_profile.json`, so a bigger buy pays more impact), and never exceeds `MAX_TRADE_AMOUNT` |
| `DRY_RUN` | only the word `false` (any capitalisation) sends real transactions; anything else simulates only |
| `POLL_SECONDS`, `FAST_POLL_SECONDS`, `COOLDOWN_SECONDS`, `MIN_OPERATOR_ETH`, `MAX_TRADE_FAILURES` | loop pacing (`FAST_POLL_SECONDS`, default 0 = off, polls faster during seconds 30 to 56 of every minute, when the pool's big trader swaps; `COOLDOWN_SECONDS` defaults to 60) and kill switch |

The **Phase 2 panel** has its own entry pause, module switches, scan and position-exit request. The legacy **Sell everything**, take-profit/stop-loss and polling-strategy settings control `legacy_main.py` only. Both sets of controls are saved to `logs/control.json`.

## Safety model

Every local live entry point uses `operator_lock.sender()`: event and legacy services, manual swaps, and both deployment commands. It holds `logs/operator.lock`, respects both `logs/HALTED` and `logs/events/HALTED`, and refuses occupied or unreadable execution records from the other service. A service can resume its own position through its own ledger checks; manual swaps/deployments require both ledgers flat. These checks are local to this checkout. Paper/dry-run commands bypass this live-sender check.

`logs/market.json` binds records to a pool, token pair and vault. A market change or unidentified old records stops startup; follow the Phase 2 migration guide to reconcile positions and archive old logs. The shipped liquidity profile belongs to Phase 1; Phase 2 risk sizing requires a fresh matching scan. Scanning the 0.05% pool takes roughly 700 bitmap reads plus initialized ticks, rather than Phase 1's roughly 120 total reads. Without a matching scan, dashboard replay explicitly labels its impact allowance as a Phase 1 estimate.

- The owner key is never in `.env` or on the server. A leaked bot key can trade only inside the on-chain limits (allowed tokens, fee tier, per-swap cap) and cannot withdraw.
- `DRY_RUN` defaults to on, and chain id is checked to be Sepolia before anything is signed.
- Secrets (the RPC URL and its key, the private key) are redacted from logs and `logs/HALTED`.
- The kill switch writes `logs/HALTED`; the bot will not start again until you read it and delete the file.
- The dashboard listens on `127.0.0.1` only, refuses requests whose `Host` or `Origin` is not local, and can change only the strategy settings, pause new buys and ask the bot for a pool scan: never `DRY_RUN`, the trade size, an address or a key.
- An unreadable `logs/ledger.json` halts the bot instead of guessing whether it holds a position.

## Development

```bash
make check     # ruff lint + format check
make test      # pytest
make events    # event service; uses saved configuration (paper by default)
```

Changing `contracts/TradingBot.sol` means a **new deployment** (a deployed contract can't be edited) and re-linking on the platform. See `AGENTS.md` before changing the contract or the loop.
