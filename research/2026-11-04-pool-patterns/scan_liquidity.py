# Read the pool's whole liquidity layout (every initialised tick and its net liquidity). Read-only:
# no key, no transaction; errors go through redact() so the RPC URL never reaches the output.
#   uv run python research/2026-11-04-pool-patterns/scan_liquidity.py OUT.json
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))
import bot_contract
import config
from main import redact


def _quiet_crash(exc_type, exc, tb):
    # an HTTP error message contains the full RPC URL (and its API key): print it redacted only
    print(redact(f"{exc_type.__name__}: {exc}"), file=sys.stderr)
    os._exit(1)


sys.excepthook = _quiet_crash

MIN_TICK, MAX_TICK = -887272, 887272


def call(fn):
    for i in range(8):
        try:
            time.sleep(0.2)
            return fn()
        except Exception as e:  # noqa: BLE001
            if "429" not in str(e) or i == 7:
                sys.exit("read failed: " + redact(str(e))[:200])
            time.sleep(2**i)


w3 = bot_contract.connect_read_only()
pool = w3.eth.contract(address=w3.to_checksum_address(os.environ["POOL_ADDRESS"]), abi=config.pool_abi)
spacing = call(pool.functions.tickSpacing().call)
slot0 = call(pool.functions.slot0().call)
liquidity = call(pool.functions.liquidity().call)
_, d0 = config.read_token(w3, call(pool.functions.token0().call))
_, d1 = config.read_token(w3, call(pool.functions.token1().call))
ticks = []
for word in range((MIN_TICK // spacing) >> 8, ((MAX_TICK // spacing) >> 8) + 1):
    bits = call(pool.functions.tickBitmap(word).call)
    for bit in range(256):
        if bits >> bit & 1:
            tick = (word * 256 + bit) * spacing
            info = call(pool.functions.ticks(tick).call)
            ticks.append({"tick": tick, "gross": info[0], "net": info[1]})
json.dump(
    {"block": w3.eth.block_number, "tick": slot0[1], "sqrtPriceX96": slot0[0], "liquidity": liquidity,
     "spacing": spacing, "fee": call(pool.functions.fee().call), "d0": d0, "d1": d1, "ticks": ticks},
    open(sys.argv[1], "w"),
)
print("ticks initialised:", len(ticks), "| current tick", slot0[1], "| active liquidity", liquidity)
