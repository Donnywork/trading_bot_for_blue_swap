"""Coherent Phase 2 snapshots, incrementally maintained from canonical pool logs."""

import copy
import json

from web3 import Web3
from web3.exceptions import BlockNotFound, Web3RPCError

import config
import event_market as market
import ledger
from runtime import rejected_log_range

FACTORY = "0x0227628f3F023bb0B980b67D528571c95c6DaC1c"
QUOTER = "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3"
RESEARCH_SNAPSHOT = config.ROOT / "research/2026-11-17-phase2-arbitrage/snapshot.json"


class SnapshotUnavailable(ValueError):
    """A complete, validated layout requires an explicit dashboard scan."""


def hexstr(value):
    return Web3.to_hex(value).lower()


def markets(w3):
    if w3.eth.chain_id != 11155111:
        raise ValueError("event service requires Sepolia")
    result = {}
    block = w3.eth.get_block("latest")["number"]
    router, quoter = (
        w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
        for address, abi in (
            (market.ROUTER, config.router_abi),
            (QUOTER, config.quoter_abi),
        )
    )
    for contract in (router, quoter):
        if (
            contract.functions.factory().call(block_identifier=block).lower()
            != FACTORY.lower()
        ):
            raise ValueError("Phase 2 router/quoter factory mismatch")
    tokens = {}
    for name, token in (("f564", market.QUOTE), ("feth", market.BASE)):
        address = Web3.to_checksum_address(token)
        erc = w3.eth.contract(address=address, abi=config.erc20_abi)
        if erc.functions.decimals().call(block_identifier=block) != 18:
            raise ValueError("Phase 2 decimals mismatch")
        tokens[name] = (address, 18, name)
    for fee, address in market.POOLS.items():
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=config.pool_abi
        )
        if (
            pool.functions.token0().call(block_identifier=block).lower() != market.QUOTE
            or pool.functions.token1().call(block_identifier=block).lower()
            != market.BASE
            or pool.functions.fee().call(block_identifier=block) != fee
            or pool.functions.factory().call(block_identifier=block).lower()
            != FACTORY.lower()
        ):
            raise ValueError("Phase 2 pool identity mismatch")
        result[fee] = {
            "w3": w3,
            "pool": pool,
            "fee": fee,
            "tokens": tokens,
            "quoter": quoter,
        }
    return result


def pool_logs(pools, start, end):
    if set(pools) != set(market.POOLS):
        raise ValueError("snapshot needs both official pools")
    decoders, addresses = {}, []
    for fee, item in pools.items():
        pool = item["pool"]
        address = market.hex_value(pool.address, 20)
        if address != market.POOLS[fee]:
            raise ValueError("snapshot pool identity mismatch")
        addresses.append(pool.address)
        decoders[address] = {
            event.topic: event
            for event in (pool.events.Mint(), pool.events.Burn(), pool.events.Swap())
        }
    logs = pool.w3.eth.get_logs(
        {
            "address": addresses,
            "fromBlock": start,
            "toBlock": end,
            "topics": [list(decoders[address])],
        }
    )
    events = {fee: [] for fee in pools}
    fees = {address: fee for fee, address in market.POOLS.items()}
    for row in logs:
        if row.get("removed"):
            raise ValueError("removed pool log")
        address = market.hex_value(row.get("address"), 20)
        if address not in decoders:
            raise ValueError("unexpected pool log address")
        if not start <= row["blockNumber"] <= end:
            raise ValueError("pool log outside requested range")
        event = decoders[address][hexstr(row["topics"][0])].process_log(row)
        events[fees[address]].append(event)
    return events


def apply_logs(profile, events):
    ticks = {t["tick"]: dict(t) for t in profile["ticks"]}
    for event in sorted(events, key=lambda e: (e["blockNumber"], e["logIndex"])):
        if event.get("removed"):
            raise ValueError("removed pool log")
        args, kind = event["args"], event["event"]
        if kind in ("Mint", "Burn"):
            delta = args["amount"] * (1 if kind == "Mint" else -1)
            for tick, sign in ((args["tickLower"], 1), (args["tickUpper"], -1)):
                row = ticks.setdefault(tick, {"tick": tick, "gross": 0, "net": 0})
                row["gross"] += delta
                row["net"] += sign * delta
                if row["gross"] < abs(row["net"]):
                    raise ValueError("invalid incremental liquidity")
                if row["gross"] == 0:
                    del ticks[tick]
        elif kind == "Swap":
            profile.update(
                sqrtPriceX96=args["sqrtPriceX96"],
                tick=args["tick"],
                liquidity=args["liquidity"],
            )
    profile["ticks"] = sorted(ticks.values(), key=lambda t: t["tick"])


