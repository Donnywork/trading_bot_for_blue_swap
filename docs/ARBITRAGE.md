# Atomic f564 arbitrage executor

The deployment target is **one [Phase2TradingBot](EVENT_CONTRACT_SETUP.md)** for all strategies. It inherits the cycle logic below and adds bounded single swaps, compiling into one contract with one shared balance. Use `phase2_contract.py` and `event_service.py` for deployment/runtime. The obsolete standalone arbitrage build command and generated artifacts have been removed; all cycle contract tests now deploy Phase2TradingBot.

`contracts/ArbitrageVault.sol` remains a required source dependency containing **f564 → fETH → f564** cycle and owner logic. Solidity includes that logic inside Phase2TradingBot; the service does not call a second vault. Keep its original source path and contents when verifying an existing deployment. Its single swaps intentionally allow inventory exposure; the cycle profit floor applies only to `execute`.

## Build and verification

For owner/operator setup, deployment, constructor fields, registration, funding and service configuration, start with [Deploy one Phase 2 contract](EVENT_CONTRACT_SETUP.md). This page describes its atomic cycle interface and invariants. An existing deployment's status comes from its record and on-chain receipt; do not infer it from a successful local build.

```bash
uv run python phase2_contract.py build
make check
make test
```

The build is offline and does not load `.env`. It requires the installed solc 0.8.27 already used by the project and produces:

- `build/Phase2TradingBot.artifact.json`: ABI, creation bytecode and runtime bytecode.
- `build/Phase2TradingBot.standard-input.json`: exact Etherscan standard JSON input.

Compiler settings match `bot_contract.standard_json_input()`: solc 0.8.27, optimizer enabled, 200 runs, EVM `paris`. The Standard JSON includes Phase2TradingBot.sol, ArbitrageVault.sol (inherited logic), and TradingBot.sol (router/token interfaces). These three source files produce the one Phase2TradingBot deployment; retain all three in the verification input. Use the exact saved build from deployment when verifying, rather than regenerating it from changed sources.

## Constructor and owner controls

Constructor arguments, in order:

1. `owner_`: enrolled wallet; `owner()` exposes it for platform registration.
2. `operator_`: separate bot wallet; preparation/runtime requires a nonzero address distinct from the owner.
3. `router_`: trusted Sepolia SwapRouter02.
4. `quote_`: Phase 2 f564 address.
5. `base_`: Phase 2 fETH address.
6. `pools`: `[narrowPoolAddress, widePoolAddress]`.
7. `limits`: `[maxQuoteIn, maxBaseIn, minProfitFloor]`, all positive raw integers. Each token has 18 decimals; the profit floor is f564.

The constructor checks that both pools contain exactly these tokens, belong to the router's factory, are registered by that factory, and have distinct fee tiers. Tokens, router, pools and fees are immutable. This restricts the operator to the intended pair and two routes. The owner must verify the actual addresses before deployment; the constructor does not identify symbols as proof that a token is f564.

The contract starts **paused**. Only the owner can change limits, pause/unpause, change/disable the operator, withdraw tokens or native ETH, or initiate an ownership transfer. Ownership transfer requires acceptance by the new owner. Withdrawals have no free-form recipient; funds go only to the owner. Phase2TradingBot also permits bounded single swaps for front-run entry/exit; those separate transactions have no atomic profit guarantee.

## `execute(Cycle)`

The tuple fields are:

| Field | Meaning |
|---|---|
| `buyPool0` | true: buy fETH in pool0, sell in pool1; false: reverse |
| `amountIn` | f564 spent, positive and at most the owner's quote cap |
| `minBaseOut` | positive minimum fETH received from the first swap |
| `minQuoteOut` | positive minimum f564 received from the second swap |
| `minProfit` | f564 gain required, at least the owner's positive profit floor |
| `deadline` | last acceptable block timestamp |
| `buySqrtPriceLimitX96` | nonzero V3 price limit for the buy, in the pool's native token order |
| `sellSqrtPriceLimitX96` | nonzero V3 price limit for the sell, in the pool's native token order |

Every output goes to the vault. The second swap sells **only the fETH received by the first swap**. Deposited fETH must end at exactly its starting balance. Approvals are for the exact input and are strictly cleared. A reentrancy guard protects execution, withdrawals and owner mutations.

