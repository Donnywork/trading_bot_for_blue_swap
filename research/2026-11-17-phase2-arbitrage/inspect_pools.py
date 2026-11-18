"""Read public Sepolia data only. No dotenv, keys, signing, or transaction methods.

Run from repo root: uv run python research/2026-11-17-phase2-arbitrage/inspect_pools.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

OUT = Path(__file__).parent
RPC = "https://fin564.vercel.app/api/rpc/sepolia"
QUOTE = "0x21abd410c4240CCC170D004fEfB6405943e0d7Ff"
BASE = "0x2238115a9B6469DEE6c004B00A0367fFba2Fd1BC"
POOLS = ["0x68D8bC396df8e791EFd45A9b214CB07fe4300f48", "0xC67Fcb8459C792aE1E271f24CE212Ba90eCC2920"]
QUOTER = "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3"
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL_ABI = [{"type": "function", "name": "aggregate3", "stateMutability": "payable",
    "inputs": [{"name": "calls", "type": "tuple[]", "components": [
        {"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"}]}],
    "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
        {"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}]}]}]


def batch(w3, multicall, pool, name, arguments, types, block):
    result = []
    for start in range(0, len(arguments), 40):
        calls = [(pool.address, False, pool.encode_abi(name, args=[a])) for a in arguments[start:start + 40]]
        rows = multicall.functions.aggregate3(calls).call(block_identifier=block)
        result.extend(w3.codec.decode(types, data) for ok, data in rows if ok)
        time.sleep(0.25)
    if len(result) != len(arguments):
        raise ValueError("incomplete multicall")
    return result


def inspect(w3, multicall, address, block):
    pool = w3.eth.contract(address=address, abi=config.pool_abi)
    call = lambda fn: fn.call(block_identifier=block)
    token0, token1 = call(pool.functions.token0()), call(pool.functions.token1())
    assert {token0, token1} == {QUOTE, BASE}
    fee, spacing = call(pool.functions.fee()), call(pool.functions.tickSpacing())
    sqrt, tick, *_ = call(pool.functions.slot0())
    active = call(pool.functions.liquidity())
    words = list(range((config.MIN_TICK // spacing) >> 8, ((config.MAX_TICK // spacing) >> 8) + 1))
    bitmap = batch(w3, multicall, pool, "tickBitmap", words, ["uint256"], block)
    indexes = [(word * 256 + bit) * spacing for word, (bits,) in zip(words, bitmap, strict=True)
               for bit in range(256) if bits >> bit & 1]
    rows = batch(w3, multicall, pool, "ticks", indexes,
                 ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"], block)
    ticks = [{"tick": t, "gross": row[0], "net": row[1]} for t, row in zip(indexes, rows, strict=True)]
    assert sum(t["net"] for t in ticks if t["tick"] <= tick) == active
    assert sum(t["net"] for t in ticks) == 0
    profile = {"block": block, "pool_address": address.lower(), "base_address": BASE.lower(),
               "quote_address": QUOTE.lower(), "base_is_token0": token0 == BASE,
               "fee": fee, "spacing": spacing, "d0": 18, "d1": 18,
               "sqrtPriceX96": sqrt, "tick": tick, "liquidity": active, "ticks": ticks,
               "factory": call(pool.functions.factory())}
    price = config.quote_per_base(sqrt, 18, 18, token0 == BASE)
    intervals, liquidity = [], 0
    for lo, hi in zip(ticks, ticks[1:]):
        liquidity += lo["net"]
        prices = sorted([1.0001 ** (-lo["tick"]), 1.0001 ** (-hi["tick"])])
        intervals.append({"lower_f564": prices[0], "upper_f564": prices[1],
                          "liquidity": liquidity, "active": lo["tick"] <= tick < hi["tick"]})
    profile["price_f564"] = price
    profile["intervals"] = sorted(intervals, key=lambda r: r["lower_f564"])
    print(f"fee={fee} price={price:.8f} initialized_ticks={len(ticks)} bitmap_words={len(words)}", flush=True)
    return profile


def main():
    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 45}))
    assert w3.eth.chain_id == 11155111
    block = w3.eth.block_number
    header = w3.eth.get_block(block)
    multicall = w3.eth.contract(address=MULTICALL, abi=MULTICALL_ABI)
    print(f"Reading both pools at block {block}", flush=True)
    profiles = [inspect(w3, multicall, address, block) for address in POOLS]
    quoter = w3.eth.contract(address=QUOTER, abi=config.quoter_abi)
    cycles = []
    for buy, sell in ((500, 10000), (10000, 500)):
        for size in (1, 10, 100, 250, 500, 1000, 2500, 5000, 10000):
            amount = size * 10**18
            row = {"buy_fee": buy, "sell_fee": sell, "input_f564": size}
            try:
                base, sqrt_buy, ticks_buy, gas_buy = quoter.functions.quoteExactInputSingle((QUOTE, BASE, amount, buy, 0)).call(block_identifier=block)
                quote, sqrt_sell, ticks_sell, gas_sell = quoter.functions.quoteExactInputSingle((BASE, QUOTE, base, sell, 0)).call(block_identifier=block)
                row.update({"base_raw": str(base), "quote_raw": str(quote),
                            "profit_f564": str(Decimal(quote - amount) / 10**18),
                            "return_pct": str(Decimal(quote - amount) / amount * 100),
                            "buy_sqrt_after": str(sqrt_buy), "sell_sqrt_after": str(sqrt_sell),
                            "ticks_crossed": [ticks_buy, ticks_sell], "quoter_gas_estimates": [gas_buy, gas_sell]})
            except Exception as exc:
                row["error"] = type(exc).__name__  # never include a provider URL/error body
            cycles.append(row)
            time.sleep(0.25)
    assert w3.eth.get_block(block)["hash"] == header["hash"], "snapshot block was reorganized"
    data = {"chain_id": 11155111, "block": block, "block_hash": header["hash"].to_0x_hex(),
            "timestamp": header["timestamp"], "utc": datetime.fromtimestamp(header["timestamp"], timezone.utc).isoformat(),
            "source": RPC, "profiles": profiles, "cycles": cycles}
    (OUT / "snapshot.json").write_text(json.dumps(data, indent=2) + "\n")
    for row in cycles:
        print(row["buy_fee"], row["input_f564"], row.get("profit_f564", row.get("error")), flush=True)


if __name__ == "__main__":
    main()
