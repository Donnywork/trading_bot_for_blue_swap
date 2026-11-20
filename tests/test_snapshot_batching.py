"""Shared two-pool catch-up with real event ABI decoding, entirely offline."""

import copy
import json
from types import SimpleNamespace

import pytest
from eth_abi import encode
from hexbytes import HexBytes
from requests import HTTPError, Response
from web3 import Web3
from web3.exceptions import Web3RPCError

import config
import event_market as market
import event_snapshot
from event_snapshot import Snapshots, SnapshotUnavailable, apply_logs

SQRT = "sqrt" + "PriceX96"
NEW_HASH = HexBytes("0x" + "ab" * 32)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    saved = json.loads(event_snapshot.RESEARCH_SNAPSHOT.read_text())
    target = copy.deepcopy(saved)
    calls, rows = [], []
    state = SimpleNamespace(gap=1, reject=None, final_reorg=False)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(saved))

    def get_block(number):
        calls.append(("block", number))
        old = number == saved["block"]
        block_hash = HexBytes(saved["block_hash"]) if old else NEW_HASH
        if state.final_reorg and number == saved["block"] + state.gap:
            block_hash = HexBytes("0x" + "cd" * 32)
        return {
            "number": saved["block"] + (0 if old else state.gap),
            "timestamp": saved["timestamp"] + (0 if old else 12 * state.gap),
            "hash": block_hash,
        }

    def get_logs(params):
        calls.append(("logs", params))
        assert {a.lower() for a in params["address"]} == set(market.POOLS.values())
        assert len(params["topics"]) == 1 and len(params["topics"][0]) == 3
        if state.reject:
            state.reject(params)
        return [
            row
            for row in rows
            if params["fromBlock"] <= row["blockNumber"] <= params["toBlock"]
        ]

    w3 = SimpleNamespace(eth=SimpleNamespace(get_block=get_block, get_logs=get_logs))
    pools = {}
    for profile in target["profiles"]:
        fee = profile["fee"]
        contract = Web3().eth.contract(
            address=Web3.to_checksum_address(market.POOLS[fee]), abi=config.pool_abi
        )

        def call(kind, profile=profile, fee=fee):
            def pinned(*, block_identifier):
                assert block_identifier == saved["block"] + state.gap
                calls.append((kind, fee))
                return (
                    (profile[SQRT], profile["tick"])
                    if kind == "slot0"
                    else profile["liquidity"]
                )

            return SimpleNamespace(call=pinned)

        pools[fee] = {
            "pool": SimpleNamespace(
                w3=w3,
                address=contract.address,
                events=contract.events,
                functions=SimpleNamespace(
                    slot0=lambda call=call: call("slot0"),
                    liquidity=lambda call=call: call("liquidity"),
                ),
            )
        }
    adapter = Snapshots(w3, pools, path, seed_path=tmp_path / "absent")
    adapter.snapshot, adapter.loaded = copy.deepcopy(saved), True
    monkeypatch.setattr(
        config,
        "read_depth",
        lambda *a, **kw: pytest.fail("catch-up must never scan"),
    )
    return SimpleNamespace(
        saved=saved,
        target=target,
        calls=calls,
        rows=rows,
        state=state,
        adapter=adapter,
        path=path,
        pools=pools,
    )


