import json
import sys

from web3 import Web3

# Read-only: asks a public node to trace transactions that already happened.
URL = "https://ethereum-sepolia-rpc.publicnode.com"
BOT = Web3.to_checksum_address("0x418f86d4f99ce7fb7d045db23c933558072a02df")  # the "to" of the failed txs
FAILED = sys.argv[1] if len(sys.argv) > 1 else "0xbbfca57acbe8d5538f281a5cb52275c1d59591c1d0ef01a66b303fc3bf4ac1e7"
NAMES = {
    "0x04e45aaf": "router.exactInputSingle",
    "0x128acb08": "pool.swap",
    "0x095ea7b3": "token.approve",
    "0x23b872dd": "token.transferFrom",
    "0xa9059cbb": "token.transfer",
    "0xfa461e33": "uniswapV3SwapCallback",
    "0x70a08231": "token.balanceOf",
    "0x3bf237d5": "bot.swap",
}
w3 = Web3(Web3.HTTPProvider(URL, request_kwargs={"timeout": 60}))


def lines(tx_hash):
    res = w3.provider.make_request("trace_transaction", [tx_hash])["result"]
    for t in res:
        a, r = t["action"], t.get("result") or {}
        sel = (a.get("input") or "")[:10]
        depth = len(t["traceAddress"])
        name = NAMES.get(sel, sel)
        gas = int(r.get("gasUsed", "0x0"), 16)
        out = (r.get("output") or "")[:74]
        err = t.get("error")
        print(f"{'  ' * depth}{name:26s} to {str(a['to'])[:10]} gas {gas:>7} {('ERROR ' + err + ' out=' + out) if err else ''}")


print("== FAILED", FAILED)
lines(FAILED)

# a successful swap of the same bot contract just before, for comparison
rc = w3.eth.get_transaction_receipt(FAILED)
n = rc["blockNumber"]
logs = w3.eth.get_logs({"fromBlock": n - 3000, "toBlock": n, "address": BOT})
ok = [lg for lg in logs if bytes(lg["topics"][0]).hex() == Web3.keccak(text="Swap(address,address,uint256,uint256)").hex().removeprefix("0x")]
print(f"\nsuccessful bot swaps in the 3000 blocks before: {len(ok)}")
if ok:
    pick = ok[-1]
    h = "0x" + bytes(pick["transactionHash"]).hex()
    print("== SUCCESS", h, "block", pick["blockNumber"])
    lines(h)
