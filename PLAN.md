# Uniswap V3 Sepolia Trading Bot: Step-by-Step Plan

Goal: a Python (uv) bot that reads a Uniswap V3 pool on Sepolia, decides buy/sell/hold, and swaps through the router.

**Testnet only.** Mainnet is out of scope. Nothing here has the safeguards real money would need.

Test each step before moving to the next. Steps 1-5 send no transactions. Steps 6+ do.

---

## Concepts (one-minute recap)

| Piece | What it is | You call it with |
|---|---|---|
| Pool | Holds liquidity and price | read-only calls (`slot0`, `token0`, ...) |
| Token (ERC-20) | Each token is its own contract | `approve`, `balanceOf`, `decimals` |
| Router (SwapRouter02) | The contract you send swaps to | `exactInputSingle` (transaction) |
| QuoterV2 | Simulates a swap and returns the output amount | `quoteExactInputSingle` via `.call()` (free to you, but heavy for the node) |

Flow per trade: read price -> strategy decides -> quote -> approve router (once) -> swap.

Glossary:
- **ABI**: JSON describing a contract's functions. Same code = same ABI; only the address changes per network. A minimal ABI (just the functions you use) is enough.
- **slot0**: the pool function returning the current price (`sqrtPriceX96`) and tick.
- **Nonce**: your wallet's transaction counter. Each transaction needs the next one.
- **Slippage**: how much worse than the quote you will still accept. `amountOutMinimum` enforces it.
- **Price impact**: how much your own trade moves the pool price. Large on a small pool.

Two facts about the router:
- Your professor's frontend approve popup used router `0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E`, which is Uniswap's official **SwapRouter02** on Sepolia.
- SwapRouter02's `exactInputSingle` struct has **no `deadline`** (the older SwapRouter does). Source: `IV3SwapRouter.sol` in `Uniswap/swap-router-contracts`.

---

## Known addresses (Sepolia)

Verified against the Uniswap deployments docs and by read-only calls to the pool. Re-check them in Step 3 yourself.

| Item | Address |
|---|---|
| Pool | `0x6a8BCbC98ff2485582049e67299fb16ad900728B` |
| Factory (official) | `0x0227628f3F023bb0B980b67D528571c95c6DaC1c` |
| SwapRouter02 | `0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E` |
| QuoterV2 | `0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3` |
| token0 (FTEH, 18 decimals) | `0x1967a5a5f12cbd95f12847067c2c00f044d3ef11` |
| token1 (FTC4, 18 decimals) | `0xfd840838f598d7abb153a8bc93f4a2ff38d2d5b8` |

Pool fee tier: `3000` (0.3%). Chain ID: `11155111`.

---

## Step 0: Pre-flight