def raw_log(chain, fee, kind, index, offset=1, amount=100):
    profile = next(p for p in chain.target["profiles"] if p["fee"] == fee)
    decoder = getattr(chain.pools[fee]["pool"].events, kind)()
    args = {
        "sender": market.ROUTER,
        "owner": market.ROUTER,
        "recipient": market.ROUTER,
        "tickLower": 80000,
        "tickUpper": 80200,
        "amount": amount,
        "amount0": 1,
        "amount1": -1 if kind == "Swap" else 1,
        SQRT: profile[SQRT],
        "tick": profile["tick"],
        "liquidity": profile["liquidity"],
    }
    inputs = decoder.abi["inputs"]
    plain = [i for i in inputs if not i["indexed"]]
    return {
        "address": chain.pools[fee]["pool"].address,
        "topics": [HexBytes(decoder.topic)]
        + [
            HexBytes(encode([i["type"]], [args[i["name"]]]))
            for i in inputs
            if i["indexed"]
        ],
        "data": HexBytes(
            encode([i["type"] for i in plain], [args[i["name"]] for i in plain])
        ),
        "blockNumber": chain.saved["block"] + offset,
        "blockHash": NEW_HASH,
        "transactionHash": HexBytes("0x" + f"{index + 1:064x}"),
        "transactionIndex": 0,
        "logIndex": index,
        "removed": False,
    }


def test_shared_mixed_logs_match_individual_ordered_replay(chain):
    expected = copy.deepcopy(chain.saved["profiles"])
    for n, p in enumerate(chain.target["profiles"]):
        p[SQRT] += n + 1
        fee = p["fee"]
        chain.rows.extend(
            [
                raw_log(chain, fee, "Burn", 4 + n, amount=30 + n),
                raw_log(chain, fee, "Swap", 2 + n),
                raw_log(chain, fee, "Mint", n, amount=100 + n * 10),
            ]
        )
        own_rows = [
            row for row in chain.rows if row["address"].lower() == market.POOLS[fee]
        ]
        decoders = {
            event.topic: event
            for event in (
                chain.pools[fee]["pool"].events.Mint(),
                chain.pools[fee]["pool"].events.Burn(),
                chain.pools[fee]["pool"].events.Swap(),
            )
        }
        apply_logs(
            expected[n],
            [
                decoders[Web3.to_hex(row["topics"][0])].process_log(row)
                for row in own_rows
            ],
        )
        expected[n]["block"] += 1
    # Deliberately interleave pools and reverse event order within each pool.
    chain.rows.sort(key=lambda row: -row["logIndex"])
    result = chain.adapter.refresh()
    assert result["profiles"] == expected
    assert [e["logIndex"] for _, e in chain.adapter.swaps] == [2, 3]
    assert (
        len(chain.calls) == 8
    )  # Three canonical headers, four state reads, one logs read.
    assert sum(kind == "logs" for kind, _ in chain.calls) == 1
    assert json.loads(chain.path.read_text()) == result
    chain.calls.clear()
    assert chain.adapter.refresh() == result
    assert chain.calls == [("block", "latest")]
    assert chain.adapter.swaps == []


@pytest.mark.parametrize("bad", ["foreign", "missing", "malformed", "removed"])
def test_invalid_log_identity_or_removal_fails_closed(chain, bad):
    chain.rows.append(raw_log(chain, 500, "Mint", 0))
    row = raw_log(chain, 10000, "Mint", 1)
    if bad == "foreign":
        row["address"] = "0x" + "11" * 20
    elif bad == "missing":
        del row["address"]
    elif bad == "malformed":
        row["address"] = 123
    else:
        row["removed"] = True
    chain.rows.append(row)
    with pytest.raises(SnapshotUnavailable):
        chain.adapter.refresh()
    assert chain.adapter.snapshot is None
    assert chain.adapter.swaps == []
    assert json.loads(chain.path.read_text()) == chain.saved
    assert not any(kind == "slot0" for kind, _ in chain.calls)


def http_error(status):
    response = Response()
    response.status_code = status
    return HTTPError(response=response)


def chained_range_error():
    error = http_error(429)
    error.__cause__ = http_error(413)
    return error