The first leg must consume exactly the requested f564 input. The second must consume exactly the newly acquired fETH. Router-reported output must equal actual token receipts, and the vault enforces both output minima itself. The second minimum is additionally raised to at least `amountIn + minProfit`. After both swaps, the vault checks:

```text
fETH_after = fETH_before
f564_after ≥ f564_before + minProfit
```

If any condition fails, the entire transaction reverts, undoing both swaps, token movements and approvals. **A reverted transaction still costs the operator native ETH gas.** The f564 profit check does not pay for or value that gas. It also does not calculate the official competition's aggregate portfolio return or the valuation change of separately held fETH.

Only standard, non-rebasing tokens are supported. Optional-return approval/transfer tokens are handled, but fee-on-transfer tokens or tokens that misreport balances/output fail the balance invariants. There are no flash loans, arbitrary external calls, caller-controlled recipients, or hard-coded liquidity ranges.

## Range handling and read-only planning

Uniswap V3 positions each have a range. A pool's active depth is the sum of positions covering its current price; it changes when a swap crosses initialized ticks. Full-range backstops can keep the pool tradable outside the main range, with much worse price impact. A pool's total token balance is not its usable depth at the current price.

The executor lets the caller bound each swap with `sqrtPriceLimitX96`. When a price limit causes a partial fill, it rejects the cycle and rolls back instead of retaining an unfinished position. It does not assume the old published 940–1060 band still describes all liquidity. Correct limits depend on direction: Phase 2 f564 is token0, so buying fETH moves raw sqrt price down and selling it moves raw sqrt price up.

`arbitrage.quote_cycle(buy_market, sell_market, amount_in, min_profit, max_base_in, ...)` is a read-only planner. Pass validated markets from `config.load_market()` for the two pools, with raw integer amounts. It:

- Pins pool prices and both QuoterV2 calls to one block and checks that block's hash afterward.
- Computes integer sqrt price limits from a maximum raw price movement (default 100 bps).
- Quotes the second leg using the actual first-leg quoted output.
- Rejects zero output, cap violations and quotes reaching a price limit (QuoterV2 does not expose input consumed, so this may indicate a partial fill).
- Requires sufficient f564 profit after a slippage allowance (default 50 bps), and expires the plan after 60 seconds by default.

These are conservative development defaults, not researched trade sizes or profitability parameters. Quoter gas estimates are not the whole executor's gas cost. Separate quote calls accurately describe the ordered route only because the pools are distinct; they are not permission to broadcast later without simulation.

After deployment, `arbitrage.simulate_cycle(w3, vault, operator, plan)` checks the Sepolia chain, token/pool identity and owner limits, then uses `eth_call` and `estimate_gas` on the **whole cycle**. This helper signs and sends nothing. The implemented `event_execution.py` adapter uses it before execution and retains the send classification/halt rules and `SWAP_GAS_BUFFER` (2.0) over the whole-cycle estimate; gas consumption changes with tick crossings. A plan can expire or stop being profitable between simulation and inclusion.

## Evidence and practical limit

The [2026-11-17 study](../research/2026-11-17-phase2-arbitrage/README.md) contains every initialized tick from both pools at a pinned block, factory/token verification, 18 actual two-leg quotes, and an offline reconstruction agreeing within 0.000001 f564. None of those sampled cycles was profitable before gas. This supports the range handling and quote maths, not a claim that an arbitrage opportunity currently exists or that this strategy will win.

Contract tests use an in-memory chain with controllable routers, including limited-liquidity partial fills, deceptive outputs, missing liquidity, reentry and token variants. They do not deploy real Uniswap V3 contracts or test a deployed executor against Sepolia. An explicit test-only mutation removing the operator check makes the permission regression test fail:

```bash
PHASE2_TEST_MUTATION=cycle_operator uv run pytest tests/test_arbitrage.py::test_stranger_cannot_execute -q
```

That command is expected to fail; the normal suite uses unmodified source. The remaining deployment gate is real-contract simulation after deployment/registration/funding, and a measured profitable opportunity with a gas budget. Do not point `main.py` or `bot_contract.py` at the new vault: their single-swap ABI and position ledger are for `TradingBot`.
