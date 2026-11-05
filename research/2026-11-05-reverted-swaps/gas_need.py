
from web3 import Web3

# Read-only: how much gas each failed swap needed in the state it was sent against, versus the state
# it was mined in. Public node, no key, nothing sent.
URL = "https://ethereum-sepolia-rpc.publicnode.com"
FAILED = {
    "07:52 sell": "0x9de4e81697e708a08c362feb4f7c6c708ac33157b3a0077944ce7441e4208d49",
    "08:31 sell": "0xe17ad05904bd6f92afad3a683b3e84c23e1fcc1e929451e566381a5585b6d395",
    "10:08 buy": "0xe1e28e0bf1134bffe01afeb9bb562f8002d0763b937b60855ad417721e3b0259",
    "12:25 sell": "0xbbfca57acbe8d5538f281a5cb52275c1d59591c1d0ef01a66b303fc3bf4ac1e7",
}
w3 = Web3(Web3.HTTPProvider(URL, request_kwargs={"timeout": 60}))


def need(call, block):
    for _ in range(4):
        try:
            return w3.eth.estimate_gas(call, block_identifier=block)
        except Exception as e:  # noqa: BLE001
            last = e
    return f"({type(last).__name__})"


print(f"{'':12s} {'gas limit':>10s} {'need before':>12s} {'need at end':>12s}   (limit = need-before x buffer)")
for label, h in FAILED.items():
    tx = w3.eth.get_transaction(h)
    n = w3.eth.get_transaction_receipt(h)["blockNumber"]
    call = {"from": tx["from"], "to": tx["to"], "data": tx["input"]}
    before, end = need(call, n - 1), need(call, n)
    print(f"{label:12s} {tx['gas']:>10} {before!s:>12} {end!s:>12}")


def runs(call, block, gas):
    # does the call succeed in that state with exactly this gas limit?
    for _ in range(4):
        try:
            w3.eth.call({**call, "gas": gas}, block_identifier=block)
            return "succeeds"
        except Exception as e:  # noqa: BLE001
            text = str(e)
            if "historical state" in text:
                continue
            return "FAILS (" + text[:50] + ")"
    return "(state not available)"


print("\nthe same call in the state it was mined in, with different gas limits:")
for label, h in FAILED.items():
    tx = w3.eth.get_transaction(h)
    n = w3.eth.get_transaction_receipt(h)["blockNumber"]
    call = {"from": tx["from"], "to": tx["to"], "data": tx["input"]}
    print(f"{label:12s} limit {tx['gas']}: {runs(call, n, tx['gas'])};   limit {tx['gas'] * 2}: {runs(call, n, tx['gas'] * 2)}")
