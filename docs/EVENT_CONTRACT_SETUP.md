# Phase 2 operator walkthrough

Deploy **one `Phase2TradingBot`**, register one address and fund that address. It holds the f564/fETH used by all strategies:

```text
Python event service → operator wallet → Phase2TradingBot (one balance)
                                          ├─ swap(): one entry or exit
                                          └─ execute(): two swaps + atomic profit check
Owner wallet → permissions / funding / withdrawals
```

Front-running uses an entry transaction and a later exit transaction. A backrun uses an atomic cross-pool cycle after a target's successful inclusion. Optional cross-pool scanning evaluates cycles on new snapshots without needing a target transaction. Public ordering is not guaranteed. Front-running sells with pre-existing inventory and ordered bundles remain unsupported by the strategy service.

The previous two-address setup is superseded. Do not deploy a separate `ArbitrageVault` or configure `ARBITRAGE_VAULT_ADDRESS` / `EVENT_FRONT_VAULT_ADDRESS` for this service. `Phase2TradingBot.sol` inherits tested cycle code from `ArbitrageVault.sol`: Solidity includes that code in **one deployment**; it does not call a second vault. Single swaps intentionally permit inventory exposure; the cycle profit floor applies to `execute`, not to separate `swap` calls. Tests are not an independent security audit.

**Why are there multiple Solidity files in the verification JSON?** `Phase2TradingBot.sol` is the deployment target. `ArbitrageVault.sol` supplies its inherited cycle/owner logic, and `TradingBot.sol` supplies router/token interface definitions. These are required source dependencies for the saved build. Keep all three original files in the Standard JSON for Etherscan; deleting, renaming or merging them changes the build and can prevent verification of the existing deployment. Only the Phase2TradingBot is deployed. The obsolete standalone arbitrage build command and its generated artifacts have been removed.

## 0. Install and observe in paper mode

Start here for a new installation. If you already have a deployment plan, record or verification build, preserve it. An existing record at `intent` requires nonce reconciliation before any fresh deployment; rebuilding or choosing a new record name does not settle it.

```bash
uv sync
```

The contract tests and offline build need solc 0.8.27. If it is missing, install it once (this downloads the compiler):

```bash
uv run python -c 'import solcx; solcx.install_solc("0.8.27")'
make check
make test
cp -n .env.example .env
```

Edit `.env` locally, preserving any existing credentials. Set `RPC_URL` to a Sepolia HTTP endpoint and provide an Alchemy Sepolia endpoint in `ALCHEMY_WS_URL`, `RPC_URL` or `RPC_FALLBACK_URLS`. A new installation with an empty `BOT_CONTRACT_ADDRESS` starts in Paper. Keep `DRY_RUN=true` for observation.

```bash
uv run python main.py --seconds 1200
# Another terminal:
uv run python dashboard.py
```

Open `http://127.0.0.1:8765` and check the Phase 2 overview. With no vault, the active mode is Paper. Startup pauses entries and catches up the saved liquidity through pool logs. `awaiting_scan` asks for **Scan Pool Liquidity**. A failed scan needs a new click.

Runtime commands load the project's `.env` without overriding exported variables. `event_service.py --no-env` uses exported configuration only. Ctrl-C finishes the current operation and stops. Continue below when you want to set up contract simulation; paper predictions alone do not verify execution.

## 1. Your two wallets and the contract

You use **two wallets plus one contract address**:

| Role | Which address it is | What it holds and does |
|---|---|---|
| **Primary wallet = owner** | Your wallet enrolled in the competition; use it for `--owner` and `OWNER_ADDRESS` | Starts with your tokens and native Sepolia ETH. You use it manually to fund the contract, top up the bot wallet's gas, change permissions/limits, and withdraw. Its private key stays in your wallet application. |
| **Bot wallet = operator** | A separate wallet; use its public address for `--operator` and its private key for the script's `PRIVATE_KEY` | The local script controls this wallet. It needs native Sepolia ETH to pay deployment and trading gas. It calls the contract to trade the contract's tokens within the owner's limits. It cannot withdraw from the contract or change its permissions/limits. |
| **Phase2TradingBot contract** | Created during deployment; save the resulting address as `BOT_CONTRACT_ADDRESS` | Holds the f564/fETH trading balance for every strategy. Swap outputs and token profits stay here. It has no wallet private key for you to manage. |

**Withdrawal is owner-only, and withdrawn funds go directly to the current owner wallet.** Your primary wallet stays the owner unless you deliberately complete the two-step ownership transfer. The bot wallet paying for deployment does not make it the owner: the constructor uses the primary address you supply as `--owner`.

