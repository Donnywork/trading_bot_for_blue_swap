"""Forward comparison of keeping NFT 234996 full range and concentrating half its liquidity."""

import argparse
import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal, localcontext
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from web3 import Web3

import config
import ledger
import operator_lock
from phase3_chain import FEE, POOL, WAD, Chain
from phase3_spot import ENTRY_DISCOUNT_BPS
from phase_markets import PHASE3

FULL = (-887220, 887220)
BAND = (-69180, -69000)
NFT = 234996
MANAGER = Web3.to_checksum_address("0x1238536071e1c677a632429e3655c799b22cda52")
OWNER = "0xb6bf0ba3f43d4386216df037dca1a1203038e135"
D = Decimal
FRACTION = D(FEE) / 10**6
SQRT_FIELD = next(
    item["name"]
    for event in config.pool_abi
    if event.get("name") == "Swap"
    for item in event["inputs"]
    if item["type"] == "uint160"
)


@lru_cache(maxsize=1024)
def root(tick):
    return D("1.0001") ** (D(tick) / 2)


def tokens(liquidity, price, bounds):
    low, high = map(root, bounds)
    inside = min(high, max(low, price))
    return liquidity * (1 / inside - 1 / high), liquidity * (inside - low)


def allocation(start):
    old = D(start["liquidity_raw"]) / WAD
    moved = D(start["liquidity_raw"] // 2) / WAD
    price = D(start["sqrt_x96"]) / 2**96
    budget = tokens(moved, price, FULL)
    unit = tokens(D(1), price, BAND)
    narrow = min(b / u for b, u in zip(budget, unit, strict=True) if u)
    return (
        old,
        old - moved,
        narrow,
        tuple(b - narrow * u for b, u in zip(budget, unit, strict=True)),
    )


def anchor(db, block):
    row = db.execute("SELECT hash FROM anchors WHERE block=?", (block,)).fetchone()
    if row is None:
        raise ValueError("missing journal anchor")
    return row[0]


def sale_to_entry(price, mark, net):
    """Gross fETH input to reach the entry price with this depth and TWAP held fixed."""
    target = (D(10000) / ((10000 - ENTRY_DISCOUNT_BPS) * mark)).sqrt()
    if target <= price:
        return 0.0
    edges = sorted({root(t) for t in net if price < root(t) < target})
    amount = D(0)
    for a, b in pairwise([price, *edges, target]):
        liquidity = sum(v for t, v in net.items() if root(t) <= (a + b) / 2)
        if liquidity < 0:
            raise ValueError("negative liquidity on sale path")
        if b == target and not liquidity:
            return None
        amount += liquidity * (b - a)
    return float(amount / (WAD * (1 - FRACTION)))


def begin(chain, market, block_hash):
    block = market["block"]
    if chain.w3.eth.chain_id != 11155111 or chain.header(block)[1] != block_hash:
        raise ValueError("invalid initial Sepolia anchor")
    w3 = chain.w3

    def nft_read(method, types):
        data = Web3.keccak(text=method)[:4] + w3.codec.encode(["uint256"], [NFT])
        return w3.codec.decode(
            types, w3.eth.call({"to": MANAGER, "data": data}, block_identifier=block)
        )

    (owner,) = nft_read("ownerOf(uint256)", ["address"])
    position = nft_read(
        "positions(uint256)",
        [
            "uint96",
            "address",
            "address",
            "address",
            "uint24",
            "int24",
            "int24",
            "uint128",
            "uint256",
            "uint256",
            "uint128",
            "uint128",
        ],
    )
    if (
        owner.lower() != OWNER
        or position[2:7] != (PHASE3.quote.lower(), PHASE3.base.lower(), FEE, *FULL)
        or position[7] <= 0
    ):
        raise ValueError("unexpected LP position")
    (slot,) = chain.views(block, [("pool", "slot0")])
    _, canonical, timestamp = chain.header(block)
    if canonical != block_hash or slot[1] != market["tick"]:
        raise ValueError("pool changed during initialization")
    return {
        "version": 1,
        "block": block,
        "hash": canonical,
        "timestamp": timestamp,
        "pool": POOL,
        "nft": NFT,
        "band": list(BAND),
        "liquidity_raw": position[7],
        "sqrt_x96": slot[0],
        "tick": slot[1],
        "protocol": slot[5],
        "net": market["liquidity_net_by_tick"],
    }


def fee_shares(start_price, end_price, tick, net, side, old, kept, narrow):
    # Split actual input across the observed V3 price path, including both paper boundaries.
    # Each interval gets its own external liquidity and diluted paper fee share.
    low, high = sorted((start_price, end_price))
    cuts = [
        low,
        *sorted({root(t) for t in (*net, *BAND, *FULL) if low < root(t) < high}),
        high,
    ]
    weights = [D(0), D(0), D(0)]
    for a, b in pairwise(cuts):

        def active(k, mid=(a + b) / 2):
            return k <= tick if low == high else root(k) <= mid

        actual = D(sum(v for k, v in net.items() if active(k))) / WAD
        if actual == 0 and low != high:
            continue
        full_active = active(FULL[0]) and not active(FULL[1])
        original = old if full_active else D(0)
        paper = (kept if full_active else D(0)) + (
            narrow if active(BAND[0]) and not active(BAND[1]) else D(0)
        )
        if actual <= 0 or actual < original:
            raise ValueError("observed liquidity below reference position")
        weight = (
            D(1) if a == b else actual * ((1 / a - 1 / b) if side == 0 else (b - a))
        )
        weights[0] += weight
        weights[1] += weight * original / actual
        if paper:
            weights[2] += weight * paper / (actual - original + paper)
    if weights[0] <= 0:
        raise ValueError("no liquidity on swap path")
    return weights[1] / weights[0], weights[2] / weights[0]


def evaluate(start, rows, market):
    if (start["version"], start["pool"], start["nft"], tuple(start["band"])) != (
        1,
        POOL,
        NFT,
        BAND,
    ):
        raise ValueError("paper experiment identity changed")
    old, kept, narrow, idle = allocation(start)
    net = Counter({int(k): v for k, v in start["net"].items()})
    # Amounts in token units. Each fee stays in the token paid until the current valuation.
    earned = defaultdict(lambda: [[D(0), D(0)], [D(0), D(0)]])
    protocol = start["protocol"]
    tick = start["tick"]
    price = D(start["sqrt_x96"]) / 2**96
    swap_signers = set()
    swaps = sales = flashes = 0
    max_sale_drop = D(0)
    for row in rows:
        a = row["args"]
        event = row["event"]
        if event in ("Mint", "Burn"):
            if (
                event == "Burn"
                and a["amount"]
                and (a["tickLower"], a["tickUpper"]) == FULL
                and row["signer"].lower() == OWNER
            ):
                raise ValueError("owner changed full-range reference position")
            change = a["amount"] if event == "Mint" else -a["amount"]
            net[a["tickLower"]] += change
            net[a["tickUpper"]] -= change
        elif event == "SetFeeProtocol":
            protocol = a["feeProtocol0New"] | (a["feeProtocol1New"] << 4)
        elif event == "Flash":
            flashes += 1
            shares = fee_shares(price, price, tick, net, 0, old, kept, narrow)
            for side, denom in enumerate((protocol & 15, protocol >> 4)):
                # Flash.paid is the pool's net balance gain, including donations.
                paid = a[f"paid{side}"]
                gain = D(paid - (paid // denom if denom else 0)) / WAD
                for leg, share in enumerate(shares):
                    earned[row["signer"].lower()][leg][side] += gain * share
        elif event == "Swap":
            next_price = D(a[SQRT_FIELD]) / 2**96
            side = 0 if a["amount0"] > 0 else 1
            paid = D(a[f"amount{side}"]) / WAD
            denom = (protocol & 15) if side == 0 else (protocol >> 4)
            if paid < 0 or (
                next_price != price and (next_price < price) != (side == 0)
            ):
                raise ValueError("inconsistent swap direction")
            fees = paid * FRACTION * (1 - D(1) / denom if denom else 1)
            shares = fee_shares(price, next_price, tick, net, side, old, kept, narrow)
            actor = row["signer"].lower()
            swap_signers.add(actor)
            for leg, share in enumerate(shares):
                earned[actor][leg][side] += fees * share
            if side == 1:
                sales += 1
                max_sale_drop = max(
                    max_sale_drop, (1 - (price / next_price) ** 2) * 10000
                )
            tick = a["tick"]
            price = next_price
            swaps += 1
            if sum(v for k, v in net.items() if k <= tick) != a["liquidity"]:
                raise ValueError("swap liquidity mismatch")
    expected = {int(k): v for k, v in market["liquidity_net_by_tick"].items() if v}
    if (
        tick != market["tick"]
        or {k: v for k, v in net.items() if v} != expected
        or sum(v for k, v in net.items() if k <= tick) != market["liquidity"]
    ):
        raise ValueError("journal does not reconcile to market")
    mark = D(str(market["twap"]))
    if (
        mark <= 0
        or market["twap_seconds"] < 1800
        or abs(1 / price**2 / D(str(market["spot"])) - 1) > D("1e-10")
    ):
        raise ValueError("invalid spot or scoring TWAP")

    def value(amounts):
        return amounts[0] + amounts[1] * mark

    full_principal = value(tokens(old, price, FULL))
    split_principal = (
        value(tokens(kept, price, FULL))
        + value(tokens(narrow, price, BAND))
        + value(idle)
    )
    fees = [sum((value(v[i]) for v in earned.values()), D(0)) for i in (0, 1)]
    edge = split_principal - full_principal + fees[1] - fees[0]
    other = {
        k: value(v[1]) - value(v[0])
        for k, v in earned.items()
        if k != PHASE3.market_bot.lower()
    }
    largest = max(other, key=other.get, default=None)
    discount = (1 - 1 / price**2 / mark) * 10000
    return {
        "start_block": start["block"],
        "start_timestamp": start["timestamp"],
        "mark_block": market["block"],
        "forward_swaps": swaps,
        "forward_flashes": flashes,
        "distinct_swap_signers": len(swap_signers),
        "confirmed_sales": sales,
        "largest_sale_spot_drop_bps": float(max_sale_drop),
        "in_narrow_range": BAND[0] <= tick < BAND[1],
        "spot_discount_bps": float(discount),
        "spot_entry_threshold_bps": ENTRY_DISCOUNT_BPS,
        "sale_to_entry_feth": sale_to_entry(price, mark, net),
        "borrowers": len(market["borrower_health_wad"]),
        "liquidatable": sum(
            1 for h in market["borrower_health_wad"].values() if h < WAD
        ),
        "full_nav_f564": float(full_principal + fees[0]),
        "split_nav_f564": float(split_principal + fees[1]),
        "full_fees_f564": float(fees[0]),
        "split_fees_f564": float(fees[1]),
        "inventory_delta_f564": float(split_principal - full_principal),
        "split_minus_full_f564": float(edge),
        "top_participant_fee_contributor": largest,
        "edge_excluding_top_participant_fees_f564": float(
            edge - max(D(0), other.get(largest, D(0)))
        ),
    }


def update(directory, initialize=False, chain=None):
    folder = directory / "lp-paper"
    folder.mkdir(exist_ok=True)
    path = folder / "start.json"
    state = json.loads((directory / "state.json").read_text())
    market = state["market"]
    block = market["block"]
    if (
        state["pool"] != POOL
        or state["phase"] != 3
        or state["health"] != "ready"
        or not 0 <= time.time() - state["updated"] <= 90
        or not 0 <= state["head"] - block <= 16
    ):
        raise ValueError("waiting for fresh Phase 3 market")
    with (
        closing(
            sqlite3.connect(f"file:{directory}/events.sqlite3?mode=ro", uri=True)
        ) as db,
        localcontext() as context,
    ):
        context.prec = 60
        db.execute("BEGIN")
        block_hash = anchor(db, block)
        if (
            anchor(db, state["indexed_block"]) != state["indexed_hash"]
            or block > state["indexed_block"]
        ):
            raise ValueError("monitor anchor changed")
        if initialize:
            if path.exists():
                raise ValueError("paper baseline already exists")
            start = begin(chain or Chain(), market, block_hash)
        else:
            start = json.loads(path.read_text())
        if start["block"] > block or anchor(db, start["block"]) != start["hash"]:
            raise ValueError("paper start block moved")
        rows = db.execute(
            "SELECT row FROM events WHERE block>? AND block<=? "
            "AND kind IN ('Mint','Burn','Swap','Flash','SetFeeProtocol') "
            "AND json_extract(row,'$.contract')='pool' ORDER BY block,idx",
            (start["block"], block),
        )
        result = evaluate(start, (json.loads(row) for (row,) in rows), market)
        if initialize:
            ledger.atomic_write(path, json.dumps(start, indent=2))
    ledger.atomic_write(
        folder / "result.json",
        json.dumps(result | {"status": "ready", "updated": int(time.time())}, indent=2),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "watch", "status"))
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent / "logs/phase3"
    folder = directory / "lp-paper"
    if args.command == "status":
        print((folder / "result.json").read_text())
        return
    with operator_lock.acquire(folder / "paper.lock"):
        if args.command == "start":
            print(json.dumps(update(directory, True), indent=2))
            return
        while True:
            try:
                update(directory)
            except (
                OSError,
                ValueError,
                KeyError,
                sqlite3.Error,
                ArithmeticError,
            ) as exc:
                ledger.atomic_write(
                    folder / "result.json",
                    json.dumps(
                        {
                            "status": "unavailable",
                            "reason": type(exc).__name__,
                            "updated": int(time.time()),
                        }
                    ),
                )
            time.sleep(30)


if __name__ == "__main__":
    main()
