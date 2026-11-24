# Phase 3 opening market and credit investigation

**Keep the starting 10,000 f564 and 10 fETH available for now.** At block 12,204,327 the single official pool had seen two swaps totaling 63.23 f564 of quote flow, the credit facility had zero borrowers, and active liquidity still matched the opening-day map. Those two swaps generated about 0.19 f564 equivalent in fees across *all* in-range liquidity before protocol fees. The first useful signals to watch are a real borrow, a large liquidity removal, or a sustained increase in swap volume. [Monitor snapshot](monitor-snapshot.json) · [Pool history](liquidity.txt)

## Market and scoring

The phase runs from Nov 24 at 08:02:25 UTC to the scheduled end on Dec 8 at 08:02:25 UTC. The endowment is worth 20,000 f564 at the opening price of 1,000. The competition marks registered holdings, eligible LP positions and net credit positions in f564 using the pricing pool’s 30-minute TWAP. Bad debt counts against the participant. Borrowing increases gross holdings and liabilities together. Joining the pool changes the composition and price exposure of the endowment. Compare any strategy with holding the original endowment at the **same** scoring TWAP. [Competition controls](https://fin564.vercel.app/api/competition/control) · [Phase definitions](https://fin564.vercel.app/api/competition/phases)

| Phase 3 component | Sepolia address |
|---|---|
| f564, token0 | `0xc319f958df8A74b077578b437C402dC7D7687864` |
| fETH, token1 | `0xE00296A373Cd19E6aBAf085e50a393938879737D` |
| Official 0.30% pool | `0x7FE977Ba8a5b7b25B8805B3f9456D21D3A4a9934` |
| Credit facility | `0x2a61eB24e46E8568C15becaEAa7E549B50700Dd2` |
| Market bot | `0x6011139d5F16689bDE1D245C6247b088F9582026` |

The site advanced from control version 118 to 119 during observation. Both captures showed a running phase, market-bot sizing of 10 bps and a 300-second interval. The site separately reported `participantEnabled=false` and `tradingEnabled=false`. On-chain participant LP activity was observable. Check platform eligibility and registration before allocating capital. The Phase 2 vault embeds its Phase 2 tokens and pools. Automated Phase 3 execution requires a compatible registered account or new vault. [Opening control](control.json) · [Later control and chain snapshot](monitor-snapshot.json)

## Liquidity and participant activity

At block 12,203,699 the pool held 195,633.94 f564 and 186.0938 fETH, about 381,734 f564 together at spot. Replaying liquidity gave the same active liquidity as the contract. Spot was 1,000.0321 and the later oracle returned 1,000.0993 over 1,800 seconds. Twelve tick boundaries describe the observed liquidity. The active segment spans **974.24–1,028.29** and contained about 23,538 f564 and 25.09 fETH. The organizer also funded a full-range position and four outer bands. [Tick map and mint history](liquidity.txt)

| Spot move from the observed price | Pool input before the 0.30% fee |
|---|---:|
| Down 0.5% / up 0.5% | 4.55 fETH / 4,528 f564 |
| Down 5% / up 5% | 42.79 fETH / 42,188 f564 |
| Down 25% / up 33% | 145.24 fETH / 139,767 f564 |

The market bot bought 0.042162 fETH for 42.29 f564 at 12:50 UTC and sold 0.021 fETH for 20.938 f564 at 14:30 UTC. Several other ticks produced only approvals. Counting those as trades would overstate both volume and fee revenue. A five-minute scheduling interval gives no reliable prediction of a fill. [Flows and approvals](flows.txt)

Competitor 1 funded a vault and added 7,791.60 f564 and 8.2084 fETH at 15:23 UTC across approximately 968.41–1,034.48, the same range as the organizer’s core position. This placed roughly 27% of active liquidity with that position at the observed tick. Both observed swaps predated its mint. Competitor 2 approved both tokens for the credit facility. No borrow followed through the snapshot block. The enrolled wallet still held its initial endowment at the token-flow probe. The monitor tracks all four contract addresses, including events from participants absent from the Phase 2 signer list. [Flows](flows.txt) · [Independent LP evidence](liquidity.txt)

## Credit rules and their economic effect

The facility measures collateral and debt at the oracle TWAP. Maximum initial borrow is 60% of collateral value. Withdrawal with outstanding debt must also respect that boundary. Health is `0.8 × collateral value / debt value`. A borrower becomes eligible for liquidation below 1.0. Accounts with zero current debt return the maximum unsigned integer as health, including accounts with recorded bad debt. [Participant lab](credit-participant.txt)

| Rule | Measurement |
|---|---|
| Market-wide capacity | 100,000 f564 and 100 fETH. Another account’s borrow uses the same capacity. Repayment releases it. |
| Utilization and interest | At 0%, 80%, 90% and 100% utilization, the respective annual rates were 10%, 40%, 95% and 150%. Interest accrues on debt. |
| Cash flow | Deposit transfers tokens. Borrow mints them. Repayment and liquidation repayment burn debt tokens. Same-token borrowing is possible. |
| Liquidation | Up to 50% of the selected debt per call, capped by available collateral and the caller’s maximum. Current bonus 5% at TWAP. The organizer’s accepted range in the lab was 5–8%. |
| Pauses | The facility pause blocked deposits, withdrawals, borrowing, repayment and liquidation. Outstanding debt was unchanged across a simulated day of pause. |
| Admin limits | The lab rejected ceilings below outstanding debt. The maximum accepted ceiling was `10^36` raw units per token. Non-admin changes reverted. |
| Residual debt | Interest accrual can leave a small balance between reading debt and mining a repayment. Check actual remaining debt before withdrawing collateral. |

A lab borrower owing 11,989.21 f564 against 20 fETH became eligible at a TWAP of 700. Repaying 5,994.60 f564 released 8.9920 fETH, a 299.73 f564 premium **at that TWAP**. Borrower health recovered to 1.028, preventing an immediate repeat. At a spot bid below the TWAP, selling the seized fETH can yield much less. In the extreme price-drop scenario the facility exhausted collateral, moved the unpaid debt into `badDebtOf`, and blocked the affected account from borrowing. `settleBadDebt` with no collateral succeeded but left both the recorded debt and borrowing block in place. Other accounts could still borrow. These observations establish the tested paths only. [Participant lab](credit-participant.txt) · [Admin lab](credit-admin.txt) · [Settlement probe](settlement.txt)

## Strategy by market condition

1. **Prepare for ordinary liquidations and keep both debt tokens available.** An alert at health below 1.10 gives time to check the debt denomination, collateral and executable exit price. At health below 1.00, compare the actual proceeds of selling seized collateral with repayment, swap fees and gas. For the example above the break-even sale price is about 667 f564/fETH before costs. Spot can cross that level while the 30-minute oracle still shows 700. The monitor follows two blocks behind the head, so its alerts are for observation and planning.
2. **Price LP exposure against the actual flow.** At current volume the entire fee pool from the observed trades was about 0.19 f564 equivalent. A position can lose more relative to holding as its range converts into one token. Re-evaluate when rolling 24-hour volume, fee share at the proposed ticks, and inventory change imply a positive return over holding. An organizer note calls for publishing a gradual liquidity-withdrawal schedule. Use actual `Burn` events or a published timetable before assuming depth has fallen.
3. **Use leverage only for a specific directional view.** At roughly 60% LTV a pure-fETH-collateral long with f564 debt approaches liquidation after a 25% TWAP fall. A pure-f564-collateral position with fETH debt approaches it after a 33% rise. The simulated mixed-collateral long crossed near 58% of its opening price, a roughly 42% fall. These are illustrative portfolios at initial rates. Interest, trades and collateral changes move the thresholds. A fully looped 20,000-f564 account could use around 30,000 f564 of borrowing capacity, so the current ceiling can support only about three such accounts simultaneously. High utilization also increases the cost of keeping a position open. [Leverage and interest scenarios](credit-participant.txt)

The pool’s opening depth makes the largest threshold moves expensive relative to one endowment. A spot move also takes time to feed through the TWAP. If depth falls or borrowers cluster near a threshold, repeat the calculations with the monitor’s current tick map, spot, TWAP, borrower health and market utilization. The Phase 2 two-pool cycle has no second official pool in this phase.

### Evidence and verification

The report uses the platform’s public control and phase APIs, public Sepolia events and contract views, and offline experiments with the deployed facility bytecode and mock tokens and pool. The participant lab idealized leveraged exchanges at TWAP with zero fees and impact. The admin lab used local stand-in admins. In its transcript, the attempted 1,000 bps bonus change reverted, so the subsequent line labeled “at its maximum” actually used 500 bps. The live monitor pins its market reads to one block and reconciles event-derived liquidity against the pool. The saved opening-day captures remain dated evidence as conditions change. [Facility on Blockscout](https://eth-sepolia.blockscout.com/address/0x2a61eB24e46E8568C15becaEAa7E549B50700Dd2) · [Monitor runbook](../../docs/DEPLOY.md#phase-3-monitor)
