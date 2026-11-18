"""Decode public transaction calldata offline and inspect target account code read-only."""

import json
from collections import Counter

from analyze import ROOT, TARGET, load, save
from collect import rpc
from web3 import Web3


def main():
    transactions = load("transactions.json")
    manifest = load("manifest.json")
    assert len(transactions) == manifest["transactions"]
    w3 = Web3()
    router = w3.eth.contract(abi=json.loads((ROOT / "abis/router.json").read_text()))

    def decode(data):
        try:
            fn, args = router.decode_function_input(data)
            if fn.fn_name == "multicall":
                return {
                    "function": fn.fn_name,
                    "args": {k: v for k, v in args.items() if k != "data"},
                    "calls": [decode(x) for x in args["data"]],
                }
            return {"function": fn.fn_name, "args": args}
        except (ValueError, TypeError):
            return {
                "selector": (data if isinstance(data, str) else "0x" + data.hex())[:10],
                "unknown_abi": True,
            }

    calls = {
        h: {
            "from": tx["from"].lower(),
            "to": (tx["to"] or "").lower(),
            "decoded": decode(tx["input"]),
        }
        for h, tx in transactions.items()
    }
    # Router paths are bytes; preserve their exact public calldata in JSON.
    safe = json.loads(json.dumps(calls, default=lambda b: "0x" + b.hex()))
    save("decoded_calls.json", safe)
    target = [
        tx
        for tx in transactions.values()
        if TARGET in (tx["from"].lower(), (tx["to"] or "").lower())
    ]
    info = {
        "target": TARGET,
        "matched_transactions": len(target),
        "from_counts": dict(Counter(tx["from"].lower() for tx in target)),
        "to_counts": dict(Counter((tx["to"] or "").lower() for tx in target)),
        "selectors": dict(Counter(tx["input"][:10] for tx in target)),
        "all_top_signers": Counter(
            tx["from"].lower() for tx in transactions.values()
        ).most_common(10),
    }
    previous = load("target_identity.json", {})
    if "target_code_bytes_at_end" in previous:
        info["target_code_bytes_at_end"] = previous["target_code_bytes_at_end"]
    else:
        try:
            code = rpc("eth_getCode", [TARGET, hex(manifest["end_block"])])
            info["target_code_bytes_at_end"] = (len(code) - 2) // 2
        except (ValueError, RuntimeError) as exc:
            info["code_lookup_error"] = str(exc)
    save("target_identity.json", info)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