class Snapshots:
    def __init__(
        self, w3, pools, path, stop=lambda: False, seed_path=RESEARCH_SNAPSHOT
    ):
        self.w3, self.pools, self.path, self.stop = w3, pools, path, stop
        self.seed_path, self.loaded = seed_path, False
        self.log_chunk = 100
        self.snapshot = None
        self.reconciled_hash = None
        self.swaps = []
        self.swaps_dropped = 0

    def load_saved(self):
        # Prefer the runtime cache; the research layout is a bootstrap, never a quote.
        for path in (self.path, self.seed_path):
            try:
                saved = json.loads(path.read_text())
                market.models(saved, saved["timestamp"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            try:
                header = self.w3.eth.get_block(saved["block"])
            except BlockNotFound:
                continue
            if (
                hexstr(header["hash"]) == saved["block_hash"]
                and header["timestamp"] == saved["timestamp"]
            ):
                self.snapshot = saved
                break
        self.loaded = True

    def refresh(self, number="latest", scan=False):
        self.swaps = []
        self.swaps_dropped = 0
        if scan:
            self.loaded = True
        elif not self.loaded:
            self.load_saved()
        if not scan and self.snapshot is None:
            raise SnapshotUnavailable("no validated snapshot; dashboard scan required")
        try:
            return self.update(number, scan)
        except (ValueError, KeyError, TypeError) as exc:
            self.snapshot = None
            raise SnapshotUnavailable(
                "snapshot validation failed; dashboard scan required"
            ) from exc

    def update(self, number, scan):
        header = self.w3.eth.get_block(number)
        number = header["number"]
        previous = None if scan else self.snapshot
        same_block = previous and number == previous["block"]
        if previous and (
            number < previous["block"]
            or hexstr(
                (header if same_block else self.w3.eth.get_block(previous["block"]))[
                    "hash"
                ]
            )
            != previous["block_hash"]
        ):
            self.snapshot = None
            raise ValueError("snapshot ancestry changed; rescan required")
        if same_block and previous["block_hash"] == self.reconciled_hash:
            return copy.deepcopy(previous)
        profiles = []
        swaps, swaps_dropped = [], 0
        if previous:
            replayed = {p["fee"]: copy.deepcopy(p) for p in previous["profiles"]}
            # Both pools share bounded ranges and advance only after the whole read.
            start = previous["block"] + 1
            while start <= number:
                if self.stop():
                    raise InterruptedError("snapshot stopped")
                end = min(number, start + self.log_chunk - 1)
                try:
                    events = pool_logs(self.pools, start, end)
                except (Web3RPCError, OSError) as exc:
                    narrower = rejected_log_range(exc) or rejected_log_range(
                        exc.__cause__
                    )
                    if end == start or not narrower:
                        raise
                    # Retry reads only; a rejection narrows the range for both pools.
                    self.log_chunk = max(1, (end - start + 1) // 2)
                    continue
                chunk_swaps = []
                for fee, rows in events.items():
                    apply_logs(replayed[fee], rows)
                    chunk_swaps.extend((fee, e) for e in rows if e["event"] == "Swap")
                swaps.extend(
                    sorted(
                        chunk_swaps,
                        key=lambda row: (row[1]["blockNumber"], row[1]["logIndex"]),
                    )
                )
                swaps_dropped += max(0, len(swaps) - 2000)
                swaps = swaps[-2000:]
                if end - start + 1 >= self.log_chunk:
                    self.log_chunk = min(800, self.log_chunk * 2)
                start = end + 1
        for fee, item in self.pools.items():
            pool = item["pool"]
            if scan:
                p = config.read_depth(
                    pool,
                    18,
                    18,
                    number,
                    stop=self.stop,
                    batch_provider=getattr(self.w3, "provider", None),
                )
                p.update(
                    pool_address=market.POOLS[fee],
                    base_address=market.BASE,
                    quote_address=market.QUOTE,
                    base_is_token0=False,
                )
            else:
                p = replayed[fee]
            if scan:
                sqrt, tick, liquidity = (
                    p["sqrt" + "PriceX96"],
                    p["tick"],
                    p["liquidity"],
                )
            else:
                sqrt, tick, *_ = pool.functions.slot0().call(block_identifier=number)
                liquidity = pool.functions.liquidity().call(block_identifier=number)
                if p["sqrt" + "PriceX96"] != sqrt or p["tick"] != tick:
                    raise ValueError("snapshot price reconciliation failed")
            if sum(t["net"] for t in p["ticks"] if t["tick"] <= tick) != liquidity:
                self.snapshot = None
                raise ValueError("snapshot liquidity reconciliation failed")
            p.update(block=number, sqrtPriceX96=sqrt, tick=tick, liquidity=liquidity)
            profiles.append(p)
        value = {
            "chain_id": 11155111,
            "block": number,
            "block_hash": hexstr(header["hash"]),
            "timestamp": header["timestamp"],
            "profiles": profiles,
        }
        market.models(value, value["timestamp"])
        if self.w3.eth.get_block(number)["hash"] != header["hash"]:
            self.snapshot = None
            raise ValueError("snapshot block changed")
        ledger.atomic_write(self.path, json.dumps(value))
        self.snapshot = value
        self.reconciled_hash = value["block_hash"]
        self.swaps = swaps
        self.swaps_dropped = swaps_dropped
        return copy.deepcopy(value)