**Funding is not owner-only.** Anyone can send ERC20 tokens to the contract address; the current contract does not restrict incoming transfers. Normally you fund it from your primary wallet. Sending tokens gives the sender no ownership or withdrawal rights, and there is no depositor refund/account system.

The intended movement of funds is:

```text
Primary wallet ── f564 trading funds ──→ Phase2TradingBot contract
Primary wallet ── native Sepolia ETH ─→ Bot wallet (deployment/trading gas)
Primary wallet ── native Sepolia ETH ─→ pays its own funding/owner-action gas
Bot wallet     ── signed trade calls ─→ Contract swaps; outputs stay in contract
Contract       ── owner withdrawal ───→ Primary wallet
```

The bot wallet does not need your trading tokens. **Native Sepolia ETH pays gas; the fETH ERC20 is a trading token and cannot pay gas.** Keep some native ETH in the primary wallet for your manual transactions as well as in the bot wallet for automated transactions.

Both deployment and trading are Sepolia-only (chain ID 11155111). The build/plan tools fix the Phase 2 router, f564/fETH and two canonical pools from `event_market.py`; they do not accept arbitrary token/router overrides. Confirm the competition is still using these identities before a later-phase deployment. Both tokens have 18 decimals.

### The three numbers you choose

These are **two per-trade maximum amounts and one minimum profit amount**. They are not three deposits, percentages, or gas settings. They do not limit the total tokens you may keep in the contract and do not impose a daily spending or loss limit.

| Deployment option | Plain meaning | Example and effect |
|---|---|---|
| `--quote-cap` | **Maximum f564 the bot may spend on one buy or one atomic cycle.** “Quote” means f564 here. | `100` allows spending up to 100 f564 per buy/cycle. You could deposit 10,000 f564 and still limit each buy/cycle to 100. This is a ceiling, not a fixed trade size. |
| `--base-cap` | **Maximum fETH bought in one entry, sold in one single swap, or bought and resold inside one atomic cycle.** “Base” means fETH here. | `1` permits up to 1 fETH for each of those operations. A buy returning more than 1 fETH reverts even if its f564 spend fits the quote cap. This keeps a new front-run position small enough to sell in one permitted exit. It does not require depositing 1 fETH. |
| `--min-profit` | **Minimum absolute f564 gain required from one complete atomic cross-pool cycle.** | `0.1` means a cycle spending 100 f564 must return at least 100.1 f564. Otherwise the entire cycle reverts. Pool fees are already reflected in the returned tokens; native ETH gas is excluded and a mined revert still costs gas. |

The minimum-profit contract check applies to atomic backruns and independent cross-pool cycles. It **does not guarantee profit on a front-run**, whose entry and exit are separate transactions. The Python service also uses `EVENT_MIN_PROFIT` to filter modeled opportunities; a model prediction is not a guarantee.

Choose the fETH cap to accommodate the fETH your intended f564 buy could receive at the prevailing price. Both caps apply together; an overly small fETH cap can prevent a buy even when the f564 cap permits it. The service can choose a smaller trade. The values **100 / 1 / 0.1** throughout the worked examples are illustrations, not profitability recommendations.

Fill these in locally before running the commands; you do not need to send the values to anyone:

| Field to fill in | Your value |
|---|---|
| Primary/owner public address | ____________________ |
| Bot/operator public address | ____________________ |
| Maximum f564 per buy/cycle (`--quote-cap`) | ____________________ f564 |
| Maximum fETH per entry/exit/cycle (`--base-cap`) | ____________________ fETH |
| Minimum f564 gain per atomic cycle (`--min-profit`) | ____________________ f564 |

Enter ordinary positive decimal token amounts in `prepare`, such as `100`, `1`, and `0.1`. **The preparation command converts them to raw integers for you.** The conversion below is only a reference for contract UI fields and later owner `setLimits` calls:

| Setting | Human units | Raw integer |
|---|---:|---:|
| `maxQuoteIn` / `EVENT_QUOTE_CAP` | 100 f564 | `100000000000000000000` |
| `maxBaseIn` / `EVENT_BASE_CAP` | 1 fETH | `1000000000000000000` |
| `minProfitFloor` / `EVENT_MIN_PROFIT` | 0.1 f564 | `100000000000000000` |

