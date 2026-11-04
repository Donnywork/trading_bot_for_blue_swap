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
- **The dashboard must stay on `127.0.0.1`, keep its `Host` / `Origin` / content-type checks, and only ever write `logs/control.json`.** It must never be able to change `DRY_RUN`, the trade size, an address or a key, or send a transaction. New settings go through `strategy.parse_config()` with limits (unknown keys are errors). **The dashboard makes no RPC calls** (it reads `logs/state.json`, written by the bot): do not add a chain read to it, an open tab must cost no quota. Do not run `dashboard.py` or `status.py` against the user's `.env` unless asked, like `live_check.py`.

## Layout

- `main.py`: `Runner` (loop, kill switch, backoff), `ChainIO` (all chain access, so `Runner` is testable with a fake), `Settings`, logging, `cli()`.
- `strategy.py`: pure (no web3): the entry strategies in `STRATEGIES`, `decide(prices, position, config)`, `exit_reason()` (take profit / stop loss shared by all), and `parse_config()` which validates every runtime setting. The strategies are placeholders. To add one: a function `(prices, values) -> bool`, a `Strategy(...)` entry with `Param` limits, and tests in `tests/test_strategies.py`; the dashboard picks it up from `schema()`.
- `ledger.py`: the bot's open position and closed trades (`logs/ledger.json`, or `ledger_dryrun.json` in dry run, never mixed) and the profit maths. `control.py` / `history.py`: the runtime settings file (`logs/control.json`) and the price history (`logs/prices.csv`). `state.py`: `logs/state.json`, the price and balances the bot read on its last tick; the bot writes it, the dashboard only reads it.
- `dashboard.py` + `dashboard.html`: the web UI (standard library only). `status.py`: the same status in the terminal.
- `live_check.py`: read-only checks of a deployed contract (roles, limits, guards via `eth_call`, simulated swaps). Never sends a transaction. `main.py --ticks N` runs the real loop N times and exits.
- `pool_monitor.py`: read-only pool activity from Swap logs (price, actors, new actors). Needs only `RPC_URL` and `POOL_ADDRESS` (`bot_contract.connect_read_only()`), never a key. Run it only when the user asks, like `live_check.py`.
- `bot_contract.py`: compile (`standard_json_input()` is the single build input, shared with Etherscan verification), deploy, status, swap, owner calldata. `validate_owner()` guards the deploy.
- `config.py`: pool, quoter and price helpers. Paths are anchored to the file, not the working directory.
- `contracts/TradingBot.sol`, `contracts/mocks/Mocks.sol` (test-only mocks).
- `tests/test_bot_contract.py` (contract + script), `tests/test_runner.py` (loop + strategy), `tests/test_live_check.py` (the live check against the in-memory chain), `tests/test_strategies.py`, `tests/test_control.py`, `tests/test_dashboard.py`, `tests/test_status.py`, `tests/test_ledger.py`, `tests/test_state.py`, `tests/test_pool_monitor.py` (monitor with fake logs), `tests/conftest.py` (shared `env` fixture). Tests switch the guard off (`guard_flip_pct=0`) unless they are about it: the `make` fixture in `test_runner.py` writes such a settings file, `make(io, guard=True)` leaves the defaults.
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
- Exits (take profit / stop loss) need no price history and are not delayed by the cooldown after a successful buy; after a failed attempt they wait the cooldown like any trade.
- A new `control.json` applies on the next tick; an invalid one keeps the previous settings instead of stopping the bot (at start-up there are none yet, so the defaults apply and the log says so).
- A swap's outcome is classified by where it failed: before the broadcast (`bot_contract.NotSent`, a read error) the tick is skipped; from the broadcast on (`SentUnknown`, or `TimeExhausted`) the bot **halts**. Never turn a post-send error into a retry. What a swap delivered is read from the receipt's Transfer logs (`received_from_receipt`), never from a balance delta. A buy is skipped if the base it would return could exceed the contract's per-swap cap, since the exit sells exactly that amount.
- The kill switch exits **0** so systemd (`Restart=on-failure`) does not restart a halted bot. Crashes exit non-zero and are restarted.
- A signal only sets a flag; the current tick, including a swap waiting for its receipt, finishes first.
- `main.py` startup refuses to run while `logs/HALTED` exists.
- The server runs `.venv/bin/python main.py` directly (no `uv run`), and `abis/trading_bot.json` (written by `deploy`) must be present so no compile happens at runtime.

## Docs

If you change commands, env vars, the loop's behavior, the strategies, the dashboard or the deploy flow, update `README.md`, `docs/GETTING_STARTED.md` (it holds the cheat sheet, the troubleshooting table and the log lines users are told to look for), `docs/DEPLOY.md`, `.env.example` and this file in the same change.
