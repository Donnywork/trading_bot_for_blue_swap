# Deploying the bot contract, verifying it live, and running the bot 24/7 on Ubuntu 24.04

Testnet (Sepolia) only. The RPC provider is Infura.

Contents: [0. The big picture](#0-the-big-picture) · [1. Prepare](#1-prepare) · [2. Deploy the contract](#2-deploy-the-contract) · [3. Dry-run the bot](#3-dry-run-the-bot) · [4. Run it 24/7](#4-run-it-247) · [5. Owner operations](#5-owner-operations) · [6. Troubleshooting](#6-troubleshooting)

---

## 0. The big picture

Three parties. Keep them straight and everything else follows.

```
 ENROLLED WALLET (owner)            BOT WALLET (operator)               BOT CONTRACT
 your MetaMask wallet               a NEW wallet, key in .env           deployed once
 key NEVER leaves the wallet        pays gas, sends swap() calls        holds f564 / fETH
 owner() points here                can only swap, inside the limits    swaps through the router
 can withdraw / change limits       cannot withdraw
```

**Which address is which?** Only three addresses matter, and `.env` holds just one of them as a key:

| Address | Where it comes from | In `.env` | Role |
|---|---|---|---|
| **Owner** = your enrolled / main wallet (MetaMask) | already exists; it is the one on the platform form | `OWNER_ADDRESS` (address only, **never its key**) | controls the funds: withdraws, changes limits |
| **Bot wallet** = operator | created in 1.5 | `PRIVATE_KEY` (its key; its address is derived) | deploys the contract, pays gas, calls `swap()` |
| **Bot contract** | created by `deploy` | `BOT_CONTRACT_ADDRESS` | holds the tokens |

The owner and the bot wallet must be **different** wallets (the deploy script refuses otherwise). There is no separate "wallet address" setting: the bot wallet's address is computed from `PRIVATE_KEY`.

- **Why a contract?** The platform scores a "bot contract" linked to your enrolled wallet. The contract proves the link with `owner()`, which returns your enrolled wallet.
- **Why a separate bot wallet?** Its key sits in a file on an unattended server. If it leaks the damage should be small: it can only call `swap()`, only for f564/fETH, only on fee tier 3000, and only up to `MAX_TRADE_AMOUNT` per swap. It cannot withdraw.
- **Who pays gas?** Whoever sends the transaction. The bot wallet sends the swaps, so **it** needs Sepolia ETH. The contract needs no ETH.
- **What does the contract do on its own?** Nothing. A contract only runs when called, and receiving tokens triggers no code. `main.py` decides when to trade; the contract is the vault and executor it calls.
- **What runs where?** The server runs `main.py` under systemd. The owner acts only from MetaMask on your own computer. The owner key never touches the server.

Order of work: Infura key and MetaMask → prepare the server → bot wallet and `.env` → deploy → check → (optional) Etherscan verify → link on the platform → fund → dry-run → go live → systemd.

---

## 1. Prepare

### 1.1 Get an Infura Sepolia URL

1. Sign in at <https://app.infura.io>, create an API key, and make sure the **Sepolia** network is enabled for it (menu names may change; you want the HTTPS endpoint).
2. The URL looks like `https://sepolia.infura.io/v3/<your-key>`. **The key is part of the URL, so the URL is a secret.** It goes in `.env` as `RPC_URL`, never in git, chat or screenshots. The bot also redacts it from its logs and from `logs/HALTED`.
3. Check your plan's request quota. At the default 30 s poll the bot makes about 3 requests per idle tick, so roughly 8,600 requests a day (more when it trades). Raise `POLL_SECONDS` if that is too much for your plan. `FAST_POLL_SECONDS` (default 0 = off) polls that often during seconds 30 to 56 of every minute: with `POLL_SECONDS=10` and `FAST_POLL_SECONDS=3` that is about 12 ticks a minute instead of 6, so about double the requests (roughly 52,000 a day); try it in a dry run first. The **dashboard** adds none: it shows what the bot last wrote to `logs/state.json`, so an open tab costs no quota (the bot reads the bot wallet's gas for it once every 5 minutes and after a trade). With `RISK_PER_TRADE_PCT` above 0 a buy being sized adds one `liquidity()` read, at most every 5 minutes. The dashboard's **Scan pool liquidity** button makes the bot make about 120 more read-only calls, 0.2 s apart: that tick takes 30 to 40 s longer (a stop signal ends the scan early; it is reported as failed and the button asks again). `status.py` and `pool_monitor.py` scans do call the RPC. A `429 Too Many Requests` is the provider's limit: the bot backs off and retries by itself.

### 1.2 MetaMask (your own computer)

