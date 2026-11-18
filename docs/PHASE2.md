# Phase 2 configuration and migration

The objective is **competition return measured in f564**. A buy spends f564 and receives fETH; a sell returns f564. Prices are always f564 per fETH, even though Phase 2 pools have f564 as token0 and fETH as token1. Ledger P/L is in f564. Its return on traded capital is not the competition's whole-portfolio return, which includes all registered wallets and positions under the platform's valuation rules. Arbitrage research must evaluate f564 → fETH → f564 proceeds and portfolio return, reporting gas separately in Sepolia ETH.

Addresses were verified against the [live phase configuration](https://fin564.vercel.app/api/competition/phases) and both pools' on-chain token/fee reads on 2026-11-17. Both tokens have 18 decimals.

## Update your existing `.env`

Stop the bot and dashboard before migrating. Edit your existing file; do not copy `.env.example` over a file holding your keys. Keep your operator key, owner address and vault address. Never put the owner's key here.

```dotenv
RPC_URL=https://sepolia.infura.io/v3/YOUR_INFURA_API_KEY
RPC_FALLBACK_URLS=https://eth-sepolia.g.alchemy.com/v2/YOUR_ALCHEMY_API_KEY
MAX_RPC_PER_SECOND=5
POOL_ADDRESS=0x68D8bC396df8e791EFd45A9b214CB07fe4300f48
POOL_FEE=500
SECONDARY_POOL_ADDRESS=0xC67Fcb8459C792aE1E271f24CE212Ba90eCC2920
SECONDARY_POOL_FEE=10000
F564_ADDRESS=0x21abd410c4240CCC170D004fEfB6405943e0d7Ff
FETH_ADDRESS=0x2238115a9B6469DEE6c004B00A0367fFba2Fd1BC
DRY_RUN=true
RISK_PER_TRADE_PCT=0
FAST_POLL_SECONDS=0
```

Replace the provider key placeholders with your own keys. Infura is primary, Alchemy is fallback. Failover keeps using the working provider and cools a failing provider for 60 seconds. The request cap applies separately to each provider; it does not guarantee a particular plan's compute-unit quota. Existing identical-signed-bytes broadcast failover and unknown-outcome halt rules still apply. Leave `RPC_FALLBACK_URLS` empty if only one provider is configured.

Keep the existing router and quoter:

```dotenv
ROUTER_ADDRESS=0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E
QUOTER_ADDRESS=0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3
```

Keep `MAX_TRADE_AMOUNT` at a small deliberate testing size (the template uses `0.01` f564). Phase 1 strategy defaults are not validated for Phase 2. The market bot currently has a configured 45-minute interval, so leave the Phase 1 fast-poll minute window disabled.

`POOL_ADDRESS` / `POOL_FEE` select the existing loop's one execution pool. `500` means 0.05%; `10000` means 1%. The secondary is for explicit read-only comparisons; new deployments allow both configured fees. Setting it does not enable arbitrage, split routing or automatic pool switching. Leave `SECONDARY_POOL_ADDRESS` empty for one pool. To use the wide pool in the loop, swap both address/fee pairs and treat that as a market change for saved records.

## Preserve Phase 1 records and positions

Reconcile any Phase 1 open position before migrating. Changing addresses cannot migrate an existing holding. Preserve old token addresses for owner withdrawals by explicit address if needed.

After reconciliation, archive the entire existing `logs/` directory outside the active `logs/` path, keeping a private local backup. Do not delete the ledger or reuse its open position for Phase 2. Fresh `logs/` means fresh controls, history, state and paper/live ledgers. Do not copy old pending sell-everything or scan requests into it. Investigate any previous `HALTED` condition before restarting.

The bot writes `logs/market.json` with public pool/token/fee/orientation and vault identity. Startup refuses unidentified existing records, a damaged identity, or a changed market/vault. Nothing is silently reset. The dashboard still writes only `logs/control.json` and makes no RPC calls.

## Existing vault permissions

An existing vault can be reused: its owner can change caps and fee permissions. No Solidity change or deployment is required for this configuration update. Editing `.env` does **not** change those on-chain permissions.

Before trading, the owner must set appropriate positive `setTokenCap` limits for both new tokens and allow `setFeeAllowed(500, true)` and `setFeeAllowed(10000, true)`. The fETH cap must accommodate the full exit from a buy; caps are raw integers with 18 decimals. The existing `bot_contract.py calldata` command prints owner calldata; only the owner wallet submits it. Confirm the vault is registered for Phase 2 and holds Phase 2 tokens. No on-chain action is part of this code update.

For a small setup test with `MAX_TRADE_AMOUNT=0.01`, these commands print four owner actions, using a cap of 0.01 of each token (10^16 raw units). They do not send transactions:

```bash
uv run python bot_contract.py calldata setTokenCap 0x21abd410c4240CCC170D004fEfB6405943e0d7Ff 10000000000000000
uv run python bot_contract.py calldata setTokenCap 0x2238115a9B6469DEE6c004B00A0367fFba2Fd1BC 10000000000000000
uv run python bot_contract.py calldata setFeeAllowed 500 true
uv run python bot_contract.py calldata setFeeAllowed 10000 true
```

For each output, use the enrolled owner wallet on Sepolia to send **0 ETH** to the printed vault address with the printed hex calldata, then wait for confirmation. The caps above are setup examples, not recommended arbitrage sizes. Before changing caps, confirm the vault address and owner using `bot_contract.py status`. Do not send an approval transaction from the operator wallet: these functions require the owner.

The current deployment target is **one Phase2TradingBot**, holding all funds for single front-run entries/exits and atomic backrun/cross-pool cycles. See [EVENT_CONTRACT_SETUP.md](EVENT_CONTRACT_SETUP.md) for offline build/preparation, explicit deployment, registration, funding and the `event_service.py` runtime. It is implemented and tested; confirm your deployment from its record and receipt. The earlier separate ArbitrageVault design is superseded and its standalone builder removed; its Solidity source remains required inherited code in the unified build. Preserve that exact source in verification inputs. The [liquidity study](../research/2026-11-17-phase2-arbitrage/README.md) found no profitable cycle in its snapshot. `bot_contract.py deploy` still deploys only the legacy single-swap contract; `DRY_RUN` does not suppress either deployment command.

## Read-only checks after configuration

When ready for network investigation:

```bash
uv run python config.py --all-pools f564 1000
uv run python config.py --all-pools fETH 1
uv run python live_check.py
```

The quote commands compare both pools at one common block and report the greatest output. Fees and price impact are included; gas is not. This is not an arbitrage profit calculation. A failed pool read fails the comparison rather than claiming a winner from incomplete data. `live_check.py` validates both configured pools, checks permissions, and simulates swaps through the vault in both directions. All three commands send nothing.

Startup checks actual pool tokens against configured addresses, actual fee against the configured tier, and router/quoter factory agreement. The loop checks that the primary fee and both tokens are allowed by the vault before running.

## Fresh liquidity before risk sizing

The shipped `pool_profile.json` is Phase 1 research, retained for reproducibility and refused for Phase 2 risk sizing. New scans carry pool/token identity and orientation; the maths uses the actual fee and supports either token order.

Keep risk sizing off initially. During a deliberate dry run, pause new buys and request **Scan pool liquidity** on the dashboard. The bot saves the primary layout to `logs/pool_profile.json`. At tick spacing 10, expect roughly **700 bitmap reads plus initialized ticks**, several minutes at the configured pause/rate cap. The old roughly 120-read estimate applied to Phase 1 spacing 60. Scans run only on request, after the tick's trade decision.

On a fresh `logs/`, start the bot once before saving dashboard controls so it can establish `market.json`. The default pattern guard blocks new buys until sufficient price history exists. This first run is still a deliberate dry run after the vault checks, not part of the offline configuration update.

Enable `RISK_PER_TRADE_PCT` only after a valid matching scan exists; otherwise startup refuses with `RISK_PER_TRADE_PCT needs pool_profile.json`. Dashboard replay uses the saved market fee. Until a matching scan exists, it labels its price-impact allowance as a Phase 1 estimate. These estimates and strategy defaults do not establish a Phase 2 edge.

## Next investigation

The first full liquidity scan and same-block cycle study are saved in `research/2026-11-17-phase2-arbitrage/`. Extend observations over time, track LP changes and executable sizes, and evaluate gas, execution risk and the competition's aggregate f564 portfolio-return formula. The legacy vault supports one swap per call; Phase2TradingBot adds atomic cycles alongside single swaps in one deployment.
