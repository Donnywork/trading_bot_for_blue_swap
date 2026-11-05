# AGENTS.md

Guidance for coding agents working in this repo. Humans: see `README.md`, `docs/GETTING_STARTED.md` and `docs/DEPLOY.md`.

## What this is

A Python (uv, Python 3.13, web3 v7) trading bot for a Uniswap V3 pool on **Sepolia testnet**, plus a Solidity vault/executor contract (`contracts/TradingBot.sol`, solc 0.8.27) the bot trades through. It is meant to run unattended on Ubuntu 24.04 under systemd. **Testnet only; mainnet is out of scope.**

## Commands

```bash
uv sync                      # install (includes dev tools)
make test                    # uv run pytest: in-memory chain, no network, no transactions
make check                   # ruff lint + format check; run `make format` to fix
make run                     # uv run python main.py (the bot loop); `main.py --ticks N` runs N ticks and exits
make status                  # uv run python status.py (read-only; needs a real .env: only when asked)
make dashboard               # uv run python dashboard.py (loopback web UI; needs a real .env: only when asked)
uv run python bot_contract.py verify-input   # offline; the other subcommands use the network
uv run python live_check.py                  # needs a real .env; read-only. Do not run it unless the user asks
```

Before finishing any change: `make check` and `make test` must both pass.

## Hard rules (secrets and money)

- **Never read, print, log or commit `.env`** or any private key or API key. `.env.example` is the only env file you may read or edit, and it holds placeholders only.
- **Never send a transaction, deploy, or sign anything** unless the user explicitly asks for that action in this conversation. Tests use an in-memory chain; keep it that way. Do not point tests at a live RPC.
- Read-only `eth_call` checks against public Sepolia data are fine, with no key involved.
- Keep `DRY_RUN` defaulting to on (anything other than `"false"` means dry run). Keep the Sepolia chain-id guard in `bot_contract.connect()`.
- The **owner's key must never be in `.env`, the code, or the server**. Owner actions are done by printing calldata (`bot_contract.py withdraw-calldata` / `calldata`).
- Do not add anything that logs the RPC URL (an Infura URL contains the API key). Route log output through `RedactingFormatter` / `redact()` in `main.py`, including crash tracebacks, `logs/HALTED` and anything the dashboard or `status.py` prints or returns.
- **The dashboard must stay on `127.0.0.1`, keep its `Host` / `Origin` / content-type checks, and only ever write `logs/control.json`.** It must never be able to change `DRY_RUN`, the trade size, an address or a key, or send a transaction. Its two buttons (`POST /api/command`: pause / resume new buys, scan the pool) only write `paused` / `scan_request` into that file (`control.save`, on top of the saved settings: a damaged file is refused, not overwritten, and `parse_config()` checks it whenever it is loaded); the bot does the work (see Runtime invariants), the dashboard never reads the chain itself. New settings go through `strategy.parse_config()` with limits (unknown keys are errors). Its read-only views (`/api/prices`, `/api/trades`, `/api/compare`, `/api/preview`) validate their query strings (`QueryError` is a 400) and only read `logs/`. **The dashboard makes no RPC calls** (it reads `logs/state.json`, written by the bot, which also carries the pool depth): do not add a chain read to it, an open tab must cost no quota. Do not run `dashboard.py` or `status.py` against the user's `.env` unless asked, like `live_check.py`.

## Layout