1. Open your **enrolled wallet** and switch to the **Sepolia** network (Settings → Advanced → "Show test networks" if you don't see it).
2. Add the two tokens (*Import tokens → Custom token*):
   - f564: `0xfd840838F598d7abB153a8bc93f4a2FF38D2D5B8`
   - fETH: `0x1967a5A5f12Cbd95f12847067C2c00F044D3EF11`
3. Copy your enrolled address with MetaMask's copy button. Do not retype it: the deploy script requires an exact checksummed address and refuses typos.

### 1.3 Prepare the Ubuntu server

Any Ubuntu 24.04 machine that stays on works (a small VPS is plenty). Steps 1.3 and 1.4 are run on the server.

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git curl rsync ufw unattended-upgrades

# firewall: the bot only makes outbound connections, so allow SSH in and nothing else
sudo ufw allow OpenSSH
sudo ufw enable

# automatic security updates
sudo dpkg-reconfigure -plow unattended-upgrades

# a dedicated, login-less user for the bot
sudo adduser --disabled-password --gecos "" tradingbot

# let that user receive files over ssh/rsync/scp with the same key you log in with
sudo mkdir -p /home/tradingbot/.ssh
sudo cp ~/.ssh/authorized_keys /home/tradingbot/.ssh/
sudo chown -R tradingbot:tradingbot /home/tradingbot/.ssh
sudo chmod 700 /home/tradingbot/.ssh
```

SSH hygiene: log in with SSH keys and turn password login off, after you have **tested** that your key works (a mistake here can lock you out):

```bash
echo "PasswordAuthentication no" | sudo tee /etc/ssh/sshd_config.d/99-no-passwords.conf
sudo systemctl reload ssh
```

Now switch to the bot user and install `uv`:

```bash
sudo -iu tradingbot
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env          # puts uv on PATH for this shell
uv --version
```

Ubuntu 24.04 ships Python 3.12 but this project needs 3.13. You don't have to do anything: `uv sync` below downloads a managed Python 3.13 by itself.

### 1.4 Put the code on the server

Pick one. Both leave `.env` and `.venv` out; the secrets are created on the server in 1.5.

**A. From your computer with rsync** (works even if the project has no git remote). Run this on your computer, in the project folder:

```bash
rsync -av --exclude .venv --exclude .git --exclude .env --exclude logs \
      --exclude build --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
      ./ tradingbot@SERVER:~/bot/
```

**B. With git**, on the server: `git clone <your-repo-url> ~/bot`

Then on the server:

```bash
cd ~/bot
uv sync --frozen --no-dev         # creates ~/bot/.venv from uv.lock; no test tooling on the server
source .venv/bin/activate         # from here on, plain `python` is the project's Python
```

> Every `python …` command below is run on the server inside this activated venv, from `~/bot`. (On your own computer the same commands are `uv run python …`.)

### 1.5 Create the bot wallet and `.env` (on the server)

> **Already deployed from your laptop** (GETTING_STARTED steps 1-8)? Skip this section. The contract's operator is the bot wallet whose key is in your laptop's `.env`, so copy that `.env` (and `abis/trading_bot.json`) to the server with `scp`, `chmod 600 .env`, and use the same key there. A new key here would not be the operator and `main.py` would refuse to start (unless the owner sends `setOperator`, section 5).

This generates a brand-new key, writes it into `.env`, and prints **only the address**. The key is never shown on screen.

```bash
cp .env.example .env
python - <<'PY'
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

Copy the printed address. Never paste the key into chat, a screenshot or a commit. Do not reuse a wallet that has ever held real money.

Now edit `.env` (`nano .env`):

| Variable | Value |
|---|---|
| `RPC_URL` | your Infura Sepolia URL from 1.1 |
| `OWNER_ADDRESS` | your enrolled wallet, copied from MetaMask |
| `RISK_PER_TRADE_PCT` | optional, default `0` (off). Above 0, a buy is sized so a stop loss costs about this share of the vault's f564 (pool fee and price impact included, from the pool's liquidity: `pool_profile.json` is in the repo (the pool's layout as of 2026-11-04), scaled by one read of the pool's `liquidity()` when a buy is sized; the dashboard's "Scan pool liquidity" button makes the bot read a fresh layout into `logs/pool_profile.json`; nothing to copy by hand), never above `MAX_TRADE_AMOUNT`. The bot refuses to start with it set if that file is missing. See GETTING_STARTED, "Trade size" |
| `MAX_TRADE_AMOUNT` | size of one **buy** in human units of the token being spent (f564), e.g. `0.01`; a sell closes exactly what that buy delivered. It is also the **on-chain cap** per swap, in each token's own units |
| `DRY_RUN` | leave `true` for now |
| `BOT_CONTRACT_ADDRESS` | leave **empty**; filled in after deploying |

Leave the rest as in `.env.example`. `OWNER_ADDRESS` must not be the bot wallet's address; the script refuses that. Keep permissions tight: `chmod 600 .env`.

### 1.6 Fund the bot wallet with Sepolia ETH

Send Sepolia ETH to the bot wallet address from a faucet or from MetaMask. About **0.05 ETH** covers the deploy and many swaps. Check it arrived (this also tests your Infura URL):

```bash
python - <<'PY'
import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
addr = w3.eth.account.from_key(os.environ["PRIVATE_KEY"]).address
print("chain id:", w3.eth.chain_id, "(11155111 = Sepolia)")
print(addr, Web3.from_wei(w3.eth.get_balance(addr), "ether"), "ETH")
PY
```

---

## 2. Deploy the contract

### 2.1 Where to run it

Deploying compiles the contract with `solc 0.8.27`, which `py-solc-x` downloads on first use. Check the server's CPU:

```bash
uname -m
```

- **`x86_64`**: deploy on the server, as below.
- **`aarch64` (ARM)**: the solc download may not be available for Linux ARM. Deploy from your Mac or any x86 machine instead (same steps there with `uv run python`, using a `.env` with the same bot key), then copy `.env` and `abis/trading_bot.json` to the server. The server never needs to compile: `main.py` reads the saved ABI file.

### 2.2 Run the deploy

```bash
python bot_contract.py deploy
```

What happens, in order:

1. It connects to the RPC and checks the chain id is Sepolia (11155111). On any other network it stops.
2. It refuses to continue if `BOT_CONTRACT_ADDRESS` is already set, so you can't deploy a second contract by accident.
3. It validates `OWNER_ADDRESS` (checksummed, not zero, not the bot wallet).
4. It reads each token's decimals and converts `MAX_TRADE_AMOUNT` into smallest units: that is the per-swap cap.
5. It prints a summary:

   ```
   Deployer / operator: 0xBOT…WALLET        <- the bot wallet
   Owner:               0xOWNER…ADDR        <- your enrolled wallet
   Router:              0x3bFA…e48E
   Allowed fee tiers:   [3000]
   Allowed token:       0xfd84…D5B8 (cap per swap: 10000000000000000)
   Allowed token:       0x1967…EF11 (cap per swap: 10000000000000000)
   Deployer ETH:        0.05
   ```

   **Read it.** The owner is the one thing you can't fix without the owner's own key. If it isn't your enrolled wallet, press Ctrl+C.
6. It asks you to type `YES`. Then it compiles, deploys from the bot wallet and waits for the receipt.
7. It prints the contract address, an Etherscan link and the constructor arguments (keep them for verification).

### 2.3 Save the address

Add it to `.env`: `BOT_CONTRACT_ADDRESS=0x...`

The deploy also saved the compiled ABI to `abis/trading_bot.json`. The bot reads that file, so it matches the deployed bytecode even if the Solidity is edited later. Keep it.

### 2.4 Check it

```bash
python bot_contract.py status
```

| Line | Must be |
|---|---|
| `owner()` | your enrolled wallet |
| `operator()` | the bot wallet |
| `pending:` | `0x000…000` (no ownership transfer in progress) |
| `fee 3000 allowed:` | `True` |
| each token's `cap per swap` | your `MAX_TRADE_AMOUNT` |
| last line | no `WARNING` |

Also open the Etherscan link and confirm the contract is there. If `owner()` is wrong, do not use this contract: clear `BOT_CONTRACT_ADDRESS` and deploy again (the cost is a little testnet gas).

### 2.5 (Optional) Verify the source on Etherscan

Verification makes the source readable and unlocks Etherscan's **Write Contract** tab, the easiest way for the owner to withdraw.

```bash
python bot_contract.py verify-input
```

This writes `build/TradingBot.standard-input.json` (download it to your computer with `scp`). On the contract's Etherscan page: *Contract → Verify and Publish*:

1. Compiler type: **Solidity (Standard-Json-Input)**.
2. Compiler version: **v0.8.27** (the entry with a commit hash).
3. License: MIT.
4. Upload the JSON file.
5. Paste the constructor arguments printed by `deploy` (hex, without a leading `0x`).

The script compiled and deployed from this same file, so the bytecode should match. A verified contract shows "Source Code Verified" and the **Read Contract** / **Write Contract** tabs on Etherscan. If you lost the constructor arguments, they are the end of the deployment transaction's input data: the part after the contract's creation bytecode.

### 2.6 Link it on the platform

On the form: Address type **Bot contract**, "Additional address" = your `BOT_CONTRACT_ADDRESS`, then approve from the enrolled wallet in MetaMask. The platform reads `owner()` to confirm the contract is yours. Whether it also counts this contract's trades toward your score is something to confirm with them.

### 2.7 Fund the contract with tokens

In MetaMask (enrolled wallet, Sepolia): *Send → f564 (or fETH) → paste the contract address → amount → Confirm*. Repeat for the other token. Start with enough for several swaps of `MAX_TRADE_AMOUNT`. Then:

```bash
python bot_contract.py status     # the token balances should show your amounts
```

Do **not** send ETH to the contract; it needs none.

### 2.8 Live check of the deployed contract (read-only)

From here on you are checking the contract **you deployed on Sepolia**, from the command line. Cheapest and safest first: nothing in 2.8 to 2.10 sends a transaction or spends gas.

```bash
python live_check.py
```

It reads the real chain and tests the real contract (the guard tests are `eth_call`s from the wrong caller or with a disallowed token, which must be rejected):

| Check | Proves |
|---|---|
| a contract exists at `BOT_CONTRACT_ADDRESS` | the address and network are right |
| code matches `contracts/TradingBot.sol` | you deployed this reviewed version, not an older one |
| `owner()` is `OWNER_ADDRESS`, `operator()` is the `PRIVATE_KEY` wallet, no ownership transfer pending | the roles are right |
| `router()` is `ROUTER_ADDRESS`, fee tier allowed | it is wired to the real router and pool |
| each token's cap equals `MAX_TRADE_AMOUNT` | the on-chain limits are what you expect |
| operator has gas | it can send transactions |
| a stranger cannot swap; the operator cannot swap into an unlisted token, use another fee tier, exceed the cap, or set `minOut` to 0; the operator cannot withdraw, change the operator or raise a cap | the protections really hold on the live contract |
| simulated swap in each direction | the whole path works against the real pool and router |

Each line is `[PASS]`, `[FAIL]`, `[WARN]` or `[SKIP]`, followed by a summary. Any `FAIL` makes the command exit with status 1: fix it before going on. `WARN` means "look at this" (for example you changed a cap, or the code comparison could not run because solc is unavailable on this machine). `SKIP simulated swap …: fund the contract` means the contract doesn't hold that token yet (2.7).

### 2.9 Dry-run swaps (both directions)

With `DRY_RUN=true`:

```bash
python bot_contract.py swap fETH 0.01
python bot_contract.py swap f564 0.01
```

Each asks the real Uniswap quoter for a price, then simulates the swap through your contract with `eth_call` (free, nothing is sent). Expect a quote report, `Simulated amountOut: …`, then `DRY_RUN=true: not sending`. If the simulation reverts, the message says why (see section 6).

### 2.10 Run the bot loop for a few ticks (dry run)

```bash
python main.py --ticks 3
```

This runs the real bot loop three times against your real contract and exits by itself. Expect `Started. … dry_run=True`, three `price=… decision=hold …` lines, then `Stopped.`. It proves the Infura reads, the contract balances, the operator check and the logging all work. `decision=hold` is correct here: the strategy needs 20 samples. The bot wallet must show **no** new transactions on Etherscan.

### 2.11 Live swaps, both directions (tiny)

Set `DRY_RUN=false` in `.env`, then:

```bash
python bot_contract.py swap fETH 0.01
python bot_contract.py swap f564 0.01
```

Each prints the transaction hash, an Etherscan link, the block, gas used, and the vault's balances before and after, for example `Vault fETH: 0.5 -> 0.49` and `Vault f564: 100 -> 100.0105…`. The input token must go down by the amount and the output token up. Then set `DRY_RUN=true` again and re-run `python live_check.py`: with both tokens now in the vault there should be no `SKIP`.

### 2.12 The owner path: take a little back

```bash
python bot_contract.py withdraw-calldata f564 0.01
```

Send the printed calldata from your **enrolled wallet** in MetaMask (section 5), or use Etherscan's Write Contract tab. Confirm your wallet's f564 balance went up and `python bot_contract.py status` shows the vault's went down. This proves `owner()` is the wallet you think it is and that only it can pull funds out.

### 2.13 What "working" means

| Evidence | Where |
|---|---|
| `live_check.py`: 0 failed, every WARN understood | 2.8 |
| quote and simulated swap work in both directions | 2.9 |
| `main.py --ticks 3` reads the chain and sends nothing | 2.10 |
| live swaps move the vault's balances the right way | 2.11 |
| the enrolled wallet receives a withdrawal | 2.12 |
| the platform accepts the contract on the form (and you've asked them whether its trades count) | 2.6 |

Then do the one-hour dry run in section 3, and section 4.

---

## 3. Dry-run the bot

### 3.1 What `main.py` does

Every `POLL_SECONDS` (default 30, or `FAST_POLL_SECONDS` during seconds 30 to 56 of each minute if set): reload `logs/control.json` if it changed → read the pool price and the vault's balances → ask `strategy.decide()` for `buy`, `sell` or `hold` → on a signal, check the cooldown, the vault balance, the operator's gas and the price impact, then swap through the contract → record the position in `logs/ledger.json`.

While the bot holds nothing, the chosen **strategy** decides when to buy. Once it holds something it bought, only the **take profit** and **stop loss** (and the **time stop**, if ticked on the dashboard, or the dashboard's **Sell everything** button) decide when to sell it, and it sells exactly what it bought. Both are set on the dashboard (see [GETTING_STARTED.md](GETTING_STARTED.md)); a dry run keeps its own paper ledger (`logs/ledger_dryrun.json`).

| Situation | What the bot does |
|---|---|
| Infura unreachable or rate limited | logs a warning, waits longer each time (up to 10 min), keeps going |
| A swap fails 3 times in a row (`MAX_TRADE_FAILURES`) | **halts**: writes `logs/HALTED` and exits cleanly |
| No receipt before the timeout, or the connection drops while sending | **halts** at once: the swap may still land, so it won't guess or resend |
| Operator ETH below `MIN_OPERATOR_ETH` | skips trading and warns until you refill it |
| Cooldown (`COOLDOWN_SECONDS`) since the last trade | skips a new buy; an exit right after a successful buy is not delayed, one after a failed attempt waits |
| `logs/ledger*.json` unreadable | **halts**: it will not guess whether it holds a position |
| `logs/control.json` invalid | keeps the settings it had and logs `ignoring the settings file` |
| SIGTERM / Ctrl+C | finishes the current tick (even a swap waiting for its receipt), then exits |
| Started while `logs/HALTED` exists | refuses to start and prints the reason |
| Unhandled error | logs it (secrets redacted), exits non-zero so systemd restarts it |

A halted bot exits with status 0 on purpose, so systemd does **not** restart it. After you fix the cause, delete `logs/HALTED`.

> **The strategies are placeholders.** `strategy.py` has six simple entry rules (mean reversion, Bollinger bands, RSI, sharp drop, breakout, moving-average cross) so the loop can run end to end and you can compare styles. The default needs 20 price samples (about 10 minutes at the default poll) and a 2% dip, and a small testnet pool may rarely move that much. Replace or add your own and unit-test it. Fees are 0.3% each way, so keep thresholds well above 0.6%. The take profit cannot be set below 1% for that reason.

### 3.2 Run it in the foreground

```bash
# .env: DRY_RUN=true
python main.py
```

Healthy output:

```
INFO Started. contract=0x… operator=0x… dry_run=True
INFO poll=30s cooldown=60s trade size=0.01 min ETH=0.01
INFO strategy=mean_reversion take profit +2.5% stop loss -4% (change it on the dashboard or in control.json)
WARNING new buys are paused: seen only 0 of the 8 big price steps needed to judge the pattern
INFO strategy=mean_reversion price=1054.267260 decision=hold base=0.500000 quote=100.000000 samples=1
```

The `WARNING` is the pattern guard (see [GETTING_STARTED, the pattern guard](GETTING_STARTED.md#the-pattern-guard)); it is normal on a fresh start.

Let it run an hour or more. On Sepolia Etherscan the **bot wallet must show zero new transactions** (a dry run sends nothing). Ctrl+C should print `Stopped.`.

Logs go to the screen and to `logs/bot.log` (rotated at 5 MB, 3 old files kept). `logs/heartbeat` holds the time of the last completed tick. A paper position, once opened, shows as `opened position: …` and later `closed position (take_profit) …`; `python status.py` summarises them.

### 3.3 Going live

Set `DRY_RUN=false` with a small `MAX_TRADE_AMOUNT`, restart the bot **and the dashboard** (it reads `DRY_RUN` at start-up), and watch the first trades in `logs/bot.log`, on the dashboard and on Etherscan before leaving it alone. Live trades use their own ledger (`logs/ledger.json`); dry-run results stay in `logs/ledger_dryrun.json`.

---

## 4. Run it 24/7

### Do you need a service?

No. The bot is already a loop that sleeps between checks (`Runner.run` in `main.py`); a service doesn't replace that loop. It only **babysits the process**: something has to restart it if it crashes, start it after a reboot, and keep it alive when you close your SSH session. Your options:

| How | Survives closing SSH | Restarts after a crash | Starts after a reboot |
|---|---|---|---|
| `python main.py` in a terminal | no | no | no |
| `tmux` (or `screen`) | yes | no | no |
| `nohup python main.py &` | yes | no | no |
| **systemd** (already on Ubuntu 24.04) | yes | yes | yes |

- **Short runs and testing: use tmux.** It is the simplest and is enough while you are still watching the bot:

  ```bash
  sudo apt install -y tmux
  tmux new -s bot
  cd ~/bot && source .venv/bin/activate && python main.py
  # detach with Ctrl+b then d; come back with: tmux attach -t bot
  # stop it: attach, then Ctrl+C (it finishes the current tick and exits)
  ```

- **Leaving it alone for days: use systemd.** With tmux a crash, an out-of-memory kill or a server reboot leaves the bot silently dead until you notice. systemd is not something extra to install: it is Ubuntu's init system, and this is one 30-line file and three commands.

The rest of this section is the systemd setup. The `logs/HALTED` kill switch works the same either way.

### 4.1 Install the service

As your admin user (not `tradingbot`):

```bash
sed -e 's|__USER__|tradingbot|' \
    -e 's|__PROJECT_DIR__|/home/tradingbot/bot|' \
    /home/tradingbot/bot/deploy/trading-bot.service \
  | sudo tee /etc/systemd/system/trading-bot.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot       # start now and at every boot
```

The unit runs `/home/tradingbot/bot/.venv/bin/python main.py` directly, so a restart needs no network and never re-syncs packages.

> The systemd unit has not been run end to end on a real Ubuntu server. Do the first run with `DRY_RUN=true` and check `systemctl status` and the log before going live.

### 4.2 Restart rules (in the unit)

- **Crash** (non-zero exit): restarted after 30 s. After 5 crashes within 10 minutes systemd stops trying and shows `failed`.
- **Clean stop or kill-switch halt** (exit 0): not restarted.
- **Stop** (`systemctl stop`): sends SIGTERM and gives the bot up to 4 minutes to finish an in-flight swap before SIGKILL.

### 4.3 Everyday commands

```bash
systemctl status trading-bot                  # running? last log lines
journalctl -u trading-bot -f                  # live log (also in ~/bot/logs/bot.log)
journalctl -u trading-bot --since "1 hour ago"
sudo systemctl restart trading-bot
sudo systemctl stop trading-bot               # graceful
systemctl status trading-bot-dashboard        # the optional dashboard service (4.4)
sudo systemctl restart trading-bot-dashboard  # after code updates or editing .env
sudo systemctl disable trading-bot            # don't start at boot
```

### 4.4 Monitoring (is it alive and healthy?)

As the `tradingbot` user, in `~/bot`:

```bash
tail -f logs/bot.log                           # what it is doing
cat logs/heartbeat                             # time of the last completed tick
cat logs/HALTED 2>/dev/null                    # exists only if the kill switch fired
find logs/heartbeat -mmin +5 | grep -q . && echo "STALE: bot not ticking"
```

`python bot_contract.py status` also prints the operator wallet's ETH. When it drops below `MIN_OPERATOR_ETH` the bot stops trading and warns in the log, so top it up.

For the bot's own position and profit and loss, run `python status.py` (read-only, needs `RPC_URL`, `POOL_ADDRESS` and `BOT_CONTRACT_ADDRESS`, no private key). The same, with a chart and a form to change strategy and take profit / stop loss, is the **dashboard**:

```bash
python dashboard.py                    # listens on 127.0.0.1:8765 only
# from your laptop:  ssh -L 8765:127.0.0.1:8765 user@server    then open http://localhost:8765
```

Never open its port in the firewall: it has no login and can change the bot's strategy. It refuses requests whose `Host` or `Origin` is not `localhost`/`127.0.0.1`, and it can never change `DRY_RUN`, the trade size, an address or a key. To keep it running, install `deploy/trading-bot-dashboard.service` the same way as the bot's unit (4.1); it must run as the same user as the bot. Restart it after changing `DRY_RUN` in `.env`. Full tour: [GETTING_STARTED.md](GETTING_STARTED.md).

To see what the *pool* is doing (price, how many other bots are trading, who is new), use the read-only pool monitor. It needs only `RPC_URL` and `POOL_ADDRESS` (no private key) and sends nothing:

```bash
python pool_monitor.py                     # last 24h, once
python pool_monitor.py --hours 6           # a shorter window
python pool_monitor.py --watch 300         # refresh every 5 minutes, announce new actors
```

An "actor" is a swap recipient (the vault or wallet that receives the tokens). Each row shows its swaps, buys, sells, net base token, how often it trades and when it last did. `scripted?` marks an actor whose gaps between swaps are very regular, `YOU` marks `BOT_CONTRACT_ADDRESS`, and `NEW` marks an address not seen in an earlier run (the first run only records a baseline). Seen addresses are kept in `logs/monitor_seen.json`. It reads Swap logs in chunks, so a long window costs more RPC quota; with Infura prefer `--hours 24` or less.

### 4.5 After a halt

1. `cat ~/bot/logs/HALTED` and read the reason.
2. If it mentions "no receipt" or "may have been sent", look at the bot wallet's pending transactions on Etherscan first, and only continue when none is pending.
3. If a **buy** may have landed before the halt, the vault can hold base the bot has no record of (the position is written right after the trade is confirmed). Compare `python bot_contract.py status` with `python status.py`; base the ledger does not know about is never sold by the bot, so sell it by hand or withdraw it (section 5).
4. Fix the cause, then `rm ~/bot/logs/HALTED` and `sudo systemctl restart trading-bot`.

### 4.6 Updating

```bash
sudo systemctl stop trading-bot
# on your computer: rsync as in 1.4 (or `git pull` on the server), then on the server:
cd ~/bot && uv sync --frozen --no-dev
sudo systemctl start trading-bot
sudo systemctl restart trading-bot-dashboard   # if you run it: it loads the strategy list at start-up
```

Run `uv run pytest` on your own computer before copying. A change to `strategy.py` needs only the copy and a restart. Your dashboard settings (`logs/control.json`), the position and history (`logs/ledger*.json`) and `logs/prices.csv` live in `logs/` and survive an update; do not `rsync --delete` over `logs/`. A change to the Solidity means a **new contract** (a deployed contract can't be edited): clear `BOT_CONTRACT_ADDRESS`, deploy, `withdraw` from the old one, re-fund, and re-link on the platform.

### 4.6b Safety on an unattended server

- The key in `.env` only controls the testnet bot wallet and its limited operator role. Keep `.env` at `chmod 600` and owned by `tradingbot`.
- SSH keys only, firewall on, automatic security updates (1.3).
- Never put the **owner's** key on the server. The owner acts from MetaMask (section 5).
- Mainnet is out of scope: it would need audited limits, private transaction submission, a hardened key setup and real backtesting.

---

## 5. Owner operations

The owner key is not in the bot, so these are done from your enrolled wallet. Two ways:

- **Etherscan "Write Contract"** (needs 2.5): connect MetaMask as the enrolled wallet and call the function.
- **Calldata**: the script prints `to` and `data` to send. In MetaMask enable *Settings → Advanced → Show hex data*, then send a transaction with value 0 to the contract with that data.

| Task | Command (prints calldata) |
|---|---|
| Withdraw tokens to yourself | `python bot_contract.py withdraw-calldata fETH 0.5` (or `all`). Stop the bot, or check that `status.py` shows no open position, first. If one is open, withdraw no more than the vault's balance minus the position's base, never `all`: the bot sells exactly what it recorded and cannot close a position the vault no longer covers |
| Change a token's per-swap cap (no redeploy: it is a parameter, not code) | `python bot_contract.py calldata setTokenCap 0xTOKEN RAW_AMOUNT` (raw smallest units: `20000000000000000` is 0.02 with 18 decimals, `2000000000000000000000` is 2,000; `0` removes the token). Raise it for **both** tokens when you raise the size: the sell trades the base a buy returned, and a buy that could return more base than the base token's cap is skipped. Then raise `MAX_TRADE_AMOUNT` in `.env` to match and restart the bot |
| Allow another fee tier | `python bot_contract.py calldata setFeeAllowed 500 true` |
| Replace a leaked or retired bot key | `python bot_contract.py calldata setOperator 0xNEW_BOT_WALLET` |
| Disable the bot instantly | `python bot_contract.py calldata setOperator 0x0000000000000000000000000000000000000000` |
| Hand over ownership | `python bot_contract.py calldata transferOwnership 0xNEW`, then the new owner sends `acceptOwnership()` |

Ownership transfer is two steps on purpose: nothing changes until the new address accepts, so a typo can't lock your funds.

If the bot key leaks: disable or replace the operator, withdraw the funds, and create a new bot wallet (1.5). If the **Infura URL** leaks, create a new key in the Infura dashboard, update `RPC_URL`, and delete the old key.

---

## 6. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `OWNER_ADDRESS … not a valid checksummed address` | Retyped or lowercased. Copy it again from MetaMask |
| `OWNER_ADDRESS is the bot wallet` | The bot's address is in the owner field. Use your enrolled wallet |
| `BOT_CONTRACT_ADDRESS is already set` | Deploy refuses to run twice. Clear the variable to deploy a new one |
| `insufficient funds` on deploy or swap | The bot wallet needs more Sepolia ETH |
| `chain id` is not 11155111 | `RPC_URL` points at the wrong network. Use the Infura **Sepolia** URL |
| `401` / `403` from Infura | Wrong or disabled API key, or Sepolia not enabled for it |
| `429` from Infura | Over your plan's quota. Raise `POLL_SECONDS` |
| solc download fails on deploy | Likely an ARM server: deploy from an x86 machine (2.1) |
| `No contract at 0x…` | Wrong `BOT_CONTRACT_ADDRESS`, wrong network, or the deploy didn't confirm |
| `WARNING: PRIVATE_KEY is not the contract's operator` | `.env` holds a different key than the one that deployed. Fix `.env`, or have the owner call `setOperator` |
| Simulation reverts `not operator` | The caller is neither operator nor owner |
| Reverts `tokenIn / tokenOut not allowed`, `fee not allowed` | Not on the allowlist. The owner can add it (section 5) |
| Reverts `amountIn out of range` | Over the per-swap cap, or zero. Lower `MAX_TRADE_AMOUNT` or raise the cap |
| Reverts `STF` | The contract doesn't hold enough of the input token. Fund it (2.7) |
| Reverts `Too little received` | Price moved or the impact exceeded slippage (1%). Retry, or trade smaller |
| `Amount too small: the minimum output rounds to 0` | Dust amount. Use a larger `MAX_TRADE_AMOUNT` |
| Log shows `read failed` repeatedly | Infura unreachable or rate limited. The bot backs off by itself; check the Infura dashboard |
| `Not starting: logs/HALTED exists` | The kill switch fired. Read the reason, fix it, delete the file (4.5) |
| Dry run halts after failed trades | The simulation reverts (see the reason above). A dry run needs the contract funded and the key to be the operator |
| `systemctl status` shows `failed` | 5 crashes in 10 minutes. Read `journalctl -u trading-bot`, fix, then `sudo systemctl reset-failed trading-bot && sudo systemctl start trading-bot` |
| `live_check.py`: `FAIL code matches` | The deployed contract is not this version of the source. Redeploy (part 2) |
| `live_check.py`: `FAIL operator()` / `owner()` / `router()` | `.env` doesn't match the contract. Fix `.env`, or have the owner call `setOperator` |
| `live_check.py`: `FAIL … the call SUCCEEDED but must be rejected` | A protection is missing on the deployed contract. Do not fund it; redeploy |
| `live_check.py`: `WARN … rejected, but not with …` | It was refused, but the node didn't return the reason text. Usually harmless; check the line's detail |
| `live_check.py`: `WARN could not compile` | solc isn't available on this machine (for example ARM). Run the check from an x86 machine if you want the code comparison |
| `live_check.py`: `SKIP simulated swap … fund the contract` | The contract has none of that token yet (2.7) |
| Nothing happens for a long time | Normal at first: a strategy needs its samples (20 by default), then a trigger. Check the `price=` lines in the log, and try another strategy on the dashboard |
| Dashboard unreachable from the laptop | Is `dashboard.py` running on the server, and is the `ssh -L 8765:127.0.0.1:8765 …` window open? Open `http://localhost:8765`, not the server's address |
| Dashboard says `use http://localhost through the SSH tunnel` | You used a hostname or IP. Only `localhost` / `127.0.0.1` are accepted (any port) |
| `ignoring the settings file` in the log | `logs/control.json` is invalid (the message says why). A running bot keeps its previous settings; a bot that starts with an invalid file uses the defaults. Save again from the dashboard, or fix the file (deleting it only takes effect at the next restart) |
| `sell skipped: vault balance … < trade size` every tick | The ledger says the bot holds base that the vault no longer has. See GETTING_STARTED, "Resetting a stuck position" |
| Bot halts with `… ledger…json is unreadable` | The position record is damaged. If you are sure the vault holds nothing the bot bought, move the file away; otherwise fix it. Then `rm logs/HALTED` and restart (4.5): the halt wrote that file |
| Halted with `may be on chain` / `outcome is unknown` | A swap was sent but its receipt could not be read. Check the bot wallet's transactions on Etherscan before anything else (4.5) |
| `buy skipped: … more base than the contract's per-swap cap` | A buy could return more base than one swap may trade, so the later sell would be refused. Lower `MAX_TRADE_AMOUNT` or raise the cap (section 5) |
