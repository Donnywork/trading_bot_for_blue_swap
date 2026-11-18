# Alchemy pending observation: 2026-11-17

**Observation has stopped.** The original Python observer exited cleanly and saved `state: stopped`. Its initial two-second stop check was too early; shutdown completion and process exit were subsequently confirmed. No further termination was needed.

The user authorized private loading of the Alchemy RPC configuration from `.env` for this run. A temporary launcher passed only RPC configuration into the observer's environment, without displaying credentials or loading signing keys. The launcher was removed. The observer itself remains process-environment-only.

## Coverage

- Full session started at **22:33:46 UTC**.
- Requested additional 20-minute window: **22:37:50–22:57:50 UTC**.
- Stop signal sent at **22:57:50 UTC**; the observer finished graceful shutdown approximately three seconds later.
- Filter: all **11 swap signers** identified in the seven-day study.
- Source: Alchemy Sepolia WebSocket `alchemy_pendingTransactions` and `alchemy_minedTransactions`, full transaction objects.
- No transactions were signed or sent by this observer.

**During the requested additional 20 minutes: zero matching pending notifications and zero matching mined notifications.** This means no matching notifications were received; an empty filtered stream alone does not prove that no activity occurred or establish perfect feed coverage.

**Across the entire session: five unique pending transactions and five matching mined transactions**, all captured before the additional window began. All five receipts were fetched afterward through the public competition RPC and show successful execution. There were no observed replacements or removed/reorg notifications.

## Captured timing

| Actor / group | Transactions | Pending arrival UTC | Mined arrival UTC | Notification lead |
|---|---:|---|---|---:|
| Market bot `0x6011139d…82026` | 1 | 22:35:35.271 | 22:35:36.498 | 1.228 seconds |
| Arbitrage signer `0xd067640b…64ef`, first pair | 2 | 22:35:40.264–40.265 | 22:35:48.654–48.656 | 8.390 seconds each |
| Same arbitrage signer, second pair | 2 | 22:36:55.219–55.220 | 22:37:00.400–00.402 | 5.182 seconds each |

These are local monotonic-clock differences between pending and mined **notification arrivals**, not exact submission times or guaranteed time remaining before inclusion. In particular, a mined notification can arrive after the block was produced. The sample confirms that Alchemy delivers these signers' pending transactions to this subscription; it does not establish a guaranteed lead-time distribution or independently identify their original submission provider.

## What the trades did

At block **12157393**, the market bot spent **4,433 f564** to buy approximately **4.224978 fETH** in the **1% pool**. The swap's resulting spot price was approximately **1,051.239 f564/fETH**. Its priority-fee cap was **2 wei**.

At the next block, **12157394**, `0xd067…64ef` completed two separate transactions:

1. Bought **2.763423383270303309 fETH** in the 0.05% pool for approximately **2,806.989522 f564**.
2. Sold exactly that fETH in the 1% pool for approximately **2,853.740039 f564**.

The pair produced **+46.750517 f564**, with **exactly zero net fETH** across the pool swap events. Native ETH gas totaled approximately **0.004507655 ETH**. Both transactions had a **10.2 gwei** priority-fee cap and adjacent transaction indices in the block. Their pending notifications reached this observer approximately 3.766 seconds after the market bot's mined notification. This observed sequence supports a post-market-trade arbitrage response, without proving its internal decision process.

At block **12157400**, another pair by the same signer used approximately **587.943505 f564**, received approximately **589.961342 f564**, and produced **+2.017837 f564**, again with exactly zero net fETH. Its gas totaled approximately **0.004838829 ETH**. This report does not attribute that later pair's trigger without reconstructing all intervening pool activity.

The two pairs' combined net pool flow was approximately **+48.768354 f564** before native ETH gas. This is pool-flow accounting, not a complete account-level or competition portfolio-return calculation. These were separate transactions, so same-block inclusion does not make them atomic.

## Practical finding

The pending feed works for the market bot and the observed arbitrage competitor. However, the only market-bot sample provided about **1.23 seconds of notification lead**. That is insufficient evidence to promise reliable front-running. The strongest directly observed execution opportunity was an arbitrage pair mined **one block after** the market bot and earning approximately **46.75 f564 before native ETH gas**.

Evidence files:

- `pending_observations-20261117T223346035819Z.jsonl`: original pending/mined notifications and local arrival times.
- `pending_watch_summary.json`: window counts, matched hashes, finalized stop state and decoded receipt evidence.
- `pending_watch_receipts.json`: all five public receipts.
- `pending_status.json`: final observer status.

Explorer references:

- [Market-bot transaction](https://sepolia.etherscan.io/tx/0x33ee381022d378cc30b0061599b5e40a9aeb4d0e86c6c86af09bf60299f7468d).
- First arbitrage pair: [buy](https://sepolia.etherscan.io/tx/0xddd6caf673dc38b2091dc1525fb76328446143018ae7f851f5c4bc8ba32273d1), [sell](https://sepolia.etherscan.io/tx/0xbc843ceabaca79201ff82ce44275947004344b0f7deb7b186f3ab6aca0e96608).
- Second pair: [buy](https://sepolia.etherscan.io/tx/0x2986416e7491c38287af7b2e0a1adb03d9de9b5c91e2d1ebfb0dbe23013cd2b8), [sell](https://sepolia.etherscan.io/tx/0x726d88ef1b0959b1b1fb388e37ce57f8571c074d7203ba9a2ee520dc865a3e7d).