- `main.py`: `Runner` (loop, kill switch, backoff), `ChainIO` (all chain access, so `Runner` is testable with a fake), `Settings`, logging, `cli()`.
- `strategy.py`: pure (no web3): the entry strategies in `STRATEGIES`, `decide(prices, position, config)`, `exit_reason()` (take profit / stop loss, and the optional time stop given `held_seconds`, shared by all), `trade_size()` (risk-based buy size; with a `loss_at_stop` function it searches the biggest buy whose stop loss fits the budget on the pool's real liquidity), the cost constants `POOL_FEE_PCT` / `IMPACT_PCT` / `FRICTION_PCT`, and `parse_config()` which validates every runtime setting. The strategies are placeholders. To add one: a function `(prices, values) -> bool`, a `Strategy(...)` entry with `Param` limits, and tests in `tests/test_strategies.py`; the dashboard picks it up from `schema()`.
- `v3pool.py` + `pool_profile.json`: exact Uniswap V3 swap maths over the pool's saved liquidity layout (`pool_profile.json` is the pool's layout as of 2026-11-04, block 12063088, from `research/2026-11-04-pool-patterns/scan_liquidity.py`: about 120 calls, too many to repeat while running, a free RPC plan rate-limits bursts, so a new scan only happens when asked: the dashboard's scan button makes the bot read it (`config.read_depth`, 0.2 s between calls) and save it to `logs/pool_profile.json`, which `v3pool.current_profile()` then prefers). What is cheap is ONE read of the pool's in-range `liquidity()`: `depth_scale()` compares it with what the saved layout holds at the same price and `scaled()` multiplies the layout by that, so the model follows the pool getting deeper or thinner as a whole (not liquidity moving to other price ranges: that needs a new scan). `v3pool.py` is pure and offline (no web3, no chain read). `loss_at_stop()` is what a stop loss costs for a buy of a given size, both pool fees and the price impact of both legs included. `main.py` loads the profile only when `RISK_PER_TRADE_PCT` is above 0 and **refuses to start without it** rather than size with a guess. `tests/test_v3pool.py`.
- `compare.py`: pure paper replay of every entry strategy on the recorded prices, with the settings in force (the dashboard's comparison table) or with settings typed in the form but not yet saved (`/api/preview`, which validates them like a save and writes nothing). It splits the result into price moves, pool fee and price impact: exact for the trade's size when given the pool's liquidity layout (`v3pool`: the saved layout scaled by the `depth_scale` in `logs/state.json`), else the fixed `strategy.POOL_FEE_PCT` / `IMPACT_PCT`; `advice()` returns the warnings. No chain access.
- `ledger.py`: the bot's open position and closed trades (`logs/ledger.json`, or `ledger_dryrun.json` in dry run, never mixed) and the profit maths. `control.py` / `history.py`: the runtime settings file (`logs/control.json`) and the price history (`logs/prices.csv`). `state.py`: `logs/state.json`, the price and balances the bot read on its last tick; the bot writes it, the dashboard only reads it.
- `dashboard.py` + `dashboard.html`: the web UI (standard library only). `status.py`: the same status in the terminal.
- `live_check.py`: read-only checks of a deployed contract (roles, limits, guards via `eth_call`, simulated swaps). Never sends a transaction. `main.py --ticks N` runs the real loop N times and exits.
- `pool_monitor.py`: read-only pool activity from Swap logs (price, actors, new actors). Needs only `RPC_URL` and `POOL_ADDRESS` (`bot_contract.connect_read_only()`), never a key. Run it only when the user asks, like `live_check.py`.
- `bot_contract.py`: compile (`standard_json_input()` is the single build input, shared with Etherscan verification), deploy, status, swap, owner calldata. `validate_owner()` guards the deploy.
- `config.py`: pool, quoter and price helpers. Paths are anchored to the file, not the working directory.
- `contracts/TradingBot.sol`, `contracts/mocks/Mocks.sol` (test-only mocks).
- `tests/test_bot_contract.py` (contract + script), `tests/test_runner.py` (loop + strategy), `tests/test_live_check.py` (the live check against the in-memory chain), `tests/test_strategies.py`, `tests/test_compare.py` (replay, cost split, warnings), `tests/test_control.py`, `tests/test_dashboard.py`, `tests/test_status.py`, `tests/test_ledger.py`, `tests/test_state.py`, `tests/test_v3pool.py`, `tests/test_pool_monitor.py` (monitor with fake logs), `tests/conftest.py` (shared `env` fixture). Tests switch the guard off (`guard_flip_pct=0`) unless they are about it: the `make` fixture in `test_runner.py` writes such a settings file, `make(io, guard=True)` leaves the defaults.
- `drafts/`: gitignored local scratch space for throwaway scripts, notes and intermediate output. Use it instead of `/tmp` or any folder outside the repo, run scripts from the repo root, and **delete what you created there when the work is done**. **Create, change and read those files with the Read / Write / Edit tools directly in `drafts/`**, never with shell heredocs, `cat`, `sed` or `python -c` chains (they trigger permission prompts); the only shell command is running the finished script (`uv run python drafts/x.py`) (anything worth keeping goes to `research/` or the repo proper). It is never committed, and the same hard rules apply: no `.env`, no transactions.
- `research/`: dated data and scripts from studies of the pool (the saved swaps, the replay, the paper forward test); a record to learn from, not part of the bot.
- `deploy/trading-bot.service` and `deploy/trading-bot-dashboard.service` (systemd), `docs/GETTING_STARTED.md` (the walkthrough for a new user) and `docs/DEPLOY.md` (the reference).

## Conventions

- Match the existing style: flat functions, banner comments (`# ------------------------------------`) on the important ones, short comments that say *why*. No classes unless state needs them (`Runner`, `ChainIO`, `Settings`).
- On-chain amounts are **integers** in the token's smallest unit. Never pass floats; use `int(Decimal(str) * 10**decimals)`. Slippage math is integer math.
- Floats are fine for price signals and display only.
- Keep `uv.lock` committed; add dependencies with `uv add` (runtime) or `uv add --dev` (tests/tools).

## Contract changes

- A deployed contract cannot be edited: any change to `TradingBot.sol` means a new deployment, moving funds with `withdraw`, and re-linking on the platform. Say so when proposing one.
- Constraints to preserve: `owner()` must return the enrolled wallet (the platform checks it); swap output always goes to `address(this)`; only the owner can withdraw, and only to itself; the operator can only `swap()` within `tokenCap` / `feeAllowed`; `amountOutMinimum` must be nonzero; ownership transfer stays two-step.
- Keep the `ISwapRouter02` struct in sync with `abis/router.json` (SwapRouter02's `exactInputSingle` has **no `deadline`**).
- When changing the contract, add or adjust tests, and make sure a deliberately broken version (for example, drop an `onlyOwner` or a limit check) fails the suite. Recompile with the same settings as `standard_json_input()` (optimizer 200 runs, `paris`) so Etherscan verification still matches.

## Runtime invariants (do not weaken)

- Never auto-retry a *send*. A receipt timeout, or a connection error while not in dry run, **halts** (writes `logs/HALTED`, exit code 0). Read errors back off and retry; they never halt.
- The bot only sells what it bought: a position is recorded in the ledger right after a trade lands, and a sell closes exactly the recorded amount (the buy and sell trade sizes differ, so never reuse `MAX_TRADE_AMOUNT` for a sell). An unreadable ledger **halts**; it is never silently reset. Live and dry-run ledgers stay separate.
- The pattern guard (`strategy.guard_status`, settings `guard_flip_pct` / `guard_drift_pct`) only ever blocks a new **buy**; it never blocks or delays an exit. It fails closed (too few big price steps means paused). `decide()` evaluates it only when an entry rule wants to buy; the runner and the dashboard also compute it every tick, for the log and the display. The runner restores the price history from `logs/prices.csv` after a restart if it is under 10 minutes old.
- The **time stop** (`time_stop_on`, `time_stop_min` in `control.json`, off by default) is one more exit: a position held at least that long is sold, but only when neither price target applies (the price reason wins). It needs the position's age, which comes from the ledger's `opened`, so a restart does not reset it.
- **Buy size**: `MAX_TRADE_AMOUNT` is the ceiling. With `RISK_PER_TRADE_PCT` above 0 (env only, never the dashboard) `strategy.trade_size()` sizes a buy so a stop loss costs that share of the vault's quote, never above the ceiling or the vault balance. The cost of the stop loss is computed exactly on the pool's saved liquidity (`v3pool.loss_at_stop`: both fees and the price impact of both legs, so a bigger buy costs more per unit); only without the profile (`Runner(depth=None)`, tests) does it fall back to the fixed `FRICTION_PCT`, which is right for buys of about 1000 and too low for 10,000 and up. It limits the loss, not the cost: it does not check that the round-trip cost leaves the take profit worth taking.
- **Pool depth** (`Runner.pool_now`): one read-only `pool.liquidity()` call, made only when a buy is being sized with `RISK_PER_TRADE_PCT` above 0, and at most every `DEPTH_MAX_AGE` (5 min). A failed read skips that buy with a warning: it is a read, so never a halt. The scale it finds goes into `logs/state.json` as `depth_scale`, which is how the dashboard's estimates know it (1.0, the layout as saved, until the first time a buy is sized). Not tested against a live RPC.
- **Pause** (`paused` in control.json, the dashboard's button): `Runner.step` turns a `buy` into `hold`. A sale (take profit, stop loss, time stop) is never held back. Logged when it changes, and as ` PAUSED` on every tick line.
- **Scan request** (`scan_request` in control.json, a number that only goes up): when it is above the last one served, the bot reads the pool's whole layout at the END of the tick (`Runner.serve_scan_request`, so a sale decided in the same tick is not held up): `config.read_depth`, about 120 read-only calls `DEPTH_CALL_PAUSE` apart, 30 to 40 s. It saves it, with the `request` it answers, to `logs/pool_profile.json` (so a restart does not scan again) and answers the dashboard in `logs/state.json` (`scan`: `done` plus `block` / `at`, or `error`, which goes through `redact()`). Every call is made at the block taken before the scan, so the layout is one snapshot, and `config.read_depth` asks the runner's stop flag before each call (a stop ends the scan as a failed one). A failed scan is not retried by itself (the button asks again), not even after a restart (`Runner.restored_scan` takes the last answer, failures included, from `logs/state.json`, or the saved layout's `request` when that is newer; the answer is written to the state file in the same tick as the scan). Saving the settings form over a damaged `control.json` comes out paused (fail closed), and never halts, whatever the exception: it is a read. The sell still closes exactly the recorded base.
- Exits (take profit / stop loss) need no price history and are not delayed by the cooldown after a successful buy; after a failed attempt they wait the cooldown like any trade.
- A new `control.json` applies on the next tick; an invalid one keeps the previous settings instead of stopping the bot (at start-up there are none yet, so the defaults apply and the log says so).
- A swap's outcome is classified by where it failed: before the broadcast (`bot_contract.NotSent`, a read error) the tick is skipped; from the broadcast on (`SentUnknown`, or `TimeExhausted`) the bot **halts**. Never turn a post-send error into a retry. What a swap delivered is read from the receipt's Transfer logs (`received_from_receipt`), never from a balance delta. A buy is skipped if the base it would return could exceed the contract's per-swap cap, since the exit sells exactly that amount.
- The kill switch exits **0** so systemd (`Restart=on-failure`) does not restart a halted bot. Crashes exit non-zero and are restarted.
- **A swap's gas limit is its estimate times `SWAP_GAS_BUFFER` (2.0), not `GAS_BUFFER` (1.2).** The gas a swap needs depends on how many initialised ticks it crosses, i.e. on where the price is when it is MINED. On 2026-11-05 four swaps ran out of gas inside the pool at 1.2x because another swap moved the price in the same block (they needed 21 to 38% more); see `research/2026-11-05-reverted-swaps/`. Do not lower it, and do not read a `Transaction reverted` as slippage without looking at the trace.
- A signal only sets a flag; the current tick, including a swap waiting for its receipt, finishes first.
- `main.py` startup refuses to run while `logs/HALTED` exists.
- The server runs `.venv/bin/python main.py` directly (no `uv run`), and `abis/trading_bot.json` (written by `deploy`, gitignored, copied to the server by hand) must be present so no compile happens at runtime.

## Docs

If you change commands, env vars, the loop's behavior, the strategies, the dashboard or the deploy flow, update `README.md`, `docs/GETTING_STARTED.md` (it holds the cheat sheet, the troubleshooting table and the log lines users are told to look for), `docs/DEPLOY.md`, `.env.example` and this file in the same change.