@pytest.mark.parametrize(
    "error",
    [
        Web3RPCError("plan range exceeded"),
        http_error(400),
        http_error(413),
        http_error(422),
        chained_range_error(),
    ],
)
def test_range_rejection_shrinks_both_pools_without_duplicate_replay(chain, error):
    chain.state.gap = 4
    chain.adapter.log_chunk = 4
    for n, fee in enumerate(market.POOLS):
        chain.rows.extend(
            [
                raw_log(chain, fee, "Mint", n, offset=1, amount=100),
                raw_log(chain, fee, "Burn", n + 2, offset=4, amount=30),
            ]
        )

    def reject(params):
        if params["toBlock"] - params["fromBlock"] + 1 > 2:
            raise error

    chain.state.reject = reject
    result = chain.adapter.refresh()
    ranges = [
        (p["fromBlock"] - chain.saved["block"], p["toBlock"] - chain.saved["block"])
        for kind, p in chain.calls
        if kind == "logs"
    ]
    assert ranges == [(1, 4), (1, 2), (3, 4)]
    assert chain.adapter.log_chunk == 4
    for profile in result["profiles"]:
        assert [t for t in profile["ticks"] if t["tick"] == 80000] == [
            {"tick": 80000, "gross": 70, "net": 70}
        ]


@pytest.mark.parametrize("failure", ["later_read", "second_pool", "final_reorg"])
def test_partial_success_never_publishes(chain, failure):
    chain.state.gap = 2
    chain.adapter.log_chunk = 1
    chain.rows.append(raw_log(chain, 500, "Swap", 0))
    chain.rows.append(raw_log(chain, 10000, "Mint", 1))
    expected_error = SnapshotUnavailable
    if failure == "later_read":

        def reject(params):
            if params["fromBlock"] > chain.saved["block"] + 1:
                raise ConnectionError("offline read failure")

        chain.state.reject = reject
        expected_error = ConnectionError
    elif failure == "second_pool":
        chain.target["profiles"][1][SQRT] += 1
    else:
        chain.state.final_reorg = True
    with pytest.raises(expected_error):
        chain.adapter.refresh()
    assert json.loads(chain.path.read_text()) == chain.saved
    assert chain.adapter.swaps == [] and chain.adapter.swaps_dropped == 0
    assert chain.adapter.reconciled_hash is None
    assert chain.adapter.snapshot == (chain.saved if failure == "later_read" else None)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_http_auth_and_quota_do_not_shrink_shared_range(chain, status):
    chain.state.gap = 100
    error = http_error(status)

    def reject(params):
        raise error

    chain.state.reject = reject
    with pytest.raises(HTTPError) as caught:
        chain.adapter.refresh()
    assert caught.value is error
    assert chain.adapter.log_chunk == 100
    assert sum(kind == "logs" for kind, _ in chain.calls) == 1
    assert chain.adapter.snapshot == chain.saved
    assert json.loads(chain.path.read_text()) == chain.saved


def test_single_block_rejection_is_not_retried(chain):
    error = http_error(413)

    def reject(params):
        raise error

    chain.state.reject = reject
    with pytest.raises(HTTPError) as caught:
        chain.adapter.refresh()
    assert caught.value is error
    assert sum(kind == "logs" for kind, _ in chain.calls) == 1
    assert chain.adapter.snapshot == chain.saved


def test_shared_quiet_ranges_grow_only_to_bounded_ceiling(chain):
    chain.state.gap = 2000
    chain.adapter.refresh()
    sizes = [
        p["toBlock"] - p["fromBlock"] + 1 for kind, p in chain.calls if kind == "logs"
    ]
    assert sizes == [100, 200, 400, 800, 500]
    assert chain.adapter.log_chunk == 800


def test_swap_diagnostics_keep_latest_bounded_cross_pool_order(chain):
    templates = {fee: raw_log(chain, fee, "Swap", 0) for fee in market.POOLS}
    chain.rows.extend(
        templates[500 if index % 2 else 10000] | {"logIndex": index}
        for index in reversed(range(2003))
    )
    chain.adapter.refresh()
    assert [row["logIndex"] for _, row in chain.adapter.swaps] == list(range(3, 2003))
    assert chain.adapter.swaps_dropped == 3
