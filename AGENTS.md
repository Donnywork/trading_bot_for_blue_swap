# AGENTS.md

Guidance for coding agents working in this repo. Humans: see `README.md` and `docs/DEPLOY.md`.

## What this is

A Python (uv, Python 3.13, web3 v7) trading bot for a Uniswap V3 pool on **Sepolia testnet**, plus a Solidity vault/executor contract (`contracts/TradingBot.sol`, solc 0.8.27) the bot trades through. It is meant to run unattended on Ubuntu 24.04 under systemd. **Testnet only; mainnet is out of scope.**

## Commands

```bash
uv sync                      # install (includes dev tools)
make test                    # uv run pytest: in-memory chain, no network, no transactions
make check                   # ruff lint + format check; run `make format` to fix
make run                     # uv run python main.py (the bot loop)
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
- Do not add anything that logs the RPC URL (an Infura URL contains the API key). Route log output through `RedactingFormatter` / `redact()` in `main.py`, including crash tracebacks and `logs/HALTED`.

## Layout

- `main.py`: `Runner` (loop, kill switch, backoff), `ChainIO` (all chain access, so `Runner` is testable with a fake), `Settings`, logging, `cli()`.
- `strategy.py`: `decide(prices, position)`, a pure function (no web3). The strategy is a placeholder.
- `live_check.py`: read-only checks of a deployed contract (roles, limits, guards via `eth_call`, simulated swaps). Never sends a transaction. `main.py --ticks N` runs the real loop N times and exits.
- `bot_contract.py`: compile (`standard_json_input()` is the single build input, shared with Etherscan verification), deploy, status, swap, owner calldata. `validate_owner()` guards the deploy.
- `config.py`: pool, quoter and price helpers. Paths are anchored to the file, not the working directory.
- `contracts/TradingBot.sol`, `contracts/mocks/Mocks.sol` (test-only mocks).
- `tests/test_bot_contract.py` (contract + script), `tests/test_runner.py` (loop + strategy), `tests/test_live_check.py` (the live check against the in-memory chain), `tests/conftest.py` (shared `env` fixture).
- `deploy/trading-bot.service` (systemd), `docs/DEPLOY.md` (the full guide).

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
- The kill switch exits **0** so systemd (`Restart=on-failure`) does not restart a halted bot. Crashes exit non-zero and are restarted.
- A signal only sets a flag; the current tick, including a swap waiting for its receipt, finishes first.
- `main.py` startup refuses to run while `logs/HALTED` exists.
- The server runs `.venv/bin/python main.py` directly (no `uv run`), and `abis/trading_bot.json` (written by `deploy`) must be present so no compile happens at runtime.

## Docs

If you change commands, env vars, the loop's behavior, or the deploy flow, update `README.md`, `docs/DEPLOY.md`, `.env.example` and this file in the same change.
