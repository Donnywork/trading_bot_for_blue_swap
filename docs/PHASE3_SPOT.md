# Phase 3 single-pool spot trading

`phase3_auto.py --live --with-spot` runs the spot strategy and the liquidation strategy in the same server process. The registered Phase 1 TradingBot holds spot inventory at `0x418F86D4F99Ce7fB7d045dB23c933558072A02Df`. It trades the Phase 3 tokens in their official 0.30% pool. The credit vault at `0x346B0c324963B91afb6f722d126E90506623bB8d` holds the liquidation funds.

## Fund and enable spot trading

Open the [local wallet setup page](http://127.0.0.1:8765/) on the computer with your wallet extension. Click **Connect wallet**, select your enrolled account on Sepolia, and review the four transactions below. After each wallet confirmation, click **Check status** to advance. To start the page from the project directory, run `python3 -m http.server 8765 --bind 127.0.0.1 --directory deploy/phase3`.

If the wallet RPC returns HTTP 429 during a status check or simulation, the page can use the PublicNode Sepolia endpoint for that read. If the extension itself shows 429 when you confirm a transaction, check its Activity for the transaction hash. You can change the Sepolia RPC URL in the wallet to `https://ethereum-sepolia.publicnode.com` (chain ID `11155111`).

From the enrolled owner wallet on Sepolia, submit the following four transactions in order. Each transaction carries zero native ETH. The operator pays gas on subsequent trades. The generated JSON supplies the destination and calldata for each row. You can reproduce it offline with `.venv/bin/python phase3_spot.py`. [Unsigned wallet calls](../deploy/phase3/spot-owner-transactions.json)

| Order | Destination | Action |
|---|---|---|
| 1 | TradingBot `0x418F…02Df` | Set the Phase 3 f564 input cap to 1,000 f564. |
| 2 | TradingBot `0x418F…02Df` | Set the Phase 3 fETH input cap to 1.5 fETH so the worker can sell what it buys. |
| 3 | Credit vault `0x346B…bB8d` | Withdraw 1,000 f564 to the enrolled wallet. |
| 4 | Phase 3 f564 token `0xc319f958df8A74b077578b437C402dC7D7687864` | Transfer the 1,000 f564 from the wallet to the trading vault `0x418F86D4F99Ce7fB7d045dB23c933558072A02Df`. |

The trading vault already allows fee tier 3000. With the empty spot vault and credit balances observed on November 24, funding 1,000 f564 leaves about 6,999.98 f564 and 8 fETH in the credit vault. If you already funded a smaller allocation, transfer only the difference needed to reach your chosen allocation.

## Entry and exit

The worker looks at the latest mined pool state. It buys only when the last swap was an fETH sale in the previous six blocks and spot is at least 1.25% below the facility's 30-minute average. It simulates the trading vault's exact swap and checks that the minimum fill, valued at the average price after a future sale fee, would cover the profit target. Pool depth at exit and recovery of the price are uncertain. A 0.30% fee on each leg requires about a 0.603% price recovery just to break even before price impact or gas.

The worker opens one position using the smaller of the spot vault's f564 balance and its owner-set f564 cap. The minimum entry is 125 f564. The exit sells the fETH received on the entry receipt. An executable quote triggers a sale at a 0.50% gain, a 1.50% loss, or after 30 minutes. At 1,000 f564 these thresholds correspond to +5 and −15 f564 before native gas. The entry and exit are separate transactions. On stop and timer exits the worker uses a minimum output 0.10% below its current quote, so losses can exceed 1.50%. A mined exit starts a 30-minute cooldown. The legacy TradingBot enforces the minimum output on chain but its swap method has no expiration. A submitted trade can mine later at any price that meets that minimum.

## Operation

`logs/phase3/auto.json` contains `spot.status`, the spot vault's balances and caps used for entry, and its position when available. `awaiting_funding_or_caps` means the trading vault needs inventory or permission. `watching` means the price threshold has not been reached. `holding` includes an executable quote for the open position. The worker sends a Discord alert for each confirmed spot trade. The competition's control endpoint must be queried with `?address=` to check that a particular registered vault is enabled.

Spot trades share the liquidation sender lock and the 0.001 Sepolia ETH per-send and 0.01 per-day gas limits. Receipts live in `logs/phase3/actions/auto-*-spot-*.json`. Preserve these for position recovery and reconcile an unresolved `pending.json` before any further submission. Keep the worker running to manage an open position. To pause new entries while allowing exits, set the f564 cap to one raw unit and leave the fETH cap sufficient for the existing position.

## Forward liquidity experiment

The [tighter range wallet page](http://127.0.0.1:8765/lp-tight.html) prepares the second rebalance. It uses the remaining liquidity in NFT 234996 to fund a new position between 997.90 and 1,003.91 f564 per fETH. It approves up to 515 f564 and 0.999 fETH. The transaction returns unused tokens to the enrolled wallet and verifies that the source NFT has zero liquidity after the mint. This plan uses wallet nonces 139–141. Serve it with the same local server as the spot setup page.

The owner's [half-position wallet page](http://127.0.0.1:8765/lp-half.html) uses the same local server as spot setup. For NFT 234996 it prepares two limited approvals to the Sepolia Uniswap V3 position manager and one atomic decrease, collection and mint. The reviewed sequence uses owner nonces 134–136. Review each request and click **Check status** after confirmation. The page verifies the owner and range of the new NFT and the remaining half of NFT 234996. A changed nonce, changed original position or failed simulation calls for a fresh review. The paper comparison ends when the owner removes reference liquidity.

`phase3_lp_paper.py` compares the owner's full-range LP NFT 234996 with moving half its liquidity into 991.93–1,009.95 f564/fETH (ticks −69180 and −69000). Both start with the same tokens. The narrower position keeps any leftover tokens idle. The experiment begins at the pinned block in `logs/phase3/lp-paper/start.json`. Restart `watch` to resume from that same block. See the [launch commands](DEPLOY.md#phase-3-monitor).

`result.json` reports the two LP values in f564 at the facility's 30-minute average price. `split_minus_full_f564` includes the difference in inventory value and accumulated fees. `edge_excluding_top_participant_fees_f564` removes the largest positive fee advantage from any single non-market-bot signer while keeping the observed price path. It measures how much one trader contributed. Both estimates assume the observed swaps and price path, exclude rebalance gas, and model only this LP allocation. Fees from before the experiment are common to both allocations and are excluded.

The same file reports the current spot discount and the size of an fETH sale needed to reach the 1.25% entry threshold at current liquidity with the average price held fixed. This is a measure of market depth. Execution also needs a confirmed sale and sufficient profit at the vault's executable quote. Compare the two LP values after an independent burst of volume or an excursion outside the range. A gain that persists after excluding the largest fee contributor is stronger evidence for moving real liquidity.
