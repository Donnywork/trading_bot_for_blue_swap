# read-only: per-swap detail for the last N hours (no key, no tx)
import os
import sys

sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
import time

import bot_contract
import config
import pool_monitor as pm
from main import redact


def _quiet_crash(exc_type, exc, tb):
    # an HTTP error message contains the full RPC URL (and its API key): print it redacted only
    print(redact(f"{exc_type.__name__}: {exc}"), file=sys.stderr)
    os._exit(1)


sys.excepthook = _quiet_crash


def call(fn, *a):
    # throttle + backoff on 429; errors are redacted so the RPC URL never reaches the output
    for i in range(8):
        try:
            time.sleep(0.25)
            return fn(*a)
        except Exception as e:
            if "429" not in str(e) or i == 7:
                print("read failed:", redact(str(e))[:200]); sys.exit(1)
            time.sleep(2 ** i)
blocks = {}


START = int(sys.argv[1])
w3 = bot_contract.connect_read_only()
pool = w3.eth.contract(address=w3.to_checksum_address(os.environ["POOL_ADDRESS"]), abi=config.pool_abi)
s0, d0 = config.read_token(w3, pool.functions.token0().call())
s1, d1 = config.read_token(w3, pool.functions.token1().call())
latest = w3.eth.block_number
logs = pm.fetch_swaps(pool, START, latest)
print("latest block", latest, "ts", call(w3.eth.get_block, latest)["timestamp"])
print("fee", pool.functions.fee().call(), "liquidity", pool.functions.liquidity().call(), "swaps", len(logs))
rows = []
for e in logs:
    a = e["args"]
    tx = call(w3.eth.get_transaction, e["transactionHash"])
    if e["blockNumber"] not in blocks:
        blocks[e["blockNumber"]] = call(w3.eth.get_block, e["blockNumber"])
    blk = blocks[e["blockNumber"]]
    price = (a["sqrtPriceX96"] / 2**96) ** 2 * 10 ** (d0 - d1)
    rows.append(dict(block=e["blockNumber"], idx=e["transactionIndex"], ts=blk["timestamp"],
        sender=a["sender"][-6:], recip=a["recipient"][-6:], frm=tx["from"][-6:],
        side="buy" if a["amount0"] < 0 else "sell", base=abs(a["amount0"]) / 10**d0,
        quote=abs(a["amount1"]) / 10**d1, price=price, gp=tx.get("gasPrice", 0) / 1e9,
        base_fee=blk.get("baseFeePerGas", 0) / 1e9, ntx=len(blk["transactions"])))
prev = None
for r in rows:
    r["impact"] = (r["price"] / prev - 1) * 100 if prev else 0.0
    prev = r["price"]
import json

json.dump(rows, open(sys.argv[2], "w"))
print("saved", len(rows))
