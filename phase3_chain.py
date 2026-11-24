"""Pinned public Phase 3 reads and event decoding. Amounts in records are raw integers."""

import json
from itertools import batched

from eth_abi import decode
from eth_utils import event_abi_to_log_topic
from web3 import Web3
from web3._utils.events import get_event_data

import config
from phase_markets import PHASE3

PUBLIC_RPC = "https://ethereum-sepolia-rpc.publicnode.com"
FEE, POOL = PHASE3.pools[0]
ADDRESSES = {
    "pool": POOL,
    "credit": PHASE3.credit,
    "f564": PHASE3.quote,
    "fETH": PHASE3.base,
}
VIEWS = json.loads((config.ROOT / "abis/phase3_views.json").read_text())
WAD = 10**18


class Chain:
    def __init__(self, url=PUBLIC_RPC):
        self.provider = config.ThrottledProvider(
            url,
            rate=5,
            request_kwargs={
                "timeout": 20,
                "headers": {
                    "Content-Type": "application/json",
                    "User-Agent": "fin564-phase3-monitor/1.0",
                },
            },
            exception_retry_configuration=None,
        )
        self.w3 = Web3(self.provider)
        self.contracts = {}
        self.events = {}
        for label, address in ADDRESSES.items():
            abi = (
                config.pool_abi
                if label == "pool"
                else VIEWS["credit" if label == "credit" else "token"]
            )
            self.contracts[label] = self.w3.eth.contract(
                address=Web3.to_checksum_address(address), abi=abi
            )
            self.events[address.lower()] = (
                label,
                {
                    event_abi_to_log_topic(item): item
                    for item in abi
                    if item["type"] == "event"
                },
            )

    def header(self, block):
        h = self.w3.eth.get_block(block)
        return h.number, Web3.to_hex(h.hash), h.timestamp

    def views(self, block, specs):
        # Numeric block on every eth_call. Only view/pure functions may enter a batch.
        results = []
        for chunk in batched(specs, self.provider.batch_size):
            params, types = [], []
            for label, name, *args in chunk:
                contract = self.contracts[label]
                fn = getattr(contract.functions, name)(*args)
                if fn.abi["stateMutability"] not in ("view", "pure"):
                    raise ValueError("expected a view")
                params.append(
                    [
                        {
                            "to": contract.address,
                            "data": contract.encode_abi(name, args=args),
                        },
                        hex(block),
                    ]
                )
                types.append([o["type"] for o in fn.abi["outputs"]])
            for schema, raw in zip(
                types, self.provider.depth_calls(params), strict=True
            ):
                vals = decode(schema, raw)
                results.append(vals[0] if len(vals) == 1 else vals)
        return results

    def validate(self, block):
        if self.w3.eth.chain_id != 11155111:
            raise ValueError("expected Sepolia")
        actual = self.views(
            block,
            [
                ("pool", "token0"),
                ("pool", "token1"),
                ("pool", "fee"),
                ("pool", "tickSpacing"),
                ("credit", "f564"),
                ("credit", "fETH"),
                ("credit", "pricingPool"),
                ("f564", "decimals"),
                ("fETH", "decimals"),
            ],
        )
        q, b = PHASE3.quote.lower(), PHASE3.base.lower()
        if actual != [q, b, FEE, 60, q, b, POOL.lower(), 18, 18]:
            raise ValueError("unexpected Phase 3 market identity")

    def events_in(self, start, end):
        rows, senders, hashes = [], {}, {}
        for log in self.w3.eth.get_logs(
            {"fromBlock": start, "toBlock": end, "address": list(ADDRESSES.values())}
        ):
            block = log.blockNumber
            if not start <= block <= end or log.get("removed"):
                raise ValueError("invalid log range or removed log")
            if block not in hashes:
                hashes[block] = self.header(block)[1]
            if hashes[block] != Web3.to_hex(log.blockHash):
                raise ValueError("orphaned event")
            label, events = self.events[log.address.lower()]
            abi = events.get(bytes(log.topics[0]))
            args = dict(get_event_data(self.w3.codec, abi, log)["args"]) if abi else {}
            tx = Web3.to_hex(log.transactionHash)
            if label in ("pool", "credit") and tx not in senders:
                transaction = self.w3.eth.get_transaction(tx)
                if transaction.blockHash != log.blockHash:
                    raise ValueError("transaction moved")
                senders[tx] = transaction["from"].lower()
            rows.append(
                {
                    "block": block,
                    "index": log.logIndex,
                    "tx": tx,
                    "contract": label,
                    "event": abi["name"] if abi else Web3.to_hex(log.topics[0]),
                    "args": args,
                    "signer": senders.get(tx),
                }
            )
        return sorted(rows, key=lambda row: (row["block"], row["index"]))

    def snapshot(self, block):
        slot, liquidity, oracle, paused, bonus, quote, base, count = self.views(
            block,
            [
                ("pool", "slot0"),
                ("pool", "liquidity"),
                ("credit", "oraclePrice"),
                ("credit", "paused"),
                ("credit", "liquidationBonusBps"),
                ("credit", "market", PHASE3.quote),
                ("credit", "market", PHASE3.base),
                ("credit", "borrowerCount"),
            ],
        )
        accounts = self.views(
            block, (("credit", "borrowerAt", i) for i in range(count))
        )
        health = self.views(
            block,
            (("credit", "healthOf", Web3.to_checksum_address(a)) for a in accounts),
        )
        return {
            "block": block,
            "spot": config.quote_per_base(slot[0], 18, 18, False),
            "tick": slot[1],
            "liquidity": liquidity,
            "twap": oracle[0] / WAD,
            "twap_seconds": oracle[1],
            "paused": paused,
            "bonus_bps": bonus,
            "markets": {
                name: dict(
                    zip(
                        ("debt", "ceiling", "utilization_wad", "apr_wad"),
                        vals,
                        strict=True,
                    )
                )
                for name, vals in (("f564", quote), ("fETH", base))
            },
            "borrower_health_wad": dict(zip(accounts, health, strict=True)),
        }
