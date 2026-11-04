# Getting started

A step-by-step walkthrough from an empty checkout to a bot you can watch and steer from your laptop. It is written to be followed in order; each step says what you should see, so you know when it worked.

**Testnet only (Sepolia).** The tokens are worthless. Nothing here has the safeguards real money would need, and none of the strategies has a proven edge: treat this as a way to learn, compare ideas and test the machinery.

Contents: [what you end up with](#what-you-end-up-with) · [before you start](#before-you-start) · [steps 1-10](#step-1-install-and-run-the-tests) · [how the bot decides](#how-the-bot-decides) · [the strategies](#the-strategies) · [the pattern guard](#the-pattern-guard) · [take profit and stop loss](#take-profit-and-stop-loss) · [reading the dashboard](#reading-the-dashboard) · [files and commands](#files-the-bot-keeps) · [troubleshooting](#troubleshooting)

---

## What you end up with

```
 your laptop                                  the server (Ubuntu, or this same laptop)
 ┌──────────────────────┐   ssh -L tunnel    ┌─────────────────────────────────────────┐
 │ browser              │ ─────────────────► │ dashboard.py  (127.0.0.1:8765 only)     │
 │ http://localhost:8765│                    │    reads/writes logs/control.json       │
 └──────────────────────┘                    │ main.py       (the bot, ticks every 30s)│
                                             │    reads control.json every tick        │
                                             │    keeps logs/ledger.json (its position)│
                                             │         │ swap()                        │
                                             │         ▼                               │
                                             │ bot contract on Sepolia (holds the funds)│
                                             └─────────────────────────────────────────┘
```

- **The bot** (`main.py`) is the only thing that trades. It checks the pool price, decides *buy*, *sell* or *hold*, and sends swaps through your **bot contract**.
- **The dashboard** (`dashboard.py`) shows what the bot is doing and lets you change the **strategy, take profit, stop loss and pattern-guard limits** without restarting anything. It cannot send a transaction, switch off dry run, change the trade size, and it never shows a key.
- **You** stay in charge of the money: only the owner wallet (MetaMask) can withdraw, and its key is never on the server.

## Before you start

| You need | Why | Where to get it |
|---|---|---|
| A computer with a terminal (macOS or Linux; on Windows use WSL), `git` and `make`, and [`uv`](https://docs.astral.sh/uv/) | runs everything (`uv` fetches Python 3.13 for you) | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then open a new terminal (or `source ~/.local/bin/env`) so `uv` is found |
| An **Infura** account with a Sepolia URL | how the bot talks to the chain. The URL contains your API key, so it is a secret | [DEPLOY 1.1](DEPLOY.md#11-get-an-infura-sepolia-url) |
| **MetaMask** with the wallet enrolled on the platform (the "owner") | owns the contract, can withdraw | [DEPLOY 1.2](DEPLOY.md#12-metamask-your-own-computer) |
| A **new, separate bot wallet** with a little Sepolia ETH | signs the bot's swaps and pays gas. Never reuse the owner wallet | [DEPLOY 1.5-1.6](DEPLOY.md#15-create-the-bot-wallet-and-env-on-the-server) |
| Some **f564 and fETH** in the owner wallet | the funds the bot will trade | the platform / your course faucet |
| (Optional) an Ubuntu server with SSH | to run it 24/7 | [DEPLOY 1.3](DEPLOY.md#13-prepare-the-ubuntu-server) |

You can do **everything on your own computer first** (steps 1-8) and move to a server later (step 9). The dashboard then needs no SSH tunnel: just open `http://localhost:8765`.

### Words used below

| Word | Meaning |
|---|---|
| **owner** | your MetaMask wallet, the one enrolled on the platform. It owns the contract and is the only one that can withdraw. Its key never touches the bot |
| **bot wallet / operator** | a separate wallet whose key sits in `.env`. It signs the bot's swaps and pays their gas, and can do nothing else |
| **bot contract / vault** | the smart contract you deploy. It holds the funds (the "vault") and performs the swaps |
| **base / quote** | the two tokens of the pool. **base = fETH**, **quote = f564**. The price is always *quote per base* (for example 1050 f564 per fETH). A **buy** spends quote and gets base |
| **tick** | one pass of the bot's loop, every 30 seconds: read the price, decide, maybe trade |
| **sample** | one price reading, one per tick. "Average over 20 samples" is about 10 minutes |
| **dry run** | the bot does everything except send: it asks the chain what *would* happen. Nothing is spent |
| **ledger** | the bot's record of what it bought (and what became of it), so it knows what to sell |

---

## Step 1. Install and run the tests

```bash
git clone <your repo> trading_bot && cd trading_bot     # or just cd into your copy
uv sync                  # installs Python 3.13 packages into .venv
make test                # runs the tests: in-memory chain, no network, no transactions
```

**You should see:** `… passed` and no failures. (`make check` additionally runs the linter.) If this fails, stop and fix it first; nothing later will work. The first `uv sync` downloads Python 3.13 if you do not have it.

## Step 2. Fill in `.env`

```bash
cp .env.example .env
chmod 600 .env           # only you can read it
```

Open `.env` in an editor. **Never commit it and never paste it anywhere**: it holds your Infura key and the bot wallet's private key.

**Create the bot wallet first.** It is a brand-new key that only this bot uses. This writes it into `.env` and prints **only the address** (the key is never shown):

```bash
uv run python - <<'PY'
import re
from pathlib import Path
from eth_account import Account

acct = Account.create()
env = Path(".env")
text, n = re.subn(r"^PRIVATE_KEY=.*$", f"PRIVATE_KEY={acct.key.hex()}", env.read_text(), flags=re.M)
assert n == 1, "no PRIVATE_KEY= line found in .env"
env.write_text(text)
env.chmod(0o600)
print("Bot wallet address:", acct.address)
PY
```

Then send about **0.05 Sepolia ETH** to that printed address (a faucet, or from MetaMask). The deploy in step 3 and every later swap are paid from it. Never reuse a wallet that has held real money.

| Variable | Put here | Secret? |
|---|---|---|
| `RPC_URL` | your Infura Sepolia URL | **yes** (contains the key) |
| `PRIVATE_KEY` | already written by the script above. It is the **bot wallet's** key, not your MetaMask one | **yes** |
| `OWNER_ADDRESS` | your enrolled MetaMask address (copy it from MetaMask to keep the capitalisation) | no |
| `MAX_TRADE_AMOUNT` | size of one buy, in units of the token being spent (see [trade size](#trade-size)). Start with `0.01` | no |
| `DRY_RUN` | leave as `true` | no |
| `BOT_CONTRACT_ADDRESS` | leave empty for now; step 3 gives you the value | no |
| everything else | leave as it is (pool, router and token addresses are already filled in) | no |

`DRY_RUN=true` means **nothing is ever sent**: the bot asks the chain what *would* happen. Only the word `false` (in any capitalisation) turns a dry run off; anything else, including a typo, stays a dry run. You will flip it only in step 10.

## Step 3. Deploy your bot contract

This is the one step that sends a real (testnet) transaction, so read the output before moving on.

```bash
uv run python bot_contract.py deploy
```

**What happens:** it checks the network is Sepolia, then prints a summary (deployer, **Owner**, router, the two allowed tokens with their per-swap cap, the bot wallet's ETH) and waits. **Read the `Owner:` line: it must be your MetaMask address.** If it is not, press Ctrl+C. Type `YES` to continue. The first run downloads the Solidity compiler (it needs internet), then deploys and waits for confirmation.

**You should see** `Deployed TradingBot at: 0x…` and an Etherscan link. Copy that address into `.env` as `BOT_CONTRACT_ADDRESS=0x…`.

Linking this contract on the platform is a separate step that this guide does not do for you: see [DEPLOY 2.6](DEPLOY.md#26-link-it-on-the-platform). Details, Etherscan verification and what the contract enforces: [DEPLOY part 2](DEPLOY.md#2-deploy-the-contract).

## Step 4. Put tokens in the contract

In MetaMask, from the **owner wallet**, send some **f564** *and* some **fETH** to the contract address. The contract needs no ETH; the bot wallet pays the gas.

Why both? A buy spends f564 (the *quote* token). The matching sell gives back fETH (the *base* token), and a dry-run sell is simulated against the real contract, which must hold that much fETH. With both in the vault, the dry run can show a complete buy → sell cycle.

## Step 5. Check that everything is wired up

```bash
uv run python live_check.py          # read-only: roles, limits, guards, a simulated swap each way
uv run python bot_contract.py status # owner, operator, balances, operator ETH
```

**You should see** mostly `[PASS]` lines and `0 failed`. `[WARN]` means "look at this", `[FAIL]` means stop and fix (the line says what). The full table of checks is in [DEPLOY 2.8](DEPLOY.md#28-live-check-of-the-deployed-contract-read-only).

## Step 6. Run the bot in dry run

```bash
uv run python main.py --ticks 3     # three ticks (about a minute), then it exits by itself: a smoke test
uv run python main.py               # the real loop, in the foreground. Ctrl+C stops it
```

**You should see** lines like:

```
INFO Started. contract=0x… operator=0x… dry_run=True
INFO poll=30s cooldown=300s trade size=0.01 min ETH=0.01
INFO strategy=mean_reversion take profit +3% stop loss -4% (change it on the dashboard or in control.json)
WARNING new buys are paused: seen only 0 of the 8 big price steps needed to judge the pattern
INFO strategy=mean_reversion price=1054.267260 decision=hold base=0.500000 quote=100.000000 samples=1
```

The `WARNING` is the [pattern guard](#the-pattern-guard): on a fresh start it has not seen any big price steps yet, so it keeps new buys paused. That is normal and not an error (it can last an hour or more on a quiet pool).

`decision=hold` is normal for a long time: the default strategy needs 20 price samples (about 10 minutes) and then a 2% dip. **Leave it running** and open a second terminal for step 7. On Etherscan the bot wallet must show **no** new transactions: a dry run sends nothing.

## Step 7. Open the dashboard

In a second terminal, in the same folder:

```bash
uv run python dashboard.py            # serves http://127.0.0.1:8765
```

Open **http://localhost:8765**. You get the price, your position, profit and loss, the vault balances, a price chart and the settings form. Section [Reading the dashboard](#reading-the-dashboard) explains every part. (On a page that fills in as the bot runs, give it a minute.)

> The chart fills in as the bot runs: it records one price per tick in `logs/prices.csv`. A new install shows "No price history yet" until the bot has ticked a few times.

**If the bot runs on a server**, reach the dashboard through an SSH tunnel from your laptop:

```bash
# on the SERVER (once, in the project folder):   python dashboard.py
# on your LAPTOP:
ssh -L 8765:127.0.0.1:8765 youruser@your-server
# then open http://localhost:8765 in your laptop's browser. Keep the ssh window open.
```

- Local port busy? Use another one: `ssh -L 9000:127.0.0.1:8765 …` and open `http://localhost:9000`.
- The dashboard only listens on the server's loopback address and has no login. **Do not** open its port in the firewall or forward it to `0.0.0.0`; the tunnel is the security.
- **Only run it on a server where you are the only user.** The loopback port has no password, so anyone with a login on that machine could open it and change your strategy settings (they still cannot send transactions or touch keys, `DRY_RUN` or the trade size). On a shared server, do not run it.
- The dashboard never calls the chain itself: the price and vault balances it shows are what the bot last read (`logs/state.json`, written every tick), so an open tab costs no Infura requests. If the bot is not running, the page shows the last values with a warning that they are old.
- To keep it running after you log out, use the systemd unit in step 9, or `tmux`.

## Step 8. Choose a strategy and set the exit rules

On the dashboard, in **Strategy and exit rules**:

1. Pick a **strategy** (when to buy). They are described [below](#the-strategies); start with *Mean reversion*.
2. Adjust its **settings** if you like (each field shows its allowed range).
3. Set **take profit** and **stop loss** (when to sell). Defaults: +3% and −4%. Below them, the **pattern guard** limits (see [The pattern guard](#the-pattern-guard)).
4. Press **Save** (it stays greyed out until you change something; **Discard changes** goes back to what is saved). The message says "Saved … The bot applies it on its next tick".

You can check that the bot noticed in its log (you only see this line when the values actually changed):

```
INFO settings changed: {'strategy': 'rsi', 'take_profit_pct': 6.0, …}
```

Nothing needs restarting. Rules of thumb:

- A **new strategy** only affects the *next buy*. An already open position is still sold by take profit / stop loss.
- **New take profit / stop loss numbers apply to an open position immediately.**
- If you type something invalid, the form refuses it and says why. If the settings file is ever invalid, the bot keeps its previous settings and logs `ignoring the settings file`.

Let the dry run go for an hour or more, then look at **Closed trades** and **Realized profit / loss** (they are *paper* results: the dashboard header says `DRY RUN`).

## Step 9. Run it 24/7 on a server (optional)

Everything about the server (hardening, copying the code, the systemd service, updates) is in [DEPLOY part 1 and 4](DEPLOY.md).

> **Important if you deployed the contract from your laptop (steps 1-8).** The contract's *operator* is the bot wallet whose key was in your laptop's `.env`. The server must use **that same key** and the same `BOT_CONTRACT_ADDRESS`, otherwise `main.py` refuses to start with `PRIVATE_KEY is not the bot contract's operator`. So: copy your `.env` to the server: after the rsync of DEPLOY 1.4 (which already copies `abis/trading_bot.json` but not `.env`), run `scp .env tradingbot@SERVER:bot/.env` from the project folder on your computer, then `chmod 600 ~/bot/.env` on the server (it needs the ssh access set up in DEPLOY 1.3). **Skip DEPLOY 1.5** (it would create a different key). Alternatively deploy from the server instead (DEPLOY part 2). A different server key only works if the owner first sends `setOperator` ([DEPLOY section 5](DEPLOY.md#5-owner-operations)).

In short, once the bot service is installed:

```bash
# the optional dashboard service, same pattern as the bot's (as your admin user):
sed -e 's|__USER__|tradingbot|' -e 's|__PROJECT_DIR__|/home/tradingbot/bot|' \
    /home/tradingbot/bot/deploy/trading-bot-dashboard.service \
  | sudo tee /etc/systemd/system/trading-bot-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot-dashboard
```

It must run as the **same user** as the bot, because it writes the settings file the bot reads. Then use the SSH tunnel from step 7. After you update the code or add a strategy, restart **both** services (`sudo systemctl restart trading-bot trading-bot-dashboard`): the dashboard loads the strategy list when it starts.

## Step 10. Going live (only when you are ready)

Checklist:

- [ ] `live_check.py` shows `0 failed`.
- [ ] A dry run ran for a while and you understand every line in `logs/bot.log`.
- [ ] You looked at the strategy's paper results and are not surprised by them.
- [ ] `MAX_TRADE_AMOUNT` is small.
- [ ] You know how to stop it (Ctrl+C, `systemctl stop trading-bot`) and where the kill-switch file is (`logs/HALTED`).

Then set `DRY_RUN=false` in `.env` (any capitalisation of `false` works), restart **both** the bot and the dashboard (the dashboard reads `DRY_RUN` at start-up), and watch the first trade in `logs/bot.log`, on the dashboard and on Etherscan.

Live and dry-run keep **separate records** (`logs/ledger.json` vs `logs/ledger_dryrun.json`), so paper trades never mix with real ones and going live starts with an empty history.

---

## How the bot decides

Every `POLL_SECONDS` (30 s) it takes one **tick**:

```
read price + vault balances → reload settings → ask the strategy → (maybe) trade → save position
```

The bot is always in one of two states:

```
   FLAT (holds nothing it bought)                    HOLDING (bought base, waiting to exit)
   ┌──────────────────────────────┐   buy signal   ┌───────────────────────────────────────┐
   │ asks the chosen strategy     │ ─────────────► │ ignores the strategy; sells everything │
   │ "is now a good time to buy?" │                │ it bought when the price reaches        │
   └──────────────────────────────┘ ◄───────────── │ take profit (+X%) or stop loss (−Y%)    │
                                       sold         └───────────────────────────────────────┘
```

- **Entry** is decided by whichever strategy you picked. **Exit is the same for all of them.**
- Only **one position at a time**. While holding, there are no more buys.
- A **cooldown** (`COOLDOWN_SECONDS`, 5 min) after any trade stops it re-entering straight away. An exit right after a *successful* buy is **not** delayed (a stop loss must not wait), but after a *failed* attempt it waits the cooldown like any trade.
- The bot only ever sells **what it bought itself**. Tokens you deposited by hand are never sold.
- Nothing is sent unless `DRY_RUN=false`. In a dry run everything else is the same, using the quoted amounts as the "fill" (so a dry-run position, and its profit and loss, are estimates, not vault amounts).

### Trade size

A **buy** spends `MAX_TRADE_AMOUNT` of the *quote* token (f564). With a price near 1000 f564 per fETH, `0.01` f564 buys only about 0.00001 fETH, which is tiny but fine for testing. The matching **sell** sells exactly the fETH that buy delivered, however large or small that is.

The contract also enforces a **per-swap cap** on-chain (set at deploy from `MAX_TRADE_AMOUNT`). To trade bigger you must raise *both* the `.env` number and the on-chain cap (the owner sends `setTokenCap`, see [DEPLOY section 5](DEPLOY.md#5-owner-operations)).

### What the bot will not do

It never sends without `DRY_RUN=false`; never resends a swap whose outcome is unknown (a connection that drops after sending, or a receipt that cannot be read, **halts** the bot and writes `logs/HALTED` instead of guessing; a connection that fails *before* sending just skips the tick); never opens a position it could not close (if a buy could return more base than the contract's per-swap cap allows to sell, it is skipped); never trades while the operator wallet is out of gas; never exceeds the on-chain limits; never withdraws (only the owner can). What a swap delivered is read from the transaction receipt, not from a balance that could be stale.

## The strategies

All six answer one question, "should I buy now?", from the recent prices (one sample per tick). They need enough history first, so a fresh start waits. **None has a proven edge**; in particular, each loses a lot to fees if its thresholds are too small. Fees are 0.3% each way (about 0.6% for a round trip) plus price impact.

| Strategy | Buys when | Tends to work | Tends to fail | Settings (default) |
|---|---|---|---|---|
| **Mean reversion** | the price is X% *below* its recent average | a price that wobbles around a level | a steady downtrend (keeps buying falling prices) | average over 20 samples; dip 2% |
| **Bollinger bands** | the price is below the average by N standard deviations (and at least 1%) | wobbly markets; adapts to how jumpy the price is | quiet pools (rarely triggers), trends | average over 20; 2.0 deviations. The newest price is part of the average, so N must stay below √(window−1): the page refuses a combination that could never buy (2 deviations needs a window of at least 6) |
| **RSI oversold** | recent moves have been overwhelmingly down (RSI ≤ limit) *and* the price fell at least 1% | sharp drops that bounce | slow bleeds | look back 14 samples; RSI ≤ 30 |
| **Sharp drop** | the price is X% below the highest of the last few samples (one big sell) | a pool where a bot swaps back and forth in big steps and the price returns | a steady slide (every step down looks like a new drop) | look back 4 samples; drop 3% |
| **Breakout** | the price rises above its recent high by X% | a price that starts a run | breakouts that fade straight away | high over 20 samples; +1% |
| **Moving average cross** | the fast average climbs above the slow one (at the moment of crossing) | smooth trends | choppy markets; enters late | fast 5, slow 20 samples |

Mean reversion, Bollinger, RSI and Sharp drop are four ways of **buying a dip**; Sharp drop reacts to one big step, the others to a drift away from the average. Breakout and MA cross **follow a trend**. They disagree on purpose: switching between them is the quickest way to see which style this pool rewards.

**Sample size:** with 30 s ticks, 20 samples is 10 minutes, 120 samples is an hour. The bot keeps the last 600 samples in memory. After a restart it takes them back from `logs/prices.csv` if the last one is less than 10 minutes old (the log says `restored N price samples`); after a longer gap it starts from nothing, and the strategies wait for a full window.

Want your own? Add a function to `strategy.py` next to the others and register it in `STRATEGIES`; it then appears on the dashboard automatically. Unit-test it like the rest in `tests/test_strategies.py`.

## Take profit and stop loss

The **entry price** is the pool price at the moment the bot decided to buy. Then, on every tick:

- price ≥ entry × (1 + take profit%) → **sell** (take profit)
- price ≤ entry × (1 − stop loss%) → **sell** (stop loss)

Worked example: entry 1000, take profit 3%, stop loss 4% → sells at ≥ 1030 or ≤ 960.

Things to know:

- **Fees are on top.** A round trip costs about 0.6% plus price impact, so a "+3%" exit nets about +2.4% and a "−4%" exit about −4.6%. The dashboard's profit and loss figures are the *actual* amounts the vault received, after fees (in a dry run, estimates from the quotes). The take profit cannot be set below 1% for this reason.
- **A stop loss is not a guarantee.** The bot checks once per tick. In a thin pool the price can jump past your level between ticks, and selling then costs price impact too.
- **There is no time limit.** A position that goes nowhere stays open until it hits one of the two levels. If you want one, say so.
- **Restarts are safe.** The open position is saved to `logs/ledger.json` the moment a trade lands, and exits work immediately after a restart (no 10-minute warm-up).

## The pattern guard

Every entry rule assumes the market keeps behaving the way it did when you chose it. On this pool one scripted bot makes most of the trades, swapping back and forth in big steps, and the Sharp drop rule lives off that. When that stops (the bot goes quiet, starts selling again and again, or other traders take over), buying dips just buys a falling price. The guard **pauses new buys** when it sees this. It never stops a sale: take profit and stop loss keep working.

It looks only at the price series the bot already has, with two checks:

1. **Do the big steps still alternate?** A "big step" is a jump of 2% or more between two samples (a large swap). Of the last 8 big steps seen (within the last 360 samples, 3 hours at the default 30 s), at least **80%** of neighbouring pairs must go in opposite directions: 6 of the 7 pairs. With fewer than 8 steps it pauses, because it cannot judge: a quiet pool has no pattern to trade.
2. **Is the price sliding?** The average of the last 60 samples may not be more than **5%** below the average of the 60 before.

The guard applies to **every** strategy, including Breakout and MA cross, so to compare strategies fairly switch it off first. **To switch it off:** on the dashboard set both guard limits to 0 and Save; the card then reads *Guard off*. The slide check needs 120 samples (about an hour) and is skipped before that. The limits were tuned on one 8-hour snapshot of this pool and a model: if the pool changes, or you point the bot at another pool, expect it to stay paused (that is by design). `status.py` does not show the guard; on plain SSH look for `new buys are paused` in the log.

Both limits are on the dashboard (0 switches a check off), and the **New buys** card shows *Allowed* or *Paused* with the reason. The log says `new buys are paused: …` and `new buys are allowed again` when it changes (not every tick).

What it costs and what it buys, from the simulations in `research/2026-11-04-pool-patterns/` (a model, not a guarantee): it made the largest drawdown 56 to 72% smaller in worlds where the pattern broke, but it earned less in every world tested, about 8% less while the pattern held and much more when the pattern was only weaker or a rival was active. Whether it also loses profit when the pattern is gone depends on the pool's shape and could not be settled. Its limits are a judgement call, not an optimum (see the research README).

Notes: the guard counts jumps in the price series, so a trade size big enough to move the price 2% by itself (about 15,000 f564 at today's liquidity) would count as a step too. And on a fresh start with no history, buying waits until 8 big steps have been seen (usually the first hour or so), unless the price file is fresh (see above).

## Reading the dashboard

| Where | What it shows |
|---|---|
| Header: `DRY RUN` or `LIVE` | whether the bot is sending real (testnet) transactions |
| Header: bot status | **running** (last tick recent), **not ticking** (no tick for 5 min: stopped or stuck), **HALTED** (kill switch, with the reason), **unknown** (no heartbeat file: not started, or it runs on another machine) |
| Price card | the pool's current price (quote per base, e.g. f564 per fETH), big, with the change over the chart window |
| Position card | none, or what is held: how much, what it cost, **entry price**, the **take-profit and stop-loss prices**, and the unrealized result at the current price |
| New buys card | **Allowed**, **Paused** (with the reason: this is the pattern guard), **Not buying: holding a position**, or **Guard off** (both limits 0); plus the share of recent big steps that alternate and the 60-sample price drift |
| Realized profit/loss | the sum over closed trades, after fees (in a dry run: estimated from the quotes), with wins and losses and how many ended at take profit vs stop loss |
| Vault balances | what the contract holds, and the bot wallet's gas (ETH) |
| Chart | the price line (hover or use the arrow keys for exact values; "Table view" lists them). `▲` = a buy, `▼` = a sell (green: take profit, red: stop loss). While holding, horizontal lines mark entry, take profit and stop loss |
| Strategy and exit rules | the form from step 8 |
| Closed trades | the latest trades: when, why it closed, entry → exit, result |
| Yellow box at the top | something is wrong but not fatal (for example the chain could not be read, or the settings file is invalid) |

Profit figures always carry a sign and an arrow (▲ / ▼), never colour alone. The page updates itself every 5 seconds and uses your system's light or dark theme.

The same information is available in the terminal, which also works over plain SSH:

```bash
uv run python status.py        # make status
```

```
=== BOT STATUS (DRY RUN, paper trades) ===
bot:       RUNNING: last tick 12s ago
price:     1058.600000 f564 per fETH
vault:     0.5 fETH, 12.49 f564   (operator gas: 0.0412 ETH)
position:  OPEN for 40m: 0.0000097 fETH bought for 0.01 f564 at 1040.200000
           take profit at 1071.406000 (+3%), stop loss at 998.592000 (-4%)
           now 1058.600000 (+1.77% from entry), unrealized +0.000178 f564 (+1.78%) at the spot price, before exit fees
realized:  3 closed trades: 2 won, 1 lost (2 take profit, 1 stop loss)
           P/L +0.000120 f564 on 0.03 f564 spent (+0.40%), after fees and price impact
recent:
  11-04 00:12  take_profit 1030.0000 -> 1081.5000  P/L +0.000500 f564
```

The terminal says `STALE` and `UNKNOWN` where the dashboard says "not ticking" and "status unknown".

To see **who else is trading** in the pool (other bots, new arrivals): `uv run python pool_monitor.py --watch 300` ([DEPLOY 4.4](DEPLOY.md#44-monitoring-is-it-alive-and-healthy)).

---

## Files the bot keeps

Everything lives in `logs/` (git-ignored). None of it contains a secret.

| File | What | Safe to delete? |
|---|---|---|
| `bot.log` | the bot's log (rotated at 5 MB) | yes |
| `heartbeat` | time of the last completed tick | yes |
| `state.json` | the price, vault balances and bot-wallet gas the bot read on its last tick; the dashboard shows these instead of calling the chain (no key or URL in it) | yes, the bot rewrites it on the next tick |
| `HALTED` | exists only after the kill switch fired; holds the reason | only after you fixed the cause (the bot refuses to start while it exists) |
| `ledger.json` / `ledger_dryrun.json` | the open position and closed trades, live / paper (the dashboard and `status.py` read the one matching `DRY_RUN`) | **not while the bot holds a position** (see below); otherwise deleting just forgets the history |
| `control.json` | the strategy, take profit, stop loss and strategy settings (what the dashboard writes). Hand-editable: the same six keys as the form (`strategy`, `take_profit_pct`, `stop_loss_pct`, `params`, `guard_flip_pct`, `guard_drift_pct`), missing keys keep their defaults, unknown keys are an error | yes, but a **running** bot keeps its current settings until you save again from the dashboard or restart it |
| `prices.csv` | one price per tick, for the chart | yes |
| `monitor_seen.json` | addresses `pool_monitor.py` has already seen | yes |

**Resetting a stuck or unwanted position.** The ledger is how the bot knows it owns something to sell. If it says OPEN but the vault no longer holds that token (you withdrew it, or it was a paper position and the vault has none), the exit logs `skipped: vault balance … < trade size` forever. To clear it: stop the bot, then either edit the ledger file of that mode (`ledger_dryrun.json` for dry run) and set `"open": null` (this keeps your closed-trade history), or delete the file (this also forgets the history), then start the bot. If the vault *does* hold base the bot bought but the ledger lost track of it, the bot will not sell it: withdraw it as owner ([DEPLOY section 5](DEPLOY.md#5-owner-operations)) or sell it by hand with `bot_contract.py swap`.

## Command cheat sheet

| I want to… | Command |
|---|---|
| run the tests / lint | `make test` / `make check` |
| see where things stand (terminal) | `make status` |
| open the dashboard | `make dashboard`, then `http://localhost:8765` |
| run the bot in the foreground | `make run` |
| try a few ticks and exit | `uv run python main.py --ticks 3` |
| check the deployed contract (read-only) | `uv run python live_check.py` |
| see who trades in the pool | `uv run python pool_monitor.py --watch 300` |
| see the contract's owner, operator, balances | `uv run python bot_contract.py status` |
| try one swap by hand (dry run unless `DRY_RUN=false`) | `uv run python bot_contract.py swap fETH 0.01` |
| take funds back | `uv run python bot_contract.py withdraw-calldata f564 0.01`, then send it from MetaMask |

On a server, use the venv's Python instead of `uv run`: `source .venv/bin/activate`, then `python status.py` and so on. Service commands are in [DEPLOY 4.3](DEPLOY.md#43-everyday-commands).

## Troubleshooting

| Symptom | What to do |
|---|---|
| Browser cannot open `http://localhost:8765` | The tunnel is not up, or the dashboard is not running. Start `python dashboard.py` on the server, then `ssh -L 8765:127.0.0.1:8765 …` on your laptop and keep that window open. Port taken? Pick another local port (step 7) |
| The page says `use http://localhost through the SSH tunnel` | You opened it by the server's IP or a hostname. Only `localhost`/`127.0.0.1` are accepted, on purpose |
| The page says `no price data yet` or `the bot last read the chain … ago` | The dashboard shows what the bot wrote to `logs/state.json`. Missing: the bot has not ticked yet, or runs from another folder or machine. Old: the bot stopped, or its reads are failing (look in `logs/bot.log` for `read failed`) |
| Chart says "No price history yet" | The bot has not ticked yet (or runs from another folder). Start `main.py`; it fills in within a minute |
| Header says **status unknown** | No `logs/heartbeat`: the bot never ran here, or it runs elsewhere. Start it |
| Header says **not ticking** | No tick for 5+ minutes: the process stopped, hung, or the RPC is down (while reads fail the bot backs off for longer and longer, up to 10 minutes between ticks). `systemctl status trading-bot` and `tail logs/bot.log` |
| Header says **HALTED** | The kill switch fired. Read the reason, fix it, delete `logs/HALTED`, restart ([DEPLOY 4.5](DEPLOY.md#45-after-a-halt)) |
| Yellow box: "could not read the chain" | RPC trouble (Infura down, wrong key, rate limited). The rest of the page still works from local files |
| Saved settings do not seem to apply | Look for `settings changed` in `logs/bot.log` (it only appears when a value really changed). If you see `ignoring the settings file`, the file is invalid (the message says why). If you see nothing at all and the values were different, the bot and dashboard use different project folders |
| Nothing happens for a long time | Normal at first: strategies need 20 or more samples, then a trigger. Watch the `price=` log lines. A quiet pool may not move enough for hours; try a trend strategy or smaller thresholds, but not below the fee floor On a fresh start the pattern guard can also hold buying for about an hour: check the log for `new buys are paused`. |
| The log says `new buys are paused: …` and nothing is bought | The pattern guard (see [The pattern guard](#the-pattern-guard)). The reason is in the log line and on the dashboard's **New buys** card. It resumes by itself when the pattern is back; to trade without it set both guard limits to 0 on the dashboard |
| `ValueError … Unknown format ''` (or similar) right after starting any script | `BOT_CONTRACT_ADDRESS` is still empty: finish step 3 and paste the contract address into `.env` |
| A connection error or `401` right after starting | `RPC_URL` still holds the placeholder `…YOUR_INFURA_API_KEY`: put your real Infura URL in `.env` (step 2) |
| `PRIVATE_KEY is not the bot contract's operator` | The key in this `.env` is not the one that was set as the contract's operator. Use the same `.env` as the machine that deployed it (step 9) |
| `Not starting: logs/HALTED exists` | The bot halted itself earlier. Read the reason in that file, fix the cause, delete `logs/HALTED`, restart ([DEPLOY 4.5](DEPLOY.md#45-after-a-halt)) |
| `Address already in use` from `dashboard.py` | Another program has port 8765 on that machine. Use `python dashboard.py --port 8766` and tunnel `-L 8765:127.0.0.1:8766` |
| `buy skipped: vault balance … < trade size` | The vault holds less f564 than `MAX_TRADE_AMOUNT`. Fund it (step 4) |
| `sell skipped: vault balance …` | The vault holds less fETH than the position needs. See "Resetting a stuck position" above |
| `buy skipped: price impact too large` | The trade would move the pool price more than 2%. Lower `MAX_TRADE_AMOUNT` |
| `ignoring the settings file` in the log | `control.json` is invalid (the message says why). A running bot keeps its previous settings; a bot that *starts* while the file is invalid uses the defaults until it is fixed. Save again from the dashboard to repair it |
| Bot halts with `… ledger…json is unreadable` | The position record is damaged. Fix the file (or, if you are sure the vault holds nothing the bot bought, move it away), then **delete `logs/HALTED`** and restart ([DEPLOY 4.5](DEPLOY.md#45-after-a-halt)) |
| `buy skipped: this buy would return more base than the contract's per-swap cap` | In this pool one quote token buys more than the cap allows to sell back. Lower `MAX_TRADE_AMOUNT`, or have the owner raise the cap (`setTokenCap`) |
| Halted with `may be on chain` / `outcome is unknown` | A swap was sent but its result could not be read. Look at the bot wallet's transactions on Etherscan first, then follow [DEPLOY 4.5](DEPLOY.md#45-after-a-halt) |
| Anything about deploying, Infura, owner operations | [DEPLOY.md section 6](DEPLOY.md#6-troubleshooting) |
