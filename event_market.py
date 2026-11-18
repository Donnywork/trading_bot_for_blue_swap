"""Offline Phase 2 transaction decoding and hypothetical pool-state simulation."""

import copy
import json
import math
from functools import cache
from pathlib import Path

from eth_abi.exceptions import DecodingError
from web3 import Web3

import v3pool

QUOTE = "0x21abd410c4240ccc170d004fefb6405943e0d7ff"
BASE = "0x2238115a9b6469dee6c004b00a0367ffba2fd1bc"
ROUTER = "0x3bfa4769fb09eefc5a80d6e87c3b9c650f7ae48e"
POOLS = {
    500: "0x68d8bc396df8e791efd45a9b214cb07fe4300f48",
    10000: "0xc67fcb8459c792ae1e271f24ce212ba90ecc2920",
}
MARKET_BOT = "0x6011139d5f16689bde1d245c6247b088f9582026"
UNIT = 10**18


class PredictionRejected(ValueError):
    """A fixed local explanation, safe to publish without provider error bodies."""


def integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    raise ValueError("expected integer or RPC hex quantity")


def hex_value(value, size):
    if (
        not isinstance(value, str)
        or not value.startswith("0x")
        or len(value) != 2 + size * 2
    ):
        raise ValueError("invalid hexadecimal identity")
    bytes.fromhex(value[2:])
    return value.lower()


@cache
def router():
    abi = json.loads((Path(__file__).parent / "abis/router.json").read_text())
    return Web3().eth.contract(abi=abi)


def transaction_identity(tx):
    if integer(tx.get("chainId")) != 11155111:
        raise ValueError("expected Sepolia transaction")
    sender = hex_value(tx.get("from"), 20)
    tx_hash = hex_value(tx.get("hash"), 32)
    nonce = integer(tx.get("nonce"))
    if nonce < 0:
        raise ValueError("negative nonce")
    return sender, nonce, tx_hash


def decode_swap(tx, now):
    sender, nonce, tx_hash = transaction_identity(tx)
    if tx.get("blockNumber") is not None:
        raise PredictionRejected("transaction already has a block")
    if (tx.get("to") or "").lower() != ROUTER or integer(tx.get("value", 0)) != 0:
        raise PredictionRejected("unsupported destination or native value")
    try:
        fn, args = router().decode_function_input(tx["input"])
        deadline = None
        if fn.fn_name == "multicall":
            # Multiple/nested calls, previous-block checks, permits and balance sentinels
            # need their full execution semantics; never silently discard them.
            if set(args) != {"deadline", "data"} or len(args["data"]) != 1:
                raise PredictionRejected("unsupported multicall")
            deadline = args["deadline"]
            if deadline < now:
                raise PredictionRejected("target expired")
            fn, args = router().decode_function_input(args["data"][0])
        if fn.fn_name != "exactInputSingle":
            raise PredictionRejected("only exactInputSingle is supported")
        params = args["params"]
    except (KeyError, TypeError, DecodingError) as exc:
        raise PredictionRejected("malformed swap calldata") from exc
    token_in, token_out = params["tokenIn"].lower(), params["tokenOut"].lower()
    if {token_in, token_out} != {QUOTE, BASE} or params["fee"] not in POOLS:
        raise PredictionRejected("unsupported pair or fee")
    if params["recipient"].lower() != sender:
        raise PredictionRejected("unsupported recipient")
    if params["sqrtPriceLimitX96"] != 0:
        raise PredictionRejected(
            "target price limit requires exact partial-fill simulation"
        )
    if not 0 < params["amountIn"] < 2**255 or params["amountOutMinimum"] <= 0:
        raise PredictionRejected("unsupported input sentinel or zero minimum")
    return {
        "hash": tx_hash,
        "sender": sender,
        "nonce": nonce,
        "fee": params["fee"],
        "side": "buy" if token_in == QUOTE else "sell",
        "amount_in": params["amountIn"],
        "minimum_out": params["amountOutMinimum"],
        "deadline": deadline,
    }


def models(snapshot, now, max_age=30):
    if snapshot.get("chain_id") != 11155111:
        raise ValueError("snapshot must be Sepolia")
    block, timestamp = snapshot.get("block"), snapshot.get("timestamp")
    if type(block) is not int or block < 0 or type(timestamp) is not int:
        raise ValueError("invalid snapshot block/time")
    hex_value(snapshot.get("block_hash"), 32)
    if not 0 <= now - timestamp <= max_age:
        raise PredictionRejected("stale or future snapshot")
    profiles = snapshot.get("profiles", [])
    if len(profiles) != 2 or {p.get("fee") for p in profiles} != set(POOLS):
        raise ValueError("snapshot needs both official pools")
    result = {}
    for p in profiles:
        v3pool.check_profile(p, "event snapshot")
        fee = p["fee"]
        if (
            p.get("pool_address", "").lower() != POOLS[fee]
            or p.get("base_address", "").lower() != BASE
            or p.get("quote_address", "").lower() != QUOTE
            or p.get("base_is_token0") is not False
            or p["d0"] != 18
            or p["d1"] != 18
            or p["block"] != block
        ):
            raise ValueError("snapshot market identity mismatch")
        ticks = sorted(p["ticks"], key=lambda t: t["tick"])
        if len({t["tick"] for t in ticks}) != len(ticks):
            raise ValueError("duplicate liquidity ticks")
        running = 0
        for tick in ticks:
            running += tick["net"]
            if running < 0:
                raise ValueError("negative range liquidity")
        if running != 0:
            raise ValueError("unbalanced liquidity layout")
        sqrt, tick = p.get("sqrtPriceX96"), p.get("tick")
        if (
            type(sqrt) is not int
            or not 4295128739 < sqrt < 1461446703485210103287273052203988822378723970342
            or type(tick) is not int
        ):
            raise ValueError("invalid pool price")
        active = sum(t["net"] for t in ticks if t["tick"] <= tick)
        inferred_tick = math.floor(math.log((sqrt / 2**96) ** 2) / math.log(1.0001))
        if abs(inferred_tick - tick) > 1:
            raise ValueError("tick and sqrt price disagree")
        if active != p.get("liquidity") or active <= 0:
            raise ValueError("active liquidity mismatch")
        pool = v3pool.V3Pool(p, (2**96 / sqrt) ** 2)
        # Exact tick-boundary conventions aren't represented by this float model.
        if not math.isclose(pool.L * UNIT, active, rel_tol=1e-10, abs_tol=1):
            raise ValueError("ambiguous tick-boundary state")
        result[fee] = pool
    return result


def apply_target(pools, swap):
    result = copy.deepcopy(pools)
    pool = result[swap["fee"]]
    amount = swap["amount_in"] / UNIT
    output = (
        pool.buy_with_quote(amount) if swap["side"] == "buy" else pool.sell_base(amount)
    )
    minimum = swap["minimum_out"] / UNIT
    # Float estimates need a margin at exact contract limits. Never round them into calldata.
    if not math.isfinite(output) or output <= minimum + max(1e-12, abs(output) * 1e-10):
        raise PredictionRejected("target minimum output not safely met")
    return result, output