These limits are enforced by the contract. Only the owner can change them later using `setLimits`; no redeployment is needed. Set the script's `EVENT_QUOTE_CAP` and `EVENT_BASE_CAP` no higher than the corresponding contract caps, and `EVENT_MIN_PROFIT` no lower than the contract's profit floor. Using matching values is the easiest initial setup. Editing `.env` alone does not change contract limits. Avoid lowering the base cap below an open position's recorded fETH, because that would block its full exit. Router, tokens and pools are immutable.

## 2. Build and prepare an unsigned deployment

```bash
uv sync
uv run python phase2_contract.py build
```

The build is offline. If solc 0.8.27 is missing, install it once, then build again:

```bash
uv run python -c 'import solcx; solcx.install_solc("0.8.27")'
```

The installer downloads a compiler; it does not load `.env` or send blockchain transactions. Builds use solc **0.8.27**, optimizer **200 runs**, EVM **paris**, and no viaIR. Keep `build/Phase2TradingBot.artifact.json` and `build/Phase2TradingBot.standard-input.json` for runtime and verification.

Replace the two uppercase address placeholders below with your own public wallet addresses. The example uses **100 / 1 / 0.1** in human token units; change those amounts to your chosen limits:

```bash
uv run python phase2_contract.py prepare \
  --owner YOUR_PRIMARY_WALLET_ADDRESS \
  --operator YOUR_BOT_WALLET_ADDRESS \
  --quote-cap 100 \
  --base-cap 1 \
  --min-profit 0.1 \
  --output build/phase2-deployment-plan.json
```

Each `--name` supplies a setting to the preparation command. A trailing `\` only continues the same terminal command onto the next line; it is not part of an amount.

This is offline and refuses to overwrite the plan. Inspect the plan's `owner`, `operator`, `constructor`, `limits_raw`, `chain_id` and `value`. It includes the exact creation data and constructor argument hex. The seven constructor fields are owner, operator, router, quote token, base token, `[pool500,pool10000]`, and `[quoteCap,baseCap,profitFloor]`. No owner/operator address or private key is guessed.

## 3. Deploy that reviewed plan

Before deployment, send native Sepolia ETH from your primary wallet to the bot wallet to cover deployment gas. Configure the script's `PRIVATE_KEY` with the **bot wallet's key**, while `--owner` in the saved plan remains your **primary wallet's public address**. Never put the primary wallet's key into the script or `.env`.

The following command **signs and sends a deployment**, regardless of `DRY_RUN`. The bot wallet pays deployment gas, and your primary wallet becomes the contract owner. It verifies Sepolia and the configured canonical market, displays owner/operator/limits and asks you to type `DEPLOY`:

```bash
uv run python phase2_contract.py deploy \
  --plan build/phase2-deployment-plan.json \
  --record build/phase2-deployment-record.json
```

The deployment command automatically loads the project's `.env`, with exported variables taking precedence; use `--no-env` for exported configuration only. It refuses an existing deployment record before compiling or loading configuration. It then compiles in memory and refuses a plan that differs from the current source/configuration, without rewriting saved verification artifacts. It saves the operator nonce before sending and never automatically retries. If the receipt is uncertain, reconcile that nonce on Sepolia before doing anything further; using another record filename does not resolve the old transaction. The shared operator lock prevents local live services from sending concurrently, and open inventory in either service must be reconciled first. `build`, `prepare` and `calldata` remain offline and do not load `.env`; only `build` writes build artifacts.

On success it prints `BOT_CONTRACT_ADDRESS=...` and saves the address/transaction hash in the record. This is the **only trading-contract address to configure**. `phase2_contract.py` is the build/deployment tool for this contract; `bot_contract.py deploy` is only for the legacy TradingBot. If a deployment record remains at `intent`, its outcome is unresolved in that record: reconcile the recorded operator nonce before another deployment, and preserve the exact plan and verification input.

All local live senders use the same check under `logs/operator.lock`: both services, manual swaps and deployments respect **both** `logs/HALTED` and `logs/events/HALTED`. A service can resume its own position through its normal ledger checks, but refuses the other service's inventory or intent. Manual swaps and deployments require both ledgers flat. An unreadable ledger blocks sending too. Reconcile the recorded state before switching; deleting a lock file or ledger does not resolve an uncertain transaction.

### Verify the deployed contract on Sepolia Etherscan

**Deploy first, then verify the resulting address.** Uploading Standard JSON to Etherscan publishes/verifies source code against an existing deployment. It does not deploy a contract, send tokens, or register your bot with the competition. Token transfers work without source verification, but verification makes the Read Contract / Write Contract interface available for checking roles and submitting owner actions.

1. Open `https://sepolia.etherscan.io/address/YOUR_NEW_PHASE2_TRADINGBOT_ADDRESS`. Use the address printed by deployment and saved in the deployment record.
2. Open **Contract → Verify and Publish**. If offered a verification method, choose manual verification.
3. Select **Solidity (Standard-Json-Input)**, compiler **0.8.27**, and **MIT** license.
4. Upload **`build/Phase2TradingBot.standard-input.json` from the build you deployed**. The `.artifact.json` and deployment-plan JSON serve different purposes; the Standard Input file is the source/settings upload.
5. If asked to select the contract, choose **`contracts/Phase2TradingBot.sol:Phase2TradingBot`**. The file also contains dependencies; the deployed target is Phase2TradingBot.
6. For constructor arguments, use the **`constructor_arguments`** hex string from the exact deployment plan used to deploy. It is already ABI-encoded; copy only the string value, without JSON quotes. Compiler settings must match: optimizer enabled, **200 runs**, EVM **paris**, no viaIR. Those settings are included in the Standard JSON file.
7. Submit verification. After success, use **Read Contract** to check `owner()` equals your primary wallet, `operator()` equals your bot wallet, and `contractVersion()` returns `2`.

