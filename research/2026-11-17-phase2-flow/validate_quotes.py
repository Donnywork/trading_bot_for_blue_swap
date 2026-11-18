"""Public read-only archive quotes for selected reconstructed opportunities."""

import json
from decimal import Decimal
from pathlib import Path

from collect import rpc, write
from web3 import Web3

OUT = Path(__file__).parent
QUOTE = "0x21abd410c4240CCC170D004fEfB6405943e0d7Ff"
BASE = "0x2238115a9B6469DEE6c004B00A0367fFba2Fd1BC"
QUOTER = "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3"


def main():
    opportunities = json.loads((OUT / "opportunities.json").read_text())
    positive = [r for r in opportunities if r["profit"] > 0.01]
    candidates = (
        sorted(positive, key=lambda r: r["profit"], reverse=True)[:5] + positive[-3:]
    )
    w3 = Web3()
    selector = Web3.keccak(
        text="quoteExactInputSingle((address,address,uint256,uint24,uint160))"
    )[:4]

    def quote(token_in, token_out, amount, fee, block):
        data = selector + w3.codec.encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(token_in, token_out, amount, fee, 0)],
        )
        result = rpc(
            "eth_call", [{"to": QUOTER, "data": "0x" + data.hex()}, hex(block)]
        )
        return w3.codec.decode(
            ["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(result[2:])
        )

    results = []
    for row in candidates:
        amount = int(Decimal(str(row["input"])) * 10**18)
        saved = {
            "block": row["block"],
            "buy_fee": row["buy_fee"],
            "sell_fee": row["sell_fee"],
            "input_raw": str(amount),
            "model_profit": row["profit"],
        }
        try:
            base = quote(QUOTE, BASE, amount, row["buy_fee"], row["block"])
            final = quote(BASE, QUOTE, base[0], row["sell_fee"], row["block"])
            gain = Decimal(final[0] - amount) / 10**18
            saved.update(
                {
                    "base_raw": str(base[0]),
                    "output_raw": str(final[0]),
                    "profit_f564": str(gain),
                    "absolute_model_error": abs(float(gain) - row["profit"]),
                    "ticks_crossed": [base[2], final[2]],
                    "quoter_gas": [base[3], final[3]],
                }
            )
            assert saved["absolute_model_error"] < 0.000001
        except (ValueError, RuntimeError) as exc:
            saved["error"] = str(exc)
        results.append(saved)
        write("archive_quotes.json", results)
        print(json.dumps(saved), flush=True)


if __name__ == "__main__":
    main()
