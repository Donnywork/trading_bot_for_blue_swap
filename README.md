# Uniswap V3 Sepolia trading bot

A Python bot that reads a Uniswap V3 pool on **Sepolia (testnet only)**, decides buy / sell / hold, and swaps through a small **bot contract** that you own. It is built to run unattended on Ubuntu 24.04 under systemd.

> Testnet only. Nothing here has the safeguards real money would need. The strategies in `strategy.py` are placeholders, not trading advice.

## Start here

**New to this repo? Follow [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**: a step-by-step walkthrough from install to a running bot you can watch and steer in a browser, with what you should see at each step. Then [docs/DEPLOY.md](docs/DEPLOY.md) is the reference for the server, owner operations and troubleshooting.

What you get:

- a **bot** that buys with a strategy you pick (six to choose from) and sells at a **take profit** or **stop loss**, with a **pattern guard** that pauses new buys when the market stops behaving as the strategy assumes;
- a **dashboard** (`dashboard.py`) showing price, position, profit and loss, with a form to switch strategy and change take profit / stop loss while the bot runs;
- a `status.py` command for the same information in a terminal, and a read-only `pool_monitor.py` that shows who else trades in the pool.

## How it fits together

```
 owner wallet (MetaMask)         bot wallet (key in .env)           bot contract
 withdraws, sets limits   <---   pays gas, calls swap()   ---->    holds f564 / fETH,
 key never on the server         inside the owner's limits          swaps via SwapRouter02
```

- **Contract** (`contracts/TradingBot.sol`): a vault + swap executor. `owner()` returns your enrolled wallet (what the platform checks). The operator (the bot's key) can only call `swap()`, only for allowed tokens and fee tiers, up to a per-swap cap, and the output stays in the contract. Only the owner can withdraw, change limits, or hand over ownership (two-step).
- **Bot** (`main.py`): polls the pool, asks `strategy.decide()` when it holds nothing, sells what it bought at a take profit or stop loss, and swaps through the contract. It keeps its position in `logs/ledger.json`, re-reads `logs/control.json` (the dashboard's settings) every tick, backs off on RPC errors, halts itself after repeated failed trades or an unknown transaction state, and never blindly resends.
- A contract never acts on its own: it only runs when called, and receiving tokens triggers nothing.

## Layout

| Path | What |
|---|---|
| `main.py` | the bot loop (`Runner`), logging with secret redaction, kill switch |
| `strategy.py` | the six entry strategies, the shared take profit / stop loss, settings validation: pure functions |
| `ledger.py` | the bot's open position and closed trades (`logs/ledger.json`), with the profit maths |
| `control.py`, `history.py` | the runtime settings file the dashboard writes, and the price history for the chart |
| `dashboard.py`, `dashboard.html` | the web dashboard (loopback only, standard library, no extra dependencies) |
| `status.py` | read-only status in the terminal: position, profit and loss, bot health |
| `bot_contract.py` | compile / deploy / status / swap / owner-calldata CLI |
| `live_check.py` | read-only check of the contract you deployed on Sepolia (no transactions) |
| `pool_monitor.py` | read-only pool status: price, swap count, who is trading, new actors (no key needed) |
| `config.py` | pool and quoter reads, price and quote helpers (also a read-only CLI) |
| `contracts/TradingBot.sol` | the bot contract (`mocks/` is test-only) |
| `abis/` | ABIs; `trading_bot.json` is written by `deploy` |
| `deploy/*.service` | systemd units for Ubuntu 24.04 (the bot, and the optional dashboard) |
| `docs/GETTING_STARTED.md` | **start here**: install, first run, dashboard, strategies, going live |
| `docs/DEPLOY.md` | full deploy and 24/7 reference, owner operations, troubleshooting |
| `tests/` | contract tests (in-memory chain) and bot-loop tests |
| `research/` | dated studies of the pool (saved swaps, replay, simulations); a record to learn from, not part of the bot |
| `PLAN.md`, `info.md` | original build plan and notes |
| `AGENTS.md` | instructions for coding agents working in this repo |

## Quick start (the short version; the full walkthrough is in [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md))

```bash
uv sync                       # Python 3.13 and dependencies
cp .env.example .env          # then fill it in (never commit .env)
uv run pytest                 # tests; no network, no transactions
```

Set in `.env`: `RPC_URL` (Infura Sepolia URL, which contains your API key, so it is a secret), `PRIVATE_KEY` (a **new** bot wallet, funded with Sepolia ETH; GETTING_STARTED step 2 has a script that creates it), `OWNER_ADDRESS` (your enrolled wallet), `MAX_TRADE_AMOUNT`. Keep `DRY_RUN=true` until you have watched a dry run.

```bash
uv run python bot_contract.py deploy      # deploy the contract, then put its address in .env
uv run python bot_contract.py status      # owner(), operator(), limits, balances
uv run python live_check.py              # read-only live check of the deployed contract
uv run python pool_monitor.py --watch 300   # read-only pool activity, refreshed every 5 minutes
uv run python bot_contract.py swap fETH 0.01   # DRY_RUN=true simulates only
uv run python main.py --ticks 3           # run the real loop 3 times and exit (smoke test)
uv run python main.py                     # run the bot in the foreground (make run)
uv run python dashboard.py                # the web dashboard on http://127.0.0.1:8765 (make dashboard)
uv run python status.py                   # position, profit and loss in the terminal (make status)
```

On a server, reach the dashboard from your laptop with `ssh -L 8765:127.0.0.1:8765 user@server`, then open <http://localhost:8765>. It only listens on the server's loopback address and has no login, so never expose its port.

Fund the contract by sending f564 / fETH to its address from MetaMask. It needs no ETH; the bot wallet pays gas.

`live_check.py` tests the contract you deployed: roles, limits, that a stranger or a disallowed token is refused, and a simulated swap each way. It sends nothing. `docs/DEPLOY.md` part 2 (2.8 to 2.13) is the live-verification runbook.

On the Ubuntu server use the venv's Python (`source .venv/bin/activate`, then `python …`) and the systemd unit. **See [docs/DEPLOY.md](docs/DEPLOY.md) for every step**, including Infura setup, server hardening, Etherscan verification, owner operations and troubleshooting.

## Configuration (`.env`)

See `.env.example` for all variables. The important ones:

| Variable | Meaning |
|---|---|
| `RPC_URL` | Infura Sepolia URL (secret) |
| `PRIVATE_KEY` | bot wallet key (the contract's operator). Never the owner's key |
| `OWNER_ADDRESS` | enrolled wallet that owns the contract (checksummed) |
| `BOT_CONTRACT_ADDRESS` | set after deploying |
| `MAX_TRADE_AMOUNT` | size of one **buy** (in f564, the quote token); a sell closes exactly what the buy delivered. Also the on-chain per-swap cap set at deploy |
| `DRY_RUN` | only the word `false` (any capitalisation) sends real transactions; anything else simulates only |
| `POLL_SECONDS`, `COOLDOWN_SECONDS`, `MIN_OPERATOR_ETH`, `MAX_TRADE_FAILURES` | loop pacing and kill switch |

The strategy, take profit, stop loss, the pattern-guard limits and the strategy's own numbers are **not** in `.env`: change them on the dashboard (they are saved to `logs/control.json`, and the bot picks them up on its next tick).

## Safety model

- The owner key is never in `.env` or on the server. A leaked bot key can trade only inside the on-chain limits (allowed tokens, fee tier, per-swap cap) and cannot withdraw.
- `DRY_RUN` defaults to on, and chain id is checked to be Sepolia before anything is signed.
- Secrets (the RPC URL and its key, the private key) are redacted from logs and `logs/HALTED`.
- The kill switch writes `logs/HALTED`; the bot will not start again until you read it and delete the file.
- The dashboard listens on `127.0.0.1` only, refuses requests whose `Host` or `Origin` is not local, and can change only the strategy settings: never `DRY_RUN`, the trade size, an address or a key.
- An unreadable `logs/ledger.json` halts the bot instead of guessing whether it holds a position.

## Development

```bash
make check     # ruff lint + format check
make test      # pytest
make run       # run the bot
```

Changing `contracts/TradingBot.sol` means a **new deployment** (a deployed contract can't be edited) and re-linking on the platform. See `AGENTS.md` before changing the contract or the loop.