Keep the deployed build and plan together for verification. Etherscan requires the source, compiler settings and constructor arguments to match the deployed bytecode. See its [verification instructions](https://info.etherscan.com/how-to-verify-contracts/) and [mismatch explanations](https://docs.etherscan.io/contract-verification/common-verification-errors). The artifact includes the ABI for the script; runtime does not compile.

## 4. Register and fund the new address

Register the one new bot contract through the competition's current owner-wallet flow, confirming that its balances/trades count toward the portfolio. `owner()` returns the enrolled wallet. Updating `.env` alone does not update competition registration.

If funds are in the old `TradingBot`, stop its runner and reconcile any recorded position. The owner calls the old contract's `withdraw(token, rawAmount)` to receive funds back, then sends Phase 2 tokens to the new contract. Preserve the old address, ABI and ledger. There is no automatic fund migration.

From your **primary wallet**, send **f564 to the new contract address** using the token's Transfer/Send action. This is the balance all strategies trade. The amount you deposit is your choice and is separate from the per-trade caps. Incoming transfers are permissionless, but only your primary wallet, as owner, can withdraw them. No initial fETH deposit is required; single entries acquire their own position and atomic cycles acquire/consume intermediate fETH. Existing deposited fETH remains separate from the event ledger's position. Approving a token allowance alone does not fund the contract.

### Send the trading tokens from your wallet

1. Open your wallet application (for example, MetaMask), select your **primary/owner account**, and switch to **Sepolia**.
2. Open the **current Phase 2 f564 token** in your token list. If it is missing, import it with its Phase 2 token contract address from the competition's configuration. The symbol alone does not distinguish an old Phase 1 token from the current one.
3. Choose **Send**. Paste **your newly deployed Phase2TradingBot address** into the recipient field. This is the `BOT_CONTRACT_ADDRESS` from deployment.
4. Enter the amount of f564 you want the bot to trade with. In the wallet's token-send screen, enter the ordinary token amount, such as `1000` for 1,000 f564; the wallet handles decimals.
5. Review the selected token, recipient and amount, then confirm the transfer from your primary wallet. Your primary wallet pays this transfer's gas in native Sepolia ETH.
6. Wait for success. On Sepolia Etherscan, the transaction's ERC20 Transfer record should show your primary wallet as sender, your Phase2TradingBot as recipient, and the chosen amount of the correct f564 token. Check the bot contract address's token holdings as well.

You can fund the contract while it is paused. It has no `deposit()` call to make: a normal ERC20 token transfer credits its balance. On Etherscan, the transfer transaction's top-level **To** may show the f564 token contract because that is where `transfer` executes; the **ERC20 Transfer recipient** must be your Phase2TradingBot address.

If you prefer Etherscan to your wallet's token-send screen, open the **f564 token contract's** verified **Write Contract** page, connect the primary wallet, and call `transfer(to, amount)`. Set `to` to the Phase2TradingBot address and `amount` to the raw integer token amount (18 decimals: 1,000 f564 is `1000000000000000000000`). This token `transfer` is on the f564 contract; you do not call `transfer` on Phase2TradingBot. Set native ETH value to zero and pay only the transaction gas.

Fund the **operator wallet** with native Sepolia ETH. With default `EVENT_MAX_GAS=1500000` and `EVENT_MAX_FEE_WEI=20000000000`, the service requires at least **0.03 ETH** before each trade attempt. Native ETH is distinct from the fETH ERC20. The contract itself does not need native ETH for trading.

## 5. Configure one address and enable the contract

Edit your existing `.env` yourself; preserve RPC credentials and the bot/operator key. Replace all uppercase placeholders below. Copy the same three decimal amounts you chose for deployment into the corresponding `EVENT_` settings:

```dotenv
BOT_CONTRACT_ADDRESS=YOUR_NEW_PHASE2_TRADINGBOT_ADDRESS
OWNER_ADDRESS=YOUR_PRIMARY_WALLET_ADDRESS
DRY_RUN=true
EVENT_QUOTE_CAP=YOUR_MAX_F564_PER_BUY
EVENT_BASE_CAP=YOUR_MAX_FETH_PER_TRADE
EVENT_MIN_PROFIT=YOUR_MIN_F564_PROFIT_PER_CYCLE
```

Remove obsolete `ARBITRAGE_VAULT_ADDRESS` and `EVENT_FRONT_VAULT_ADDRESS` settings. Retain `build/Phase2TradingBot.artifact.json` on the server. Use `main.py` for Phase 2 and `phase2_contract.py` for deployment and owner calldata. `legacy_main.py` provides the legacy polling and recovery interface.

The contract starts **paused**. Verify these getters via the ABI/verified contract page: `contractVersion()` = 2, owner/operator/router/token identities, pool0 = narrow, pool1 = wide, fees 500/10000, chosen caps and profit floor. Then generate an owner action offline:

```bash
uv run python phase2_contract.py calldata \
  --address YOUR_NEW_PHASE2_TRADINGBOT_ADDRESS setPaused false
```

Send **0 ETH value** from the **owner wallet** to the printed contract address with the printed calldata, or call `setPaused(false)` through a trusted contract UI. Wait for confirmation. Other offline calldata commands support `setLimits QUOTE_RAW BASE_RAW PROFIT_RAW`, `setOperator ADDRESS`, `withdraw TOKEN RAW_AMOUNT`, `withdrawETH RAW_AMOUNT`, and two-step ownership transfer. Administrative actions require the owner; for ownership transfer, the current owner calls `transferOwnership ADDRESS` and the designated pending owner calls `acceptOwnership` (the shared CLI printer labels calls as owner actions). Both pool fees are fixed by the constructor; there is no per-fee setup transaction.

On-chain `setPaused(true)` blocks both swaps and cycles, including exits. The dashboard's **entry pause** only blocks new entries/cycles and lets exits continue. Atomic and single-swap eth_call checks require the contract to be unpaused. Keep the service in simulation while making the contract callable.

## 6. Simulate, then deliberately enable live trading

```bash
uv run python main.py
```

In another terminal, run `uv run python dashboard.py` and open `http://127.0.0.1:8765`. Check that the active mode is **Simulation**. Expand **Phase 2 strategies, watched actors and limits** to verify the contract address and choose the modules. Click **Start Trading** to allow simulations. Backruns default on, and front-runs and independent cross-pool scans default off.

Successful execution checks say `simulated` and have no live position or transaction hash. `prepared` or `paper_revalidated` alone is only a model candidate. A quiet period with no attempts is not an execution-path test. All paths validate exact integer-sized quotes, limits and contract calls; single swaps also include a deadline and nonzero pool price limit. Partial fills revert instead of leaving unmatched accounting.

Set `DRY_RUN=false` in operator configuration before launching a live-capable bot. Select **Live (Sepolia)** on the dashboard, check the active mode and address, then click **Start Trading**. `EVENT_EXECUTION_MODE` is accepted for compatibility. A mode switch pauses entries and preserves the feed. **Stop Trading** pauses new entries while recorded exits continue. Resolve the displayed reason before submitting another blocked request. Preserve the existing ledgers and halt markers throughout reconciliation.

A live front-run is still two transactions and has no guaranteed profit or ordering. Cycle profit is enforced atomically before native ETH gas. Unknown send outcomes halt; do not rebuild/retry them. Stopping the service with a position open leaves that position requiring a live restart or deliberate reconciliation.

See [EVENT_STRATEGIES.md](EVENT_STRATEGIES.md) for the runtime, controls and recovery behavior. The agent's implementation work did not deploy or move funds. Your own deployment status must be checked against its record and on-chain receipt.

For unattended operation, continue with [Phase 2 on Ubuntu](DEPLOY.md#phase-2-on-ubuntu). Copy the saved `build/Phase2TradingBot.artifact.json` to the server explicitly: git and the general rsync example omit `build/`. Preserve your existing execution records when moving a running installation.
