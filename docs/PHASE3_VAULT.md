# Phase 3 credit vault on Sepolia

**Deployed vault** `0x346B0c324963B91afb6f722d126E90506623bB8d`. Block 12,204,515 confirmed the deployment. An independent Sepolia read matched the runtime bytecode and immutable settings to the release and confirmed that the enrolled wallet `0xB6bf0Ba3F43D4386216Df037dcA1A1203038e135` owns the vault. [Deployment receipt](https://sepolia.etherscan.io/tx/0x69316323c0953196ea1e130219f04298f35a3f7f07defa0877bd169b124fcbe5)

The enrolled wallet owns the vault. The server's operator pays gas and sends a specific vault action. The credit facility accounts for collateral and debt under the vault address. Portfolio registration is a separate owner action on the competition site.

## Install and verify

The release includes `abis/phase3_vault.artifact.json`. To rebuild it locally with the project compiler, run `uv run python phase3_vault.py build`. This also produces `build/Phase3Vault.standard-input.json` for source verification with solc 0.8.27, 200 optimizer runs and the Paris EVM target.

From the server checkout, `.venv/bin/python phase3_vault.py estimate-deploy` simulates contract creation and prints the owner, operator and estimated gas. For an authorized Sepolia creation, `.venv/bin/python phase3_vault.py deploy` uses the configured operator. A successful receipt and code verification produce `logs/phase3/deployment.json`. Use `.venv/bin/python phase3_vault.py verify` to check the runtime bytecode, market identities, enrolled owner and operator before funding.

The vault has an immutable 40% aggregate LTV ceiling for borrowing and collateral withdrawal. An owner setting also caps each token’s **total outstanding debt**, including interest. Borrowing starts disabled, debt and liquidation caps start at zero, and collateral deposits leave 1,000 f564 or 1 fETH liquid in the corresponding token. The owner can change either reserve. Depositing extra collateral and repaying debt remain available while borrowing is disabled. Interest and price moves can raise LTV after a borrow. Only the owner can transfer tokens out to their wallet.

## Enrolled wallet steps

1. Open the competition site with the enrolled wallet `0xb6bf0ba3f43d4386216df037dca1a1203038e135`. Register the **verified vault address** from the deployment record. Confirm that it appears in your Phase 3 portfolio before funding it. [Competition site](https://fin564.vercel.app/)
2. Use that wallet’s ERC-20 transfer action to send the chosen amount of Phase 3 f564, fETH, or both to the vault. f564 is `0xc319f958df8A74b077578b437C402dC7D7687864`. fETH is `0xE00296A373Cd19E6aBAf085e50a393938879737D`. Check the vault balance and registered portfolio. The operator pays native Sepolia ETH gas. The vault handles facility and router approvals internally.
3. To authorize a strategy, use the owner wallet to call `setLimits(token, (debtCap, liquidReserve, maxLiquidation))` for each token the strategy needs. All three values use 18-decimal raw units. A liquidation cap authorizes both holding seized collateral and an atomic close. An atomic close enforces its requested positive gain in the repaid token, excluding native gas. If borrowing is desired, also call `setBorrowingEnabled(true)`. For liquidation-only operation leave borrowing disabled. Debt caps of zero prevent borrowing that asset. A liquidation cap of zero disables repayment of that asset for third-party liquidations.

## Prepare a wallet or operator call

The CLI takes JSON with `id`, `function` and `args`. For example, to allow up to 2,000 f564 of outstanding debt, keep 1,000 f564 liquid and authorize no f564-funded liquidation, put this in an owner request file. It is an example allocation.

```json
{"id":"quote-limits-1","function":"setLimits","args":["0xc319f958df8A74b077578b437C402dC7D7687864",[2000000000000000000000,1000000000000000000000,0]]}
```

Run `.venv/bin/python phase3_vault.py calldata owner-request.json`. Send the printed calldata to the printed vault address from the enrolled wallet with value zero. Use raw integers and a JSON editor that preserves their precision.

Operator functions are `deposit(token, amount)`, `repay(token, maximum)`, `borrow(token, amount)`, `withdrawCollateral(token, amount)`, `liquidate(lot)` and `liquidateAndClose(lot, minGain, sqrtLimit)`. The lot tuple is `[borrower, debtToken, collateralToken, maxRepay, minSeized, deadline]`. The deadline is a Unix timestamp in seconds. For a cross-token close, provide a pool-native sqrt price limit for the sale and `minGain` in raw debt-token units. The sale uses only the collateral received in that transaction. A same-token close checks the balance gain directly.

For example, a deposit request for 2 fETH is shown below. With the initial reserve it requires at least 3 fETH in the vault.

```json
{"id":"deposit-base-1","function":"deposit","args":["0xE00296A373Cd19E6aBAf085e50a393938879737D",2000000000000000000]}
```

Use `.venv/bin/python phase3_vault.py simulate request.json` for an operator preview. `.venv/bin/python phase3_vault.py submit request.json` is the explicit signed action and requires `DRY_RUN=false`. The CLI verifies the contract, simulates the operator call, records intent before sending and checks the mined receipt. An action ID is single-use. Monitor observations alone do not create requests. After repayment, read actual remaining debt before withdrawing all collateral.

If `logs/phase3/pending.json` remains after a timeout or a crash, reconcile its operator, nonce and calldata hash on Sepolia. Preserve the file and deployment/action records until the canonical receipt and resulting state are independently verified. The shared lock blocks another live sender during reconciliation.

## Automatic liquidations

The worker checks fresh Sepolia heads for borrowers with health below 1.00. It previews `liquidateAndClose` using exact integer amounts, including the sale of the seized collateral. It checks the tightened calldata on the sending provider and submits through the vault's receipted sender. It tries smaller amounts when a larger close fails. Native gas comes from the operator. The worker budgets at most 0.001 Sepolia ETH per send and 0.01 per UTC day. The protocol bonus and realized token gains depend on the actual loan and pool price.

**Owner setup.** Send two zero-value transactions on Sepolia from the enrolled owner wallet to `0x346B0c324963B91afb6f722d126E90506623bB8d`. The [prepared wallet calls](PHASE3_OWNER_CALLS.md) contain the exact calldata. One approves up to 1,000 f564 per liquidation and keeps 6,000 f564 liquid. The other approves up to 1 fETH and keeps 6 fETH liquid. Both debt caps are zero. The worker detects each permission as its transaction mines. The contract grants permission for both kinds of liquidation when the cap is positive. This worker exclusively builds `liquidateAndClose` transactions.

To preview once, run `.venv/bin/python phase3_auto.py --once`. To start the live service from the server checkout, run:

```sh
tmux new-session -d -s phase3-auto 'set -a; . "$HOME/.watchdog-webhook"; set +a; exec .venv/bin/python -u phase3_auto.py --live'
```

The live command requires the configured operator and `DRY_RUN=false`. Read `logs/phase3/auto.json` for status. `awaiting_owner_limits` means the worker is watching for permissions. `watching` means it found no eligible borrower. `confirmed` includes a transaction hash, and `logs/phase3/actions/auto-*.json` records mined results and gas costs. For `halted_reconcile`, inspect the existing halt markers and pending record. To stop submissions immediately from this worker, stop its tmux session and check any in-flight transaction. Setting the on-chain liquidation caps back to zero disables future liquidations by the vault. Relaunch the tmux process after a host reboot. The worker stops at the scheduled end of Phase 3.
