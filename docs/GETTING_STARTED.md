# Getting started

### Phase 3 observation

Discord swap alerts show the trader’s buy or sell, the exchanged amounts, the resulting pool price and a receipt link. The displayed fee is the total pool fee.

Run `.venv/bin/python phase3_lp_paper.py status` on the monitor host for the forward comparison of full-range and concentrated liquidity. `status: unavailable` means the watcher could not validate the latest comparison. Check the monitor's freshness and its journal before interpreting results. [Paper experiment](PHASE3_SPOT.md#forward-liquidity-experiment)

For spot trading, fund and configure the registered trading vault, then check the `spot.status` field in `logs/phase3/auto.json`. The suggested buy cap is 1,000 f564 and the suggested sell cap is 1.5 fETH. [Wallet calls and strategy rules](PHASE3_SPOT.md)

For automatic trading, follow [automatic liquidation setup](PHASE3_VAULT.md#automatic-liquidations). `logs/phase3/auto.json` reports the live worker's status, latest observed block, borrower count and owner caps.

For the credit vault, follow the [Phase 3 owner walkthrough](PHASE3_VAULT.md). It identifies the wallet for each action, the two Phase 3 tokens and the owner settings for borrowing and liquidation.

After `uv sync`, run `uv run python phase3_monitor.py --once` for one snapshot or omit `--once` for continuous monitoring. No credentials or `.env` are needed. Inspect `logs/phase3/state.json` for the indexed block, market snapshot block, f564 per fETH spot and TWAP, credit utilization and borrower health. `events.sqlite3` holds event amounts in raw token units, transaction hashes and pool/credit transaction signers. A `degraded` status preserves the last successful market snapshot with its block number. Use the [server runbook](DEPLOY.md#phase-3-monitor) for Discord and restart instructions.

**Use [the Phase 2 operator walkthrough](EVENT_CONTRACT_SETUP.md) for the current bot.** Follow it in order: install and observe in paper mode, prepare one Phase2TradingBot, verify/register/fund it, simulate, then deliberately enable live trading. It contains the complete commands and wallet/limit examples. For Ubuntu service installation, continue with [DEPLOY](DEPLOY.md#phase-2-on-ubuntu).

### Phase 2 command reference

| Task | Command / location |
|---|---|
| Start Phase 2 | `uv run python main.py` or `make run` |
| Bounded observation | `uv run python main.py --seconds 1200` |
| Dashboard | `make dashboard`, then open `http://127.0.0.1:8765` |
| Unsigned preparation, deployment, owner actions | [Operator walkthrough](EVENT_CONTRACT_SETUP.md) |
| Enable entries | Verify the active mode, then **Start Trading** |
| Switch mode | Click the single **Mode: …** toggle, wait for acknowledgement, then **Start Trading** |
| Pause entries | **Stop Trading** keeps monitoring, receipt checks and exits running |
| Saved event status and execution records | `logs/events/`; retain across restarts |
| Phase 2 price charts | Both pools are stacked on the main page; select one shared time window. Units are f564 per fETH. |
| Scan / exit | Separate **Maintenance and position actions** area |
| Decision pages | Expand **Recent decisions and watched actors**, scroll the table, then use Previous/Next |
| Legacy polling recovery | `http://127.0.0.1:8765/legacy`; controls apply to `legacy_main.py` only |
| Runtime ABI | `abis/phase2_trading_bot.json`, included in the checkout; no runtime build needed |
| Competitive fee opt-in | Operator configuration: `EVENT_FEE_POLICY=competitive`; [units, controls and example](EVENT_FEES.md#configuration) |
| Fee and submission evidence | Expand **My execution results → Latest fee and submission evidence**; full records remain in `logs/events/` |

### Phase 2 troubleshooting

| Message / symptom | Next step |
|---|---|
| `awaiting_scan` | Click **Scan Pool Liquidity**. A failed scan requires another click. |
| `Cannot read abis/phase2_trading_bot.json` | Restore the tracked ABI from the repository. Runtime and owner calldata need no local build artifact. Older versions reporting `Vault checks failed: FileNotFoundError` should update the checkout. |
| `eth_getLogs` fails during startup | Catch-up narrows HTTP 400/413/422 or specifically identified JSON-RPC range/result-size failures. JSON-RPC quota/authentication errors preserve the range and back off. For 401/403, check provider access; for 429, check request limits and remaining quota. |
| `Reconcile …/HALTED before starting a live sender` | Reconcile the indicated service's transaction outcome before clearing its halt marker. Both services' markers block all local live senders. |
| `inventory or an unresolved intent` | Resume/reconcile the service that owns the position. Manual swaps/deployments require both ledgers flat. |
| `cannot verify … execution ledger` | Restore/reconcile the unreadable record; do not delete it to bypass the check. |
| `another local service holds the operator lock` | Stop the other local sender and let its active operation finish. Deleting the lock file is not a handoff. |
| No simulated execution | Check the Phase 2 panel's entry pause and module switches. `prepared` is only a model result; `simulated` confirms execution checks. |
| Feed connected / snapshot ready, but entries paused | Read the entry explanation. After selecting a mode, wait for acknowledgement, then click **Start Trading**. An older Start request cannot authorize the new mode. |
| Rejected or unprepared target | The latest evaluation shows the rejection stage/reason. `unprepared` means successful pending preparation is missing; it is not proof that a profitable trade was missed. Older records may only contain a generic rejection. |
| No opportunity | The enabled strategy found no eligible route under its caps and profit floor at the sampled state. It does not prove that no opportunity existed between samples. Backruns wait for target inclusion; independent cross-pool arbitrage is separately enabled. |
| `execution snapshot is no longer current` / `quote block reorganized` | Validation stopped before sending because its pinned state changed. Check validation timing and the next block's activity. Raising the fee does not shorten quoting. |
| `execution reverted: SPL` during validation | The pool rejected the sqrt-price limit. Compare the quote block with the simulation/estimate state; a price move can invalidate an older limit. This is not proof of an on-chain revert: check broadcast timing and the receipt. Current cycle validation uses one pinned quote/simulation context and one gas estimate. |
| Empty price chart after upgrade | Restart bot and dashboard using the normal handoff. History starts with new reconciled block observations; it is not reconstructed from the legacy CSV. Select another range if needed. |
| Stale price or gaps | The chart labels prices older than 30 seconds. Gaps over 60 seconds, restarts and mode changes break the line. Check feed and snapshot freshness; the dashboard never refreshes the chain itself. |
| Price history unreadable | Inspect `history_error` in event status and preserve the damaged file for diagnosis. Chart storage failures do not halt exits or reset trading records. |
| Mode says “Switching…” | The file write succeeded but the runtime has not acknowledged it. Wait or inspect the process; Start stays disabled. A refusal retains the actual mode and shows its reason. |
| Start disabled / stale runtime | Restore fresh runtime status before starting. **Stop Trading** remains available to request a pause when the control file is usable. |
| Holdings stale or read error | The last successful observation retains its block/time and balances. Inspect `observation_error` and RPC diagnostics. No unavailable balance is replaced with zero. |
| Awaiting scan, but holdings available | Holdings diagnostics continue on their bounded schedule using pinned balances and the primary spot price. A full liquidity scan is still required for entries. |
| Prices or entry references withheld after recovery | Invalidated prices stay excluded even after a higher-block scan succeeds. An open-entry reference requires verified execution and activity; its recorded position remains visible for reconciliation. |
| Missing transaction markers | Coverage begins with recorded logs/receipts; signer/time enrichment is bounded. Unverified or reorganized markers are withheld. Inspect the transaction table, which also lists swaps without a known time. |
| Portfolio return unavailable | No valid portfolio baseline exists. Do not use deposits, recent trades, or a restart as invented starting capital. |
| Deadline cannot cover inclusion | Preparation consumed the swap's inclusion margin. This is a pre-send refusal; no transaction was broadcast for this attempt. Review validation timing and RPC delays. |
| `configured fee ceiling is too low` | Inspect `execution_attempt.fee_decision` and its reference base fee. Fixed mode needs max ≥ base + fixed priority; competitive mode needs room for a positive tip under its own ceilings. The attempt was not signed. |
| `network_missing`, `network_stale`, `network_zero_samples` | Competitive mode uses its baseline and any eligible competitor evidence, with a fresh base-fee check. Inspect the fee-refresh RPC category/error; stale evidence is excluded. |
| `priority_ceiling` / `max_fee_ceiling_reduced_tip` | The selected bid is below the desired bid. Read both fields; do not interpret the desired bid as submitted or as proof of outbidding. |
| Provider accepted, inclusion unavailable | RPC acceptance is not a receipt. An uncertain outcome halts for reconciliation; never replace or resubmit it through this policy. |

Each start pauses entries and selects Simulation with a vault or Paper without one. Paper displays modeled candidates. Simulation runs the existing read-only contract execution checks. Live requires `DRY_RUN=false` in operator configuration plus a valid vault, signer and limits. `EVENT_EXECUTION_MODE` is accepted for compatibility and cannot enable new live entries. A recorded live position resumes exit management and needs a fresh Live request once flat. The dashboard displays a refusal reason if a switch is blocked. Select the desired mode again after resolving it.

Before recovery can exit a recorded position, the runtime validates retained execution receipt block hashes, including receipts older than 64 blocks. The entry backing an open position is checked every maintenance pass, even if its trade row was trimmed. An unavailable or missing block read defers execution; a successfully fetched block with a changed hash halts for reconciliation. Do not clear or reset the position to bypass this check.

### Phase 2 dashboard operation

Fees remain fixed unless the operator opts in. The [competitive example](EVENT_FEES.md) uses a 20 gwei baseline, 1.25× network and 1.5× competitor multipliers, and 100/200 gwei priority/total ceilings. All fee settings use integer wei or basis points; the old `EVENT_PRIORITY_FEE_WEI` / `EVENT_MAX_FEE_WEI` fields remain fixed-mode bids only. Exits receive the same baseline when feasible, even while entries are paused. Changing fee configuration requires the ordinary operator service handoff, not a dashboard control. These experimental bids cannot guarantee order or profit.

The primary row contains exactly two buttons. **Mode: …** displays the runtime's acknowledged mode and requests the other mode; every switch pauses entries. **Start Trading / Stop Trading** reflects applied permission to open new trades, including while the bot is idle. A saved request is shown as pending until the runtime acknowledges it. Start is blocked for unknown/stale state and pending or refused mode requests. Stop continues to request a pause with stale status. Paper and unavailable modes explain their limitations. Strategy switches, scan and position-exit actions are separate from this row.

The **0.05% primary pool is above the 1% secondary pool**. Both remain visible with independent price axes unless Shared price scale is selected. Hover for exact recorded time/price and a synchronized time cursor. Wheel/trackpad zoom is centered on the pointer; drag pans both charts. On touch devices, drag horizontally or pinch; keyboard users can focus a chart and use arrows or +/−. Presets offer 15 minutes, 1 hour, 6 hours, 24 hours and all retained history. Zoom, pan, presets and Reset enter historical inspection; refreshes preserve the selected window. **Follow latest** explicitly resumes live scrolling. Accessible price and paginated transaction tables are below the charts.

Prices come from reconciled snapshots. Gaps over 60 seconds, process restarts and mode changes break lines. These samples do not capture every intra-block move. Transaction markers use normalized confirmed swap amounts: triangles for your buys/sells, diamonds for market bots, squares for competitors; quieter other/unknown actors are optional. Watched categories use configured `EVENT_MARKET_BOTS` / `EVENT_COMPETITORS` transaction signers, never a shared router's pool-event sender. Hover, focus or select an annotation for full addresses, amounts, execution/post-swap prices, exact block/transaction/log order and transaction links. Multiple swaps retain distinct log identities, and grouped glyphs expose all constituent swaps. Entry/exit references share a position ID across pools; atomic-cycle legs share a transaction hash. Pending, reverted, not-sent and simulated attempts remain in the execution/decision views and never become confirmed chart markers.

**Portfolio** shows actual contract balances including deposits, in every execution mode. Both token balances and the primary-pool price are pinned to one block and checked against its hash. fETH marked value = fETH balance × f564-per-fETH price; total = f564 balance + marked fETH value. Operator native ETH is a separate Gas wallet. Simulation does not change actual holdings. The open position records only bot-acquired fETH and its acquisition cost; deposited fETH is separate. Its exits follow target nonce inclusion, the configured event time limit or an explicit exit request, with the runtime cooldown after failures.

**Realized trading P/L** sums confirmed exit proceeds minus recorded position cost, plus confirmed atomic-cycle proceeds minus input. It covers the retained live execution ledger (at most 200 attempts), with dates and missing-history labels; it is not lifetime performance. A reconciled row missing its timestamp still contributes valid profit and gas amounts; the date range is unavailable and the missing timestamp count is shown. Verified flat inventory shows “No open position,” without a stale-mark warning. **Unrealized P/L** is recorded position quantity × the current pinned spot price minus recorded cost, with stale marks labeled. **Gas spent** sums confirmed mined execution costs, including recorded reverts, in native ETH; older missing gas records make the total unavailable. Simulated attempts have no confirmed P/L. No whole-portfolio baseline or reconciled external-flow ledger currently exists, so portfolio change/return reads **“Unavailable — baseline not established.”** Deposits/withdrawals affect holdings, never trading P/L. No flow-adjusted return is claimed. Spot valuation excludes liquidation fees, slippage and impact; trading P/L and competition portfolio return measure different things.

Price history is capped at 5 MB; transaction history at 4,000 rows and 5 MB. Trimming and canonical verification status are disclosed. No historical backfill is started. Unknown signer/time metadata stays explicit, and corrupt history is preserved for diagnosis. Full snapshot catch-up defers while a position is open, so chart spot samples can gap while lightweight holdings and receipt annotations continue. The decision section retains the latest 30 session decisions, paginated ten per page in a scrollable table. Later pages hold their contents during refreshes until you return to page 1.

Startup/recovery rechecks the retained price anchor. Snapshot validation failure or removed logs conservatively mark earlier observations invalid; they remain in the bounded file for diagnosis but are excluded from chart lines, latest-price labels and tables. New canonical observations resume without connecting through that history. Position valuation and fallback entry lines remain withheld until execution validation succeeds; fallback lines also require verified activity.

## Legacy polling bot reference

The rest of this page is for existing **TradingBot + legacy_main.py** installations. Historical references to `main.py` in descriptions below refer to the polling runner. Its deployment, `live_check.py` and swap commands use the legacy contract interface.

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
- **The dashboard** (`dashboard.py`) shows what the bot is doing and lets you change the **strategy, take profit, stop loss, an optional time stop and the pattern-guard limits** without restarting anything. It also draws a zoomable price chart and compares all strategies on the recorded prices. It cannot send a transaction, switch off dry run, change the trade size, and it never shows a key.
- **You** stay in charge of the money: only the owner wallet (MetaMask) can withdraw, and its key is never on the server.

## Before you start

| You need | Why | Where to get it |
|---|---|---|
| A computer with a terminal (macOS or Linux; on Windows use WSL), `git` and `make`, and [`uv`](https://docs.astral.sh/uv/) | runs everything (`uv` fetches Python 3.13 for you) | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, then open a new terminal (or `source ~/.local/bin/env`) so `uv` is found |
| An **Infura** or **Alchemy** account with a Sepolia URL | how the bot talks to the chain. The URL contains your API key, so it is a secret | [DEPLOY 1.1](DEPLOY.md#11-get-an-infura-or-alchemy-sepolia-url) |
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

**Existing Phase 1 installation:** follow [Phase 2 migration](PHASE2.md), including new token/pool addresses, Infura + Alchemy, vault permissions, and archiving reconciled Phase 1 records. Do not overwrite an existing `.env` with the copy command below. The template now selects the Phase 2 0.05% pool for the loop and the 1% pool for read-only comparison. Both have fETH as the strategy's base and f564 as its quote, despite the reversed on-chain token order.

```bash
cp .env.example .env
chmod 600 .env           # only you can read it
```

Open `.env` in an editor. **Never commit it and never paste it anywhere**: it holds your RPC provider key (Infura or Alchemy) and the bot wallet's private key.

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
| `RPC_URL` | your Infura **or Alchemy** Sepolia URL (DEPLOY 1.1) | **yes** (contains the key) |
| `RPC_FALLBACK_URLS` | Alchemy Sepolia URL when Infura is primary; replace the template placeholder or clear it | **yes** |
| `MAX_RPC_PER_SECOND` | template uses `5` requests/s per provider (`0` disables the cap) | no |
| `POOL_ADDRESS`, `POOL_FEE` | primary execution pool, Phase 2 narrow pool / `500` | no |
| `SECONDARY_POOL_ADDRESS`, `SECONDARY_POOL_FEE` | comparison pool, Phase 2 wide pool / `10000` | no |
| `PRIVATE_KEY` | already written by the script above. It is the **bot wallet's** key, not your MetaMask one | **yes** |
| `OWNER_ADDRESS` | your enrolled MetaMask address (copy it from MetaMask to keep the capitalisation) | no |
| `MAX_TRADE_AMOUNT` | size of one buy, in units of the token being spent (see [trade size](#trade-size)). Start with `0.01` | no |
| `RISK_PER_TRADE_PCT` | optional, default `0` (off): size each buy so a stop loss costs about this share of the vault's f564, never above `MAX_TRADE_AMOUNT` (see [trade size](#trade-size)) | no |
| `DRY_RUN` | leave as `true` | no |
| `BOT_CONTRACT_ADDRESS` | leave empty for now; step 3 gives you the value | no |
| everything else | leave as it is (pool, router and token addresses are already filled in) | no |

`DRY_RUN=true` means **nothing is ever sent**: the bot asks the chain what *would* happen. Only the word `false` (in any capitalisation) turns a dry run off; anything else, including a typo, stays a dry run. You will flip it only in step 10.

## Step 3. Deploy your bot contract

This deploys the **legacy TradingBot/main.py** installation. For the current contract, use [the Phase 2 operator walkthrough](EVENT_CONTRACT_SETUP.md); runtime and recovery details are in [EVENT_STRATEGIES](EVENT_STRATEGIES.md).

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

`uv run python config.py --all-pools f564 1000` compares both configured pools at one block (read-only). `live_check.py` checks both pool fees and simulates both directions. These commands do not activate arbitrage or automatic routing. The target is portfolio return in f564; the dashboard's strategy P/L is not the official competition score.

```bash
uv run python live_check.py          # read-only: roles, limits, guards, a simulated swap each way
uv run python bot_contract.py status # owner, operator, balances, operator ETH
```

**You should see** mostly `[PASS]` lines and `0 failed`. `[WARN]` means "look at this", `[FAIL]` means stop and fix (the line says what). The full table of checks is in [DEPLOY 2.8](DEPLOY.md#28-live-check-of-the-deployed-contract-read-only).

## Step 6. Run the bot in dry run

On migration, `Unidentified market records` or `Market changed` means the old ledger/history cannot be reused: reconcile the old position and archive `logs/` as explained in [PHASE2.md](PHASE2.md). Startup writes a public `logs/market.json` identity for fresh records. `RISK_PER_TRADE_PCT needs pool_profile.json` means there is no valid matching layout: keep risk sizing at `0`, pause new buys in a deliberate dry run, and request a scan. Phase 2 narrow-pool scans need roughly 700 bitmap reads plus initialized ticks (several minutes). Fast polling remains off until Phase 2 research validates its timing.

```bash
uv run python legacy_main.py --ticks 3     # three legacy ticks
uv run python legacy_main.py               # legacy polling loop
```

**You should see** lines like:

```
INFO Started. contract=0x… operator=0x… dry_run=True
INFO poll=30s cooldown=60s trade size=0.01 min ETH=0.01
INFO strategy=mean_reversion take profit +2.5% stop loss -4% (change it on the dashboard or in control.json)
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
- The dashboard never calls the chain itself: the price and vault balances it shows are what the bot last read (`logs/state.json`, written every tick), so an open tab costs no RPC requests. If the bot is not running, the page shows the last values with a warning that they are old.
- To keep it running after you log out, use the systemd unit in step 9, or `tmux`.

## Step 8. Choose a strategy and set the exit rules

On the dashboard, in **Strategy and exit rules**:

1. Pick a **strategy** (when to buy). They are described [below](#the-strategies); start with *Mean reversion*.
2. Adjust its **settings** if you like (each field shows its allowed range).
3. Set **take profit** and **stop loss** (when to sell). Defaults: +2.5% and −4%. Optionally tick the **time stop** (sell what has been held too long, 30 minutes by default; it also applies to a position that is already open). Below them, the **pattern guard** limits (see [The pattern guard](#the-pattern-guard)).
4. Look at the **Estimated effect** box above the Save button: it replays every strategy on the recorded prices with the numbers you typed, and shows the estimated gain or loss against the saved settings, after the pool fee and the price impact, with warnings. It is a paper replay at `MAX_TRADE_AMOUNT` per trade (not the risk-sized amount, if you use `RISK_PER_TRADE_PCT`: a smaller trade pays less price impact, so both the amounts and the percentages differ). Each trade is charged what its size costs on the pool's liquidity layout, scaled by how deep the bot last found the pool (`depth_scale` in `logs/state.json`; as saved until it has sized a buy), so use it to see which way a change pushes.
5. Press **Save** (it stays greyed out until you change something; **Discard changes** goes back to what is saved). The message says "Saved … The bot applies it on its next tick".

You can check that the bot noticed in its log (you only see this line when the values actually changed):

```
INFO settings changed: {'strategy': 'rsi', 'take_profit_pct': 6.0, …}
```

Nothing needs restarting. Rules of thumb:

- A **new strategy** only affects the *next buy*. An already open position is still sold by take profit / stop loss.
- **New take profit / stop loss numbers apply to an open position immediately.** So does the time stop.
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

Every `POLL_SECONDS` (30 s; with `FAST_POLL_SECONDS` set, that often during seconds 30 to 56 of every minute instead) it takes one **tick**:

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
- A **cooldown** (`COOLDOWN_SECONDS`, 60 s by default) after any trade stops it re-entering straight away. An exit right after a *successful* buy is **not** delayed (a stop loss must not wait), but after a *failed* attempt it waits the cooldown like any trade.
- The bot only ever sells **what it bought itself**. Tokens you deposited by hand are never sold.
- Nothing is sent unless `DRY_RUN=false`. In a dry run everything else is the same, using the quoted amounts as the "fill" (so a dry-run position, and its profit and loss, are estimates, not vault amounts).

### Trade size

A **buy** spends `MAX_TRADE_AMOUNT` of the *quote* token (f564), or less if you set `RISK_PER_TRADE_PCT` (below). With a price near 1000 f564 per fETH, `0.01` f564 buys only about 0.00001 fETH, which is tiny but fine for testing. The matching **sell** sells exactly the fETH that buy delivered, however large or small that is.

The contract also enforces a **per-swap cap** on-chain (set at deploy from `MAX_TRADE_AMOUNT`). To trade bigger you must raise *both* the `.env` number and the on-chain cap (the owner sends `setTokenCap`, see [DEPLOY section 5](DEPLOY.md#5-owner-operations)).

**Raising the size, step by step** (example: 1,000 to 2,000 f564 per buy; the vault must hold at least that much f564):

1. Print the calldata (read-only, nothing is sent; the amount is in raw units, 18 decimals, so 2,000 f564 is `2000` followed by 18 zeros):
   `uv run python bot_contract.py calldata setTokenCap 0xfd840838F598d7abB153a8bc93f4a2FF38D2D5B8 2000000000000000000000`
2. From the **owner** wallet in MetaMask (Settings → Advanced → Show hex data), send a transaction with value 0 to the contract with that `data`. The owner key never goes on the server.
3. Check it: `uv run python bot_contract.py status` shows `cap per swap` for **both** tokens. The sell trades the fETH the buy brought, so the **fETH cap must be larger than that** (about the buy size ÷ the price, plus 1% slippage: roughly 2.1 fETH for 2,000 f564). Deploy set both caps to the same `MAX_TRADE_AMOUNT`; if your fETH cap is small, send a second `setTokenCap` for the fETH address. Otherwise the bot logs `buy skipped: this buy would return more base than the contract's per-swap cap`.
4. Set `MAX_TRADE_AMOUNT=2000` in `.env` and restart the bot. The on-chain number must be at least as large as `.env`, or the swap reverts with `amountIn out of range`. `live_check.py` will `WARN` that the cap differs from `MAX_TRADE_AMOUNT`: that is expected.

The price-impact limit (2%) is far away: a buy of about 1,000 f564 (about 1 fETH) moved the price only about 0.03 to 0.08% on the live trades (a round trip cost about 0.75% then, 0.60% of it the pool fee). Impact grows with size, so the dashboard's estimates and the risk sizing work out what each trade's actual size costs from the pool's liquidity (see "Sizing by risk" below), and stay right at 10,000 f564 too. The size is bounded by the cap, the vault balance and how much loss you accept, not by the pool.

**Sizing by risk (optional).** Set `RISK_PER_TRADE_PCT` in `.env` (default `0` = off). The bot then looks for the biggest buy whose stop loss costs at most `vault f564 × risk%`, never above `MAX_TRADE_AMOUNT` or the vault balance. What a stop loss costs is worked out exactly on the pool's liquidity saved in `pool_profile.json` (a snapshot; the maths is `v3pool.py`): the price falling by the stop loss, both 0.3% pool fees and the price impact of the buy and of the sell. The impact grows with the size: a round trip costs about 0.73% at 1,000 f564, 1.25% at 5,000 and 2.3% at 14,000. So with a small vault the result is close to `vault × risk% ÷ (stop loss% + 0.75%)` (1,386 f564, 2% risk, 4% stop loss: about 583 f564 per buy), and with a big one it is clearly smaller than that formula says (40,000 f564, 2% risk, 5% stop loss: the formula asks for 13,913, the bot buys less, because a buy that big pays a lot more than 0.75%). As the vault grows, buys grow with it, up to the ceiling. The bot refuses to start with `RISK_PER_TRADE_PCT` above 0 if `pool_profile.json` is missing. Each time a buy is sized the bot also reads the pool's current liquidity (one read-only call, at most every 5 minutes) and scales the saved layout to it, so a pool that has got deeper or thinner since the snapshot is followed; if that read fails the buy is skipped (`buy skipped: could not read the pool's liquidity`). It does not see liquidity moving to *other price ranges*: for that, press **Scan pool liquidity** on the dashboard (see "Pause and scan buttons" below). Note that it limits the *loss*, not the *cost*: at big sizes the round trip can eat most of a 3% take profit, so keep `MAX_TRADE_AMOUNT` as a ceiling you are comfortable with. A price that jumps past the stop loss loses more than planned (live stops filled at -4.2%), so the "risk" is a plan, not a guarantee. `RISK_PER_TRADE_PCT` cannot be changed from the dashboard, on purpose.

**Pause, scan and sell-everything buttons.** The **Controls** card on the dashboard has three buttons. None talks to the chain from the page: they write a flag in `logs/control.json` and the bot acts on its next tick.

- **Pause new buys** / **Resume buying**: while paused the bot makes no new buy. A position that is open is **still sold** at its take profit, stop loss or time stop, so pausing never leaves you without an exit. The header shows "New buys paused". Use it before you change the vault by hand, or when the pool looks odd.
- **Scan pool liquidity**: the bot reads the pool's whole liquidity layout (about 120 read-only calls to the node, spaced out so a rate-limited RPC does not refuse them, 30 to 40 seconds). It does this at the end of the tick, so a sale due in that tick is not held up. The result is kept in `logs/pool_profile.json` and replaces the layout that ships with the code: from then on trade sizes and the dashboard's estimates use it, also after a restart. Press it after liquidity providers have clearly changed the pool, not on a schedule. If it fails (for example the node says "too many requests"), the card says why and the old layout stays; press again later; a restart does not retry it by itself, and the card still shows the last answer after one.
- **Sell everything to f564**: asks you to confirm, then pauses new buys and makes the bot sell its open position at the market on its next tick (`closed position (liquidate)` in the log, "Sold everything" in the trades table), whatever the price is; if a take profit or stop loss applies in that same tick it is the recorded reason. With no open position it just says so. Buying stays paused afterwards until you press **Resume buying**. It sells only what the bot bought: base token you put into the vault by hand is left alone (the log says how much). A swap that fails is retried only after the usual cooldown and, after `MAX_TRADE_FAILURES` failures in a row, halts the bot like any other. Use it at the end of a run so everything is back in f564.

**Taking the starting fETH out of the vault.** The bot only ever sells what it bought, so fETH you deposited stays untouched. To withdraw it, **stop the bot, or make sure `status.py` shows no open position, before you send the withdrawal** (a buy could land between printing the calldata and mining it). If a position is open, withdraw no more than the vault's fETH minus that position's fETH, and never `all`: the bot sells exactly what it recorded, and with less in the vault it logs `sell skipped: vault balance … < trade size` and its take profit and stop loss can no longer fire. `uv run python bot_contract.py withdraw-calldata fETH 0.9999` prints the calldata, sent from the owner wallet as above.

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
- optional, if you tick **time stop**: held for at least that many minutes and neither of the above has happened → **sell** at the market price (`time stop`). It is **off by default**. The default length, 30 minutes, comes from a backtest on 43 hours of recorded prices (`research/2026-11-06-fade-and-exits/`): of 15 to 60 minutes, 30 earned most. It is in-sample, so treat it as a starting point

Worked example: entry 1000, take profit 2.5%, stop loss 4% → sells at ≥ 1025 or ≤ 960.

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
| Position card | none, or what is held: how much, what it cost, **entry price**, the **take-profit and stop-loss prices** (and when the time stop will fire, if ticked), and the unrealized result at the current price |
| New buys card | **Allowed**, **Paused** (with the reason: this is the pattern guard), **Not buying: holding a position**, or **Guard off** (both limits 0); plus the last big price steps drawn as arrows (`▲` up, `▼` down; a red underlined arrow repeats the direction before it, which breaks the up/down pattern), how many of the 7 neighbouring pairs alternate against how many are needed, and the 60-sample price drift |
| Realized profit/loss | the sum over closed trades, after fees (in a dry run: estimated from the quotes), with wins and losses and how many ended at take profit / stop loss / time stop |
| Vault balances | what the contract holds, and the bot wallet's gas (ETH) |
| Chart | the price line (hover or use the arrow keys for exact values; "Table view" lists them). `▲` = a buy, `▼` = a sell (green: take profit, red: stop loss, amber: time stop). While holding, horizontal lines mark entry, take profit and stop loss. **Zoom**: the range buttons (1h to All), drag a rectangle on the chart, Ctrl + scroll at the pointer, `＋` `－` `◀` `▶`, or the `+` `-` keys with the chart focused; double-click or "Back to live" returns to the live view. A long window is thinned to about 900 points, keeping the highs and lows |
| Strategy comparison | every strategy replayed on the recorded prices with your current exits, guard and cooldown (a paper replay: fills at the recorded price, minus the pool's 0.3% fee on each leg and about 0.15% price impact per round trip; the table shows price moves, pool fee, impact and the net separately, plus a warnings count). The strategy in use is marked, the last row is what the bot really did in the same window. It ranks ideas on prices the settings were tuned on; it is not a forecast. The dashboard computes it from `logs/prices.csv`, with no RPC call |
| Strategy and exit rules | the form from step 8. Above the Save button, an **Estimated effect** box replays every strategy on the recorded prices with the numbers *as you type them* (before saving): price moves, pool fee, price impact and the net, the **estimated gain or loss against the saved settings** for each strategy, and **warnings** for the settings (a take profit the costs would eat, a stop loss smaller than the pool's usual jump, a time stop that would cut winners, a guard that is off or very strict, a strategy number that is mostly noise, a setting that loses on the replay). Nothing is saved or sent while you look. The same caveats apply as for the comparison: recorded prices the settings were tuned on, gas not counted, not a forecast |
| Closed trades | every closed trade, newest first, 25 per page, with a filter by why it closed (take profit / stop loss / time stop), how long it was held, the result in tokens and %, and the total of the filtered rows |
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
realized:  3 closed trades: 2 won, 1 lost (2 take profit, 1 stop loss)   (a "time stop" count is added when there is one)
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
| `control.json` | the strategy, take profit, stop loss and strategy settings (what the dashboard writes). Hand-editable: the same eight keys as the form (`strategy`, `take_profit_pct`, `stop_loss_pct`, `params`, `guard_flip_pct`, `guard_drift_pct`, `time_stop_on`, `time_stop_min`) plus the buttons' numbers (`paused`, `scan_request`, `liquidate_request`: leave those to the dashboard), missing keys keep their defaults, unknown keys are an error | yes, but a **running** bot keeps its current settings until you save again from the dashboard or restart it |
| `prices.csv` | one price per tick, for the chart | yes |
| `monitor_seen.json` | addresses `pool_monitor.py` has already seen | yes |

**Resetting a stuck or unwanted position.** The ledger is how the bot knows it owns something to sell. If it says OPEN but the vault no longer holds that token (you withdrew it, or it was a paper position and the vault has none), the exit logs `skipped: vault balance … < trade size` forever. To clear it: stop the bot, then either edit the ledger file of that mode (`ledger_dryrun.json` for dry run) and set `"open": null` (this keeps your closed-trade history), or delete the file (this also forgets the history), then start the bot. If the vault *does* hold base the bot bought but the ledger lost track of it, the bot will not sell it: withdraw it as owner ([DEPLOY section 5](DEPLOY.md#5-owner-operations)) or sell it by hand with `bot_contract.py swap`.

## Command cheat sheet

| I want to… | Command |
|---|---|
| run the tests / lint | `make test` / `make check` |
| see where things stand (terminal) | `make status` |
| open the dashboard | `make dashboard`, then `http://localhost:8765` |
| run the legacy polling bot in the foreground | `uv run python legacy_main.py` |
| try a few legacy ticks and exit | `uv run python legacy_main.py --ticks 3` |
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
| Yellow box: "could not read the chain" | RPC trouble (provider down, wrong key, rate limited). The rest of the page still works from local files |
| Saved settings do not seem to apply | Look for `settings changed` in `logs/bot.log` (it only appears when a value really changed). If you see `ignoring the settings file`, the file is invalid (the message says why). If you see nothing at all and the values were different, the bot and dashboard use different project folders |
| Nothing happens for a long time | Normal at first: strategies need 20 or more samples, then a trigger. Watch the `price=` log lines. A quiet pool may not move enough for hours; try a trend strategy or smaller thresholds, but not below the fee floor On a fresh start the pattern guard can also hold buying for about an hour: check the log for `new buys are paused`. |
| The log says `new buys are paused: …` and nothing is bought | The pattern guard (see [The pattern guard](#the-pattern-guard)). The reason is in the log line and on the dashboard's **New buys** card. It resumes by itself when the pattern is back; to trade without it set both guard limits to 0 on the dashboard |
| `ValueError … Unknown format ''` (or similar) right after starting any script | `BOT_CONTRACT_ADDRESS` is still empty: finish step 3 and paste the contract address into `.env` |
| A connection error or `401` right after starting | `RPC_URL` still holds a placeholder (`…YOUR_INFURA_API_KEY` or `…YOUR_ALCHEMY_API_KEY`): put your real provider URL in `.env` (step 2) |
| `PRIVATE_KEY is not the bot contract's operator` | The key in this `.env` is not the one that was set as the contract's operator. Use the same `.env` as the machine that deployed it (step 9) |
| `Not starting: logs/HALTED exists` | The bot halted itself earlier. Read the reason in that file, fix the cause, delete `logs/HALTED`, restart ([DEPLOY 4.5](DEPLOY.md#45-after-a-halt)) |
| `Address already in use` from `dashboard.py` | Another program has port 8765 on that machine. Use `python dashboard.py --port 8766` and tunnel `-L 8765:127.0.0.1:8766` |
| `buy skipped: vault balance … < trade size` | The vault holds less f564 than the trade size (`MAX_TRADE_AMOUNT`, or the smaller risk-sized amount). Fund it (step 4) |
| `buy size X (ceiling Y): RISK_PER_TRADE_PCT=…` | Not a problem: `RISK_PER_TRADE_PCT` made this buy smaller than `MAX_TRADE_AMOUNT` |
| `sell failed (N in a row): Transaction reverted: 0x…` (or `buy failed`) | The swap was mined and refused, nothing was lost but gas. On 2026-11-05 four of these were **out of gas**, not slippage: the gas limit was only 20% above the estimate and a swap by someone else in the same block moved the price, so ours crossed more price ticks and needed 21 to 38% more. Swaps now get twice the estimate (unused gas is refunded). If one still happens, paste the hash into Sepolia Etherscan; the page shows the reason. After any failed attempt the next try waits the cooldown, and three failures in a row halt the bot |
| `pool depth: N% of the saved layout (liquidity …)` | Not a problem: the pool's liquidity was read when a buy was sized. Far from 100% means liquidity providers changed the pool since the snapshot, and the buy is sized for that |
| `new buys are PAUSED from the dashboard (a sale still runs)` / `new buys are resumed …` | You pressed the pause button (or resumed). Every tick line then ends with `PAUSED`, and a buy signal shows as `decision=hold` |
| `scanning the pool's liquidity (asked on the dashboard)` / `pool scanned at block N` / `the pool scan failed: …` | The scan button. The tick that does it takes 30 to 40 seconds longer. A failure leaves the old layout in use |
| `buy skipped: the pool's depth cannot be sized: …` | The liquidity read worked, but the saved layout holds nothing at the current price (the price has left the range it covers) or the pool is dry. Buys are skipped until the price comes back; press **Scan pool liquidity** if the pool has really moved |
| `ignoring the scanned pool layout, using the shipped one: …` | `logs/pool_profile.json` is damaged. Press **Scan pool liquidity** to write a new one, or delete the file |
| `buy skipped: could not read the pool's liquidity` | That one read failed (RPC error): the buy is skipped and the next signal tries again. If it repeats, check the RPC |
| `buy skipped: the risk-sized buy X is below 1.00% of MAX_TRADE_AMOUNT` | The vault holds so little f564 that a risk-sized buy would not be worth its gas. Fund it |
| `closed position (time_stop) at …` | The time stop sold a position that had been held for its limit. Untick it on the dashboard if you do not want it |
| `sell everything (asked on the dashboard): …` / `closed position (liquidate) at …` | The **Sell everything** button was served: it says what was sold, or that there was nothing to sell. Press **Resume buying** to trade again |
| `sell skipped: vault balance …` | The vault holds less fETH than the position needs. See "Resetting a stuck position" above |
| `buy skipped: price impact too large` | The trade would move the pool price more than 2%. Lower `MAX_TRADE_AMOUNT` |
| `ignoring the settings file` in the log | `control.json` is invalid (the message says why). A running bot keeps its previous settings; a bot that *starts* while the file is invalid uses the defaults until it is fixed. Save again from the dashboard to repair it |
| Bot halts with `… ledger…json is unreadable` | The position record is damaged. Fix the file (or, if you are sure the vault holds nothing the bot bought, move it away), then **delete `logs/HALTED`** and restart ([DEPLOY 4.5](DEPLOY.md#45-after-a-halt)) |
| `buy skipped: this buy would return more base than the contract's per-swap cap` | In this pool one quote token buys more than the cap allows to sell back. Lower `MAX_TRADE_AMOUNT`, or have the owner raise the cap (`setTokenCap`) |
| Halted with `may be on chain` / `outcome is unknown` | A swap was sent but its result could not be read. Look at the bot wallet's transactions on Etherscan first, then follow [DEPLOY 4.5](DEPLOY.md#45-after-a-halt) |
| Anything about deploying, Infura, owner operations | [DEPLOY.md section 6](DEPLOY.md#6-troubleshooting) |
