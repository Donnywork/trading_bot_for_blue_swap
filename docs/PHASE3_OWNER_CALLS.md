# Enable automatic Phase 3 liquidations

Use your enrolled wallet `0xB6bf0Ba3F43D4386216Df037dcA1A1203038e135` on **Sepolia**. For both transactions, set the destination to your vault `0x346B0c324963B91afb6f722d126E90506623bB8d` and the amount to **0 ETH**. Paste the corresponding hex into the wallet's transaction data field. Each signature authorizes liquidations funded with one token. Borrowing stays disabled and each debt cap is zero.

### 1. Enable f564 repayments

This sets the reserve to **6,000 f564** and the maximum liquidation repayment to **1,000 f564** per transaction.

```text
0x71989d73000000000000000000000000c319f958df8a74b077578b437c402dc7d7687864000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000014542ba12a337c0000000000000000000000000000000000000000000000000003635c9adc5dea00000
```

### 2. Enable fETH repayments

This sets the reserve to **6 fETH** and the maximum liquidation repayment to **1 fETH** per transaction.

```text
0x71989d73000000000000000000000000e00296a373cd19e6abaf085e50a393938879737d000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000053444835ec5800000000000000000000000000000000000000000000000000000de0b6b3a7640000
```

The worker reads the permissions from the vault as each transaction confirms. It simulates and submits eligible atomic closes automatically. You can check the current status on `xyz` in `logs/phase3/auto.json`.