- [ ] Create a **new** wallet used only for the bot. Never reuse a wallet that holds real funds or has touched mainnet.
  - `PRIVATE_KEY` is a 64-hex-character key (export it from the new wallet's account details), **not** the seed phrase.
- [ ] Get Sepolia ETH for gas (any Sepolia faucet; some require a login or a small mainnet balance).
- [ ] Get FTEH and FTC4 into the bot wallet. Look for a faucet or mint option on the frontend; otherwise ask your professor. Steps 1-5 work without tokens.
- [ ] Ask your professor: what does "connect a bot wallet" do (does your address need registering)? Any rules on trade size or frequency for the shared pool?
- [ ] Confirm the router: open `0x3bFA…Ae48E` on `sepolia.etherscan.io` and check it is named SwapRouter02 before writing its ABI in Step 2.

## Step 1: Project setup

```bash
cd /Users/aidev/Projects/blockchain/trading_bot/main
git init                     # skip if already a repo
uv init --package --name bot .
uv add "web3>=7,<8"
uv add python-dotenv
uv add --dev pytest ruff
```

`uv init` creates a `.gitignore` (it ignores `.venv` and caches but **not** `.env`). Append to it **before** creating `.env` and before your first `git add`:

```
.env
.env.*
!.env.example
logs/
```

Create `.env.example` (placeholders only, safe to commit):

```
RPC_URL=https://ethereum-sepolia-rpc.publicnode.com
PRIVATE_KEY=
POOL_ADDRESS=0x6a8BCbC98ff2485582049e67299fb16ad900728B
ROUTER_ADDRESS=0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E
QUOTER_ADDRESS=0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3
DRY_RUN=true
MAX_TRADE_AMOUNT=0.01        # in human units of the input token
```

Copy it to `.env` and fill in `PRIVATE_KEY`.

Key hygiene:
- Never paste the key into chat, docs, logs or screenshots.
- Never print `config`, `os.environ`, or the account object.
- Load `.env` with an explicit path (or run with `uv run --env-file .env ...`) so loading doesn't depend on the current directory.

Suggested layout:

```
abis/
  pool.json        # the ABI you pasted
  erc20.json
  router.json      # only exactInputSingle is needed
  quoter.json      # only quoteExactInputSingle is needed
src/bot/
  config.py        # load .env, safety constants, chain-ID guard
  chain.py         # w3, account, contract helpers, send_tx()
  pool.py          # read price/state
  executor.py      # approve + swap (enforces MAX_TRADE_AMOUNT, DRY_RUN)
  strategy.py      # pure function: state -> "buy" | "sell" | "hold"
  main.py          # loop
```

**Checks:**
- `git check-ignore -v .env` prints a matching rule. If it prints nothing, stop and fix `.gitignore`.
- `uv run python -c "import web3; print(web3.__version__)"` prints a `7.x` version.

## Step 2: Get the ABIs

Hand-write or copy **only the functions you use**. Tuple arguments are the part beginners get wrong, so copy these entries from Etherscan rather than typing them.

- **Pool:** save the JSON you already have to `abis/pool.json`.
- **ERC-20:** `approve`, `allowance`, `balanceOf`, `decimals`, `symbol`. Standard and identical for every token.
- **Router:** from the router's Etherscan *Contract -> Code -> Contract ABI*. You need `exactInputSingle`, whose single argument is a struct (tuple):
  `(tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96)`. Returns `amountOut`.
- **Quoter:** from the QuoterV2 address. You need `quoteExactInputSingle`. **The field order differs from the router:**
  `(tokenIn, tokenOut, amountIn, fee, sqrtPriceLimitX96)`.
  Returns `(amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)`.

## Step 3: Read-only verification script (no key needed)

Write a script that:
1. Connects: `w3 = Web3(Web3.HTTPProvider(RPC_URL))`. Check `w3.is_connected()` and `w3.eth.chain_id == 11155111`. Print the latest block number.
2. Builds the pool contract: `w3.eth.contract(address=Web3.to_checksum_address(POOL), abi=pool_abi)`.
3. Prints `token0()`, `token1()`, `fee()`, `factory()`, `tickSpacing()`, `liquidity()`.
4. For each token, prints `symbol()` and `decimals()`.
5. Calls `factory()` on the router and on the quoter, and compares both with `pool.factory()`.
6. Reads `slot0()` and computes the price:

```
price (token1 per token0) = (sqrtPriceX96 / 2**96) ** 2 * 10 ** (decimals0 - decimals1)
```

This float price is for **display and strategy signals only**. Never use it to compute amounts that go on-chain.

**Expected output (from my review; yours should match):**
- `fee() == 3000`.
- `token0` is FTEH `0x1967a5…ef11`, `token1` is FTC4 `0xfd8408…d5b8`, both 18 decimals.
- Pool, router and quoter all report factory `0x0227628f…aC1c`.
- `liquidity()` is non-zero. If it is 0 or tiny, stop: the pool is not usable.
- Price is around 1000 FTC4 per FTEH (it moves). It should be within about 1% of the frontend's price. If it looks inverted, you swapped token0 and token1.

If any factory differs, the pool is a custom deployment: ask your professor for the matching router and quoter before going further.

## Step 4: Config guards and wallet balances

In `config.py` and `chain.py`:
- **Chain-ID guard:** on startup, refuse to run unless `w3.eth.chain_id == 11155111`. This is the only thing stopping a mainnet `RPC_URL` typo from sending real transactions.
- **Dry-run default:** `DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"`. Live trading requires setting `DRY_RUN=false` explicitly.
- **MAX_TRADE_AMOUNT:** read it from `.env` and convert to the token's smallest unit with `int(Decimal(str) * 10**decimals)`.

Load the account: `acct = w3.eth.account.from_key(PRIVATE_KEY)`. Print:
- ETH balance: `w3.eth.get_balance(acct.address)`
- `balanceOf(acct.address)` for both tokens, divided by `10**decimals`
- Transaction count: `w3.eth.get_transaction_count(acct.address, "pending")`

**Check:** ETH balance is at least about 0.05 and the input token balance is non-zero. Amounts on-chain are integers in the token's smallest unit. Never pass floats: `1.5 * 10**18` is a float and web3 rejects it. Use `int(Decimal("1.5") * 10**decimals)`.

## Step 5: Quote (read-only)

Build the quoter contract and call it with `.call()`, never as a transaction:

```python
amount_out, *_ = quoter.functions.quoteExactInputSingle(
    (token_in, token_out, amount_in, 3000, 0)
).call()
```

- `sqrtPriceLimitX96 = 0` means "no price limit".
- The quote is valid for the current block only; the price can move before your swap lands.
- Public RPCs cap `eth_call` gas, so a big quote on a thin pool may time out.

Compute slippage protection with **integer math**:

```python
SLIPPAGE_BPS = 100  # 1%
amount_out_min = amount_out * (10_000 - SLIPPAGE_BPS) // 10_000
```

Also compare the quote with the spot price from Step 3. If the quote is worse by more than your price-impact limit (for example 2%), the trade is too large for this pool: abort. Keep trades well below 1% of `liquidity()` where possible.

**Check:** the quote for a small amount is close to `amount * price * 0.997` (the 0.3% fee).

## Step 6: Approve the router

Put one helper in `chain.py` and use it for every transaction:

```python
def send_tx(w3, acct, fn, gas_pad=1.2):
    tx = fn.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        }
    )
    tx["gas"] = int(tx["gas"] * gas_pad)  # estimate can be tight for swaps
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    assert receipt.status == 1, "transaction reverted"
    return receipt
```

Notes:
- `build_transaction` fills gas, fees and chain ID automatically, but **not the nonce**, so it is set explicitly. `"pending"` avoids colliding with an unconfirmed transaction.
- It runs a gas estimate first, so a call that would revert fails here with `ContractLogicError` before anything is signed.
- Your private key never leaves your machine. The RPC only receives signed bytes.

Approval:
1. Check first: `token.functions.allowance(acct.address, ROUTER).call()`.
2. If it is too low, send `token.functions.approve(ROUTER, amount)` with `send_tx`.
3. `amount` is an integer in smallest units. Use a bounded cap, for example `5 * MAX_TRADE_AMOUNT` (not unlimited).
4. `approve` **overwrites** the previous allowance; it does not add to it.
5. Approvals are per token. If the bot will sell both tokens, each needs its own.

Respect `DRY_RUN`: in dry-run mode, print what would be sent and send nothing.

**Check:** `allowance(...)` returns the new cap, and the transaction is visible on Sepolia Etherscan.

When you are finished experimenting, revoke leftovers with `approve(ROUTER, 0)`.

## Step 7: First swap (tiny, with strict limits)

In `executor.py`, before building anything:
- Enforce `amount_in <= MAX_TRADE_AMOUNT`. This check lives in the executor so it holds even if the strategy has a bug.
- Respect `DRY_RUN`.
- Check the wallet has enough input token and ETH for gas.

Then:
1. Get a quote and compute `amount_out_min` (Step 5).
2. Simulate: `router.functions.exactInputSingle((token_in, token_out, 3000, acct.address, amount_in, amount_out_min, 0)).call({"from": acct.address})`. This needs the approval from Step 6 and shows a revert at no cost. It only reflects the current state, so the real swap can still fail.
3. Send with `send_tx`.

**Check:** token balances changed in the expected direction. To see the swap event, decode it with `pool.events.Swap().process_receipt(receipt)`, or open the transaction on Etherscan and look at the Logs tab.

Common failures:
- `STF` -> balance or allowance too low (or a fee-on-transfer token).
- `Too little received` -> price moved or price impact exceeded your `amount_out_min`.
- Revert with no reason -> wrong fee tier or tokens (no pool at that address), or no liquidity in range.
- Out of gas -> raise `gas_pad`.
- `nonce too low` / `replacement transaction underpriced` -> a previous transaction is still pending.

Note there is no on-chain deadline in SwapRouter02's `exactInputSingle`. A stuck pending transaction could land later at a stale price, so keep `wait_for_transaction_receipt` timeouts and handle `TimeExhausted`: wait again, or deliberately replace the transaction with the same nonce and **higher** fees. Never rebuild it with a new nonce.

## Step 8: Strategy as a pure function

In `strategy.py`:

```python
def decide(prices: list[float], position: Position) -> str:   # "buy" | "sell" | "hold"
```

- Define `Position` yourself (for example token balances, last trade price, last trade time).
- No web3 calls inside, so you can unit-test it with fake price lists.
- Return `"hold"` until you have enough price samples.
- Start simple, for example buy when price is X% below the moving average and sell when X% above.
- **Fees matter:** 0.3% each way is about 0.6% round trip, plus price impact. Thresholds need to be well above that, or the strategy loses money even when it is "right".
- A small testnet pool may barely move, so the strategy may never trigger. Prove it with fake series.

**Check (pytest):** three fake series (flat, rising, falling) produce the signals you expect.

## Step 9: The bot loop

```
every N seconds:
    read pool state -> append price to history
    signal = decide(history, position)
    if signal != hold:
        quote -> check price impact -> check allowance -> swap -> log result
```

Include:
- Poll every 15-60 seconds (Sepolia blocks are about 12 seconds; public RPCs rate-limit with HTTP 429).
- `DRY_RUN` and `MAX_TRADE_AMOUNT` enforced in the executor (Steps 4 and 7).
- **Kill switch:** stop after N consecutive failures or reverts, or if the ETH balance drops below a floor.
- **Retries:** safe for reads. For a send, never auto-retry without checking whether the first transaction already landed, or the bot can trade twice. Don't retry `ContractLogicError`.
- **Error types:** in web3 v7+, raw node errors (such as `insufficient funds`) raise `Web3RPCError`, and reverts raise `ContractLogicError`. Neither is a `ValueError`.
- Logging of every price, decision and tx hash to `logs/` (CSV or text). Never log the key or the full config.
- Clean exit on Ctrl+C, but not between sending a transaction and its receipt.

**Check:** run with `DRY_RUN=true` for an hour. Confirm your wallet shows **zero** new transactions on Sepolia Etherscan.

## Step 10: Harden before trusting it

- Unit-test the strategy and the executor's limit checks.
- Replay your logged prices through `decide()`. A few days of testnet data say little about real markets.
- If the public RPC rate-limits you, switch to a free Alchemy/Infura Sepolia key (only `RPC_URL` changes).
- Re-read slippage, trade size and kill-switch values before running unattended.

### Safety notes

- Thin liquidity: anyone can move this pool's price, so testnet prices make a weak market signal.
- Public-mempool transactions can be front-run, even on testnets.
- **Mainnet is out of scope.** Using real funds would need far more: a hardened key setup, private transaction submission, audited risk limits, and real backtesting.

---

## Troubleshooting quick reference

| Symptom | Likely cause |
|---|---|
| `InvalidAddress` | Not checksummed: wrap with `Web3.to_checksum_address` |
| `MismatchedABI` on an amount | You passed a float: use `int(Decimal(...) * 10**decimals)` |
| `insufficient funds` (`Web3RPCError`) | No Sepolia ETH for gas |
| Swap reverts `STF` | Missing approval or token balance |
| `ContractLogicError` at build time | The call would revert: simulate with `.call()` and check inputs |
| Quoter call fails | Quoter's factory differs from the pool's, wrong fee tier, or no liquidity in range |
| Price looks inverted | Check which token is token0; both are 18 decimals here |
| `nonce too low` | Previous tx not confirmed yet, or stale RPC count: use `"pending"` |
| `replacement transaction underpriced` | Resending a pending nonce with similar fees: wait, or raise fees |
| `TimeExhausted` | Receipt timeout: wait again, don't rebuild with a new nonce |
| HTTP 429 | Public RPC rate limit: poll less often or use an Alchemy/Infura key |
| Balance 0 but you have tokens | Wrong wallet or wrong token address |
