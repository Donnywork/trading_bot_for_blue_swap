import copy
import json
import threading
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest
from web3 import Web3
from web3.exceptions import BlockNotFound, TimeExhausted, Web3RPCError

import bot_contract
import control
import event_feed
import event_fees
import event_history
import event_market as market
import event_modes
import event_snapshot
import operator_lock
import rpc_metrics as rpc
import strategy
from event_execution import Executor, sizes, sizes_near
from event_feed import Feed, endpoint
from event_service import Service
from event_settings import EventSettings, token_amount
from event_snapshot import Snapshots, SnapshotUnavailable, apply_logs
from event_store import EventStore, dashboard_view
from legacy_main import Halt

A = 10**18
HASH = "0x" + "ab" * 32
ROOT = Path(__file__).parents[1]


def test_event_settings_default_and_live_interlocks():
    assert EventSettings.from_env({}).mode == "paper"
    assert not EventSettings.from_env({"EVENT_EXECUTION_MODE": "live"}).live_enabled
    assert EventSettings.from_env({"EVENT_EXECUTION_MODE": "simulate"}).mode == "paper"
    with pytest.raises(ValueError, match="OWNER"):
        EventSettings.from_env({"BOT_CONTRACT_ADDRESS": "0x" + "11" * 20})
    with pytest.raises(ValueError, match="priority"):
        EventSettings.from_env({"EVENT_MAX_FEE_WEI": "1"})
    assert token_amount("123456789.123456789123456789") == 123456789123456789123456789
    assert all(type(n) is int for n in sizes(123456789123456789123456789))


def test_observer_interval_is_configurable_and_bounded():
    # The observer is pure CU spend; a slower plan needs a slower cadence.
    assert EventSettings.from_env({}).observer_seconds == 15
    assert (
        EventSettings.from_env({"EVENT_OBSERVER_SECONDS": "60"}).observer_seconds == 60
    )
    with pytest.raises(ValueError):
        EventSettings.from_env({"EVENT_OBSERVER_SECONDS": "1"})


def test_execution_ladder_covers_small_and_narrow_optima():
    # Thin pools peak at small sizes (observed ~3% of cap); the ladder must reach
    # cap/64 and keep adjacent rungs close so narrow peaks stay bracketed.
    cap = 10000 * 10**18
    ladder = sizes(cap)
    assert all(type(n) is int for n in ladder)
    assert ladder[0] == cap // 64  # a ~3%-of-cap optimum has a rung beside it
    assert ladder[-1] == cap
    assert len(ladder) == 14
    for index, (low, high) in enumerate(pairwise(ladder)):
        # Integer rungs cannot split the first (cap/64 -> cap/32) step below 2x;
        # everywhere else adjacent rungs stay within 1.5x (high*2 <= low*3).
        assert high * 2 <= (low * 4 if index == 0 else low * 3) + 2
    # The Nov 20 recomputation: denser mid-range rungs bracket big-window
    # optima that previously fell between 3750 and 5000.
    assert 20 * cap // 64 in ladder  # 3125 on a 10k cap
    assert 36 * cap // 64 in ladder  # 5625 on a 10k cap
    assert sizes(1) == [1] and sizes(2) == [1, 2]


def test_priority_ladder_orders_rungs_by_the_paper_optimum_only():
    # The float hint decides which integer rungs are quoted first in a race;
    # every returned size is still a configuration-derived integer rung.
    cap = 10000 * 10**18
    ladder = sizes(cap)
    optimum = 4593 * 10**18  # the observed 04:50 paper optimum
    priority, rest = sizes_near(cap, optimum)
    assert all(type(n) is int for n in priority + rest)
    assert sorted(priority + rest) == ladder  # nothing lost, nothing invented
    # Distance ordering: every priority rung is nearer the optimum than any rest rung.
    assert max(abs(n - optimum) for n in priority) < min(abs(n - optimum) for n in rest)
    assert len(priority) == 3
    assert sizes_near(cap, 0) == (ladder, [])
    assert sizes_near(cap, None) == (ladder, [])
    assert sizes_near(cap, "bad") == (ladder, [])


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "0", "-1", "0.0000000000000000001", "bad", str(2**256)]
)
def test_bad_event_token_amount(value):
    with pytest.raises(ValueError):
        token_amount(value)


def test_endpoint_accepts_only_alchemy_sepolia_and_fallback():
    assert (
        endpoint(
            {
                "RPC_FALLBACK_URLS": "https://example.org,https://eth-sepolia.g.alchemy.com/v2/placeholder"
            }
        )
        == "wss://eth-sepolia.g.alchemy.com/v2/placeholder"
    )
    with pytest.raises(ValueError):
        endpoint({"ALCHEMY_WS_URL": "wss://eth-mainnet.g.alchemy.com/v2/placeholder"})


def test_feed_overflow_invalidates_connection():
    feed = Feed("unused", [], threading.Event(), capacity=1)
    feed.ready.set()
    feed.gap.clear()
    feed.put({"kind": "head"})
    with pytest.raises(RuntimeError, match="overflow"):
        feed.put({"kind": "head"})
    assert feed.gap.is_set() and not feed.ready.is_set()


def test_feed_subscribes_to_signers_and_heads_without_exposing_endpoint(monkeypatch):
    stop = threading.Event()
    sent = []
    replies = [
        {"id": 1, "result": "0xaa36a7"},
        {"id": 2, "result": "pending-id"},
        {"id": 3, "result": "mined-id"},
        {"id": 4, "result": "head-id"},
        {"params": {"subscription": "pending-id", "result": {"hash": HASH}}},
    ]

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, **kwargs):
            reply = replies.pop(0)
            if not replies:
                stop.set()
            return json.dumps(reply)

    monkeypatch.setattr(event_feed, "connect", lambda *a, **k: Socket())
    metrics = rpc.RpcMetrics()
    feed = Feed("wss://private-placeholder", [market.MARKET_BOT], stop, metrics=metrics)
    feed.run()
    result = feed.queue.get_nowait()
    assert result["kind"] == "pending" and result["transaction"]["hash"] == HASH
    assert [r["params"][0] for r in sent[1:]] == [
        "alchemy_pendingTransactions",
        "alchemy_minedTransactions",
        "newHeads",
    ]
    assert sent[1]["params"][1]["fromAddress"] == [market.MARKET_BOT]
    assert not feed.ready.is_set() and feed.gap.is_set()
    stop.clear()
    replies.extend(
        [
            {"id": 1, "result": "0xaa36a7"},
            {"id": 2, "result": "p"},
            {"id": 3, "result": "m"},
            {"id": 4, "result": "h"},
        ]
    )
    feed.run()
    counts = metrics.snapshot()
    assert feed.generation == 2
    assert counts["total"] == counts["by_purpose"][rpc.FEED] == 8
    assert counts["http_attempts"] == 0


def test_subscription_backlog_retains_notification_arrival_time(monkeypatch):
    stop = threading.Event()
    replies = [
        {"id": 1, "result": "0xaa36a7"},
        {"id": 2, "result": "pending-id"},
        {"params": {"subscription": "pending-id", "result": {"hash": HASH}}},
        {"id": 3, "result": "mined-id"},
        {"id": 4, "result": "head-id"},
        {"params": {"subscription": "head-id", "result": {"number": "0x1"}}},
    ]

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, value):
            pass

        def recv(self, **kwargs):
            reply = replies.pop(0)
            if not replies:
                stop.set()
            return json.dumps(reply)

    ticks = iter([100, 105])
    monkeypatch.setattr(event_feed.time, "time", lambda: next(ticks))
    monkeypatch.setattr(event_feed, "connect", lambda *a, **k: Socket())
    feed = Feed("unused", [], stop)
    feed.queue.put({"kind": "old-generation"})
    feed.run()
    assert feed.queue.get_nowait()["received"] == 100
    assert feed.queue.get_nowait()["received"] == 105
    assert feed.queue.empty()


def test_service_switch_refuses_other_inventory_and_corrupt_ledger(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"open": {"base": "1"}}))
    with pytest.raises(RuntimeError, match="inventory"):
        operator_lock.require_flat(path, "open")
    path.write_text("broken")
    with pytest.raises(RuntimeError, match="cannot verify"):
        operator_lock.require_flat(path, "open")


def test_execution_intent_restart_and_identity_fail_closed(tmp_path):
    store = EventStore(tmp_path, {"vault": "one"}, "live")
    store.begin("entry:" + HASH, {"amount_raw": A})
    with pytest.raises(Halt, match="unfinished"):
        EventStore(tmp_path, {"vault": "one"}, "live")
    assert (tmp_path / "HALTED").exists()
    other = tmp_path / "other"
    EventStore(other, {"vault": "one"}, "paper")
    with pytest.raises(Halt, match="identity"):
        EventStore(other, {"vault": "two"}, "paper")


@pytest.mark.parametrize(
    "blocker", ["position", "intent", "corrupt", "halt", "dry_run", "inflight"]
)
def test_refused_mode_change_stays_paused(tmp_path, snapshot, blocker):
    svc = service(tmp_path, snapshot)
    s = replace(
        svc.settings, mode="live", bot_address="0x" + "11" * 20, live_enabled=True
    )
    modes = event_modes.Modes(svc.executor.w3, {}, s, svc.store.directory, {})
    modes.executor = svc.executor
    svc.executor.store, svc.executor.settings = svc.store, s
    svc.modes, svc.settings = modes, s
    if blocker == "position":
        svc.store.data["position"] = {"base_raw": 1}
    elif blocker == "intent":
        svc.store.begin("outstanding", {})
    elif blocker == "corrupt":
        svc.store.path.write_text("broken")
    elif blocker == "halt":
        svc.store.halt_path.write_text("reconcile")
    elif blocker == "dry_run":
        modes.settings = replace(s, live_enabled=False)
    cfg = replace(
        strategy.DEFAULT,
        event_mode_request=1,
        event_mode="live" if blocker == "dry_run" else "simulate",
    )
    control.save(svc.control_file, cfg)
    svc.read_controls()
    svc.process_mode(busy=blocker == "inflight")
    assert svc.settings is s and svc.mode_block and svc.controls.event_paused
    control.save(
        svc.control_file, replace(cfg, event_paused=False, event_start_request=1)
    )
    svc.read_controls()
    assert svc.controls.event_paused
    svc.publish()
    assert dashboard_view(svc.store.directory / "state.json", svc.clock())["mode_block"]


def test_restart_with_live_position_recovers_only_exits(
    tmp_path, snapshot, monkeypatch
):
    svc = service(tmp_path, snapshot)
    cfg = replace(
        strategy.DEFAULT,
        event_mode="live",
        event_mode_request=3,
        event_start_request=4,
        event_paused=False,
    )
    control.save(svc.control_file, cfg)
    position = {
        "base_raw": 1,
        "quote_raw": 2,
        "target": HASH,
        "target_sender": market.MARKET_BOT,
        "target_nonce": 1,
        "opened": svc.clock() - 100,
        "entry_tx": HASH,
        "entry_block": 1,
        "entry_block_hash": HASH,
        "exit_fee": 500,
    }
    svc.store.data["position"] = position
    svc.store.save()
    settings = replace(EventSettings(), mode="simulate", bot_address="0x" + "11" * 20)
    modes = event_modes.Modes(svc.executor.w3, {}, settings, svc.store.directory, {})
    with pytest.raises(operator_lock.SendBlocked, match="DRY_RUN=false"):
        modes.startup()
    modes.settings = replace(settings, live_enabled=True)
    modes.w3.eth.account = SimpleNamespace(from_key=lambda key: object())
    monkeypatch.setattr(event_modes, "required", lambda name: "test-only")

    def adapter(w3, pools, s, store, account):
        assert s.mode == "live" and store.data["position"] == position
        svc.executor.settings, svc.executor.store = s, store
        return svc.executor

    monkeypatch.setattr(event_modes, "Executor", adapter)
    try:
        ex = modes.startup()
        recovering = Service(
            ex.settings,
            ex.store,
            svc.snapshots,
            ex,
            svc.feed,
            svc.control_file,
            svc.clock,
            modes,
        )
        control.save(svc.control_file, replace(cfg, event_start_request=5))
        recovering.read_controls()
        assert recovering.mode_block and recovering.controls.event_paused
        calls = []
        ex.exit = lambda reason: calls.append(reason) or {"status": "not_sent"}
        recovering.maintain()
        assert calls == ["time_limit"]
    finally:
        modes.close()


def test_mode_change_cannot_hide_live_inventory(tmp_path):
    store = EventStore(tmp_path, {}, "live")
    store.data["intent"] = {"key": "inflight"}
    store.save()
    with pytest.raises(Halt, match="live event"):
        EventStore(tmp_path, {}, "paper")


def test_shared_operator_lock(tmp_path):
    path = tmp_path / "operator.lock"
    with (
        operator_lock.acquire(path),
        pytest.raises(RuntimeError, match="another local"),
        operator_lock.acquire(path),
    ):
        pass
    with operator_lock.acquire(path):
        pass


def test_mint_burn_updates_both_boundaries_and_order():
    p = {"ticks": []}
    mint = {
        "event": "Mint",
        "blockNumber": 1,
        "logIndex": 1,
        "args": {"tickLower": -10, "tickUpper": 10, "amount": 100},
    }
    burn = {
        "event": "Burn",
        "blockNumber": 2,
        "logIndex": 0,
        "args": mint["args"] | {"amount": 40},
    }
    apply_logs(p, [burn, mint])
    assert p["ticks"] == [
        {"tick": -10, "net": 60, "gross": 60},
        {"tick": 10, "net": -60, "gross": 60},
    ]
    apply_logs(p, [burn | {"args": mint["args"] | {"amount": 60}}])
    assert p["ticks"] == []
    with pytest.raises(ValueError, match="liquidity"):
        apply_logs(p, [burn])


@pytest.fixture
def snapshot():
    return json.loads(
        (ROOT / "research/2026-11-17-phase2-arbitrage/snapshot.json").read_text()
    )


class Call:
    def __init__(self, value):
        self.value = value

    def call(self, *args, **kwargs):
        return self.value


def test_incremental_snapshot_pinned_and_reorg_rejected(
    snapshot, tmp_path, monkeypatch
):
    saved = tmp_path / "snapshot.json"
    saved.write_text(json.dumps(snapshot))
    old_hash, new_hash = (
        Web3.to_bytes(hexstr=snapshot["block_hash"]),
        bytes.fromhex("cd" * 32),
    )
    old_block = snapshot["block"]
    changed = False

    def header(number):
        old = number == old_block
        return {
            "number": old_block if old else old_block + 1,
            "timestamp": snapshot["timestamp"] + (0 if old else 12),
            "hash": old_hash if old and not changed else new_hash,
        }

    eth = SimpleNamespace(get_block=header)
    pools = {}
    ranges = []

    monkeypatch.setattr(
        event_snapshot,
        "pool_logs",
        lambda pools, start, end: (
            ranges.append((start, end)) or {fee: [] for fee in pools}
        ),
    )

    for p in snapshot["profiles"]:
        funcs = SimpleNamespace(
            slot0=lambda p=p: Call((p["sqrt" + "PriceX96"], p["tick"])),
            liquidity=lambda p=p: Call(p["liquidity"]),
        )
        pools[p["fee"]] = {
            "pool": SimpleNamespace(
                functions=funcs,
            )
        }
    adapter = Snapshots(SimpleNamespace(eth=eth), pools, saved)
    result = adapter.refresh()
    assert result["block"] == old_block + 1
    assert ranges == [(old_block + 1, old_block + 1)]
    # Change the canonical hash of the adapter's latest block.
    eth.get_block = lambda n: header(n) | {"hash": bytes.fromhex("ef" * 32)}
    with pytest.raises(SnapshotUnavailable, match="validation"):
        adapter.refresh()
    assert adapter.snapshot is None


def snapshot_chain(snapshot, events=(), target=None):
    target = target or snapshot
    calls = []

    def header(number):
        old = number == snapshot["block"]
        return {
            "number": snapshot["block"] + (0 if old else 1),
            "timestamp": snapshot["timestamp"] + (0 if old else 12),
            "hash": Web3.to_bytes(hexstr=snapshot["block_hash"] if old else HASH),
        }

    class PinnedCall(Call):
        def call(self, *, block_identifier):
            assert block_identifier == snapshot["block"] + 1
            return self.value

    topics = {k: Web3.to_hex(Web3.keccak(text=k)) for k in ("Mint", "Burn", "Swap")}
    by_address = {a: list(events) for a in market.POOLS.values()}

    def get_logs(params):
        assert params["topics"] == [list(topics.values())]
        calls.append(params)
        addresses = params["address"]
        if isinstance(addresses, str):
            addresses = [addresses]
        return [
            e
            | {"address": address, "topics": [Web3.to_bytes(hexstr=topics[e["event"]])]}
            for address in addresses
            for e in by_address[address.lower()]
        ]

    w3 = SimpleNamespace(eth=SimpleNamespace(get_block=header, get_logs=get_logs))

    pools = {}
    for p in target["profiles"]:
        pools[p["fee"]] = {
            "pool": SimpleNamespace(
                w3=w3,
                address=market.POOLS[p["fee"]],
                rows=by_address[market.POOLS[p["fee"]]],
                functions=SimpleNamespace(
                    slot0=lambda p=p: PinnedCall((p["sqrt" + "PriceX96"], p["tick"])),
                    liquidity=lambda p=p: PinnedCall(p["liquidity"]),
                ),
                events=SimpleNamespace(
                    **{
                        k: lambda k=k: SimpleNamespace(
                            topic=topics[k], process_log=lambda row: row
                        )
                        for k in topics
                    }
                ),
            )
        }
    return w3, pools, calls


@pytest.fixture
def forbid_depth(monkeypatch):
    monkeypatch.setattr(
        event_snapshot.config,
        "read_depth",
        lambda *a, **k: pytest.fail("unexpected full liquidity scan"),
    )


@pytest.mark.parametrize(
    "cache", ["missing", "broken", "wrong_pool", "wrong_hash", "wrong_time", "valid"]
)
def test_startup_uses_valid_cache_or_research(snapshot, tmp_path, forbid_depth, cache):
    saved, seed = tmp_path / "snapshot.json", tmp_path / "research.json"
    seed.write_text(json.dumps(snapshot))
    value = copy.deepcopy(snapshot)
    if cache == "wrong_pool":
        value["profiles"][0]["pool_address"] = "0x" + "11" * 20
    elif cache == "wrong_hash":
        value["block_hash"] = HASH
    elif cache == "wrong_time":
        value["timestamp"] += 1
    elif cache == "valid":
        seed.write_text("broken")  # A valid runtime cache takes precedence.
    if cache != "missing":
        saved.write_text("broken" if cache == "broken" else json.dumps(value))
    w3, pools, calls = snapshot_chain(snapshot)
    adapter = Snapshots(w3, pools, saved, seed_path=seed)
    result = adapter.refresh()
    assert result["block"] == snapshot["block"] + 1
    assert json.loads(saved.read_text()) == result
    assert len(calls) == 1
    assert all(r["fromBlock"] == r["toBlock"] == snapshot["block"] + 1 for r in calls)
    if cache != "valid":
        assert json.loads(seed.read_text()) == snapshot


@pytest.mark.parametrize("invalid", ["missing", "broken", "wrong_pool", "wrong_hash"])
def test_unusable_snapshots_never_scan(snapshot, tmp_path, forbid_depth, invalid):
    seed = tmp_path / "research.json"
    value = copy.deepcopy(snapshot)
    if invalid == "wrong_pool":
        value["profiles"][0]["pool_address"] = "0x" + "11" * 20
    elif invalid == "wrong_hash":
        value["block_hash"] = HASH
    if invalid != "missing":
        seed.write_text("broken" if invalid == "broken" else json.dumps(value))
    w3, pools, calls = snapshot_chain(snapshot)
    adapter = Snapshots(w3, pools, tmp_path / "snapshot.json", seed_path=seed)
    for _ in range(2):
        with pytest.raises(SnapshotUnavailable):
            adapter.refresh()
    assert not calls and adapter.snapshot is None


def test_research_catchup_replays_ordered_events(snapshot, tmp_path, forbid_depth):
    seed = tmp_path / "research.json"
    seed.write_text(json.dumps(snapshot))
    block = snapshot["block"] + 1
    # New out-of-range positions must survive catch-up, even though active liquidity is unchanged.
    events = [
        {
            "event": kind,
            "blockNumber": block,
            "logIndex": index,
            "args": {"tickLower": 80000, "tickUpper": 80200, "amount": amount},
        }
        for kind, index, amount in (("Burn", 2, 30), ("Mint", 0, 100))
    ]
    target = copy.deepcopy(snapshot)
    for p in target["profiles"]:
        p["sqrt" + "PriceX96"] += 1
    # Each pool has its own final Swap log.
    w3, pools, _ = snapshot_chain(snapshot, events, target)
    for p in target["profiles"]:
        swap = {
            "event": "Swap",
            "blockNumber": block,
            "logIndex": 1,
            "args": {k: p[k] for k in ("sqrt" + "PriceX96", "tick", "liquidity")},
        }
        pools[p["fee"]]["pool"].rows.append(swap)
    result = Snapshots(w3, pools, tmp_path / "snapshot.json", seed_path=seed).refresh()
    for p, expected in zip(result["profiles"], target["profiles"], strict=True):
        assert p["sqrt" + "PriceX96"] == expected["sqrt" + "PriceX96"]
        assert [t for t in p["ticks"] if t["tick"] in (80000, 80200)] == [
            {"tick": 80000, "gross": 70, "net": 70},
            {"tick": 80200, "gross": 70, "net": -70},
        ]


@pytest.mark.parametrize("failure", ["price", "liquidity", "reorg", "read"])
def test_failed_catchup_withholds_snapshot_without_scan(
    snapshot, tmp_path, forbid_depth, failure
):
    saved = tmp_path / "snapshot.json"
    saved.write_text(json.dumps(snapshot))
    target = copy.deepcopy(snapshot)
    if failure == "price":
        target["profiles"][0]["sqrt" + "PriceX96"] += 1
    elif failure == "liquidity":
        target["profiles"][0]["liquidity"] += 1
    w3, pools, _ = snapshot_chain(snapshot, target=target)
    adapter = Snapshots(w3, pools, saved)
    adapter.load_saved()
    if failure == "reorg":
        header = w3.eth.get_block
        w3.eth.get_block = lambda n: header(n) | {"hash": bytes.fromhex("ef" * 32)}
    elif failure == "read":

        def fail(params):
            raise ConnectionError("provider unavailable")

        w3.eth.get_logs = fail
    for _ in range(2):
        with pytest.raises(
            ConnectionError if failure == "read" else SnapshotUnavailable
        ):
            adapter.refresh()
    assert json.loads(saved.read_text()) == snapshot


def test_only_explicit_scan_rebuilds_invalidated_snapshot(
    snapshot, tmp_path, monkeypatch
):
    w3, pools, _ = snapshot_chain(snapshot)
    adapter = Snapshots(
        w3, pools, tmp_path / "snapshot.json", seed_path=tmp_path / "missing"
    )
    with pytest.raises(SnapshotUnavailable):
        adapter.refresh()
    scans = []

    def read_depth(pool, d0, d1, number, **kwargs):
        scans.append(number)
        fee = next(fee for fee, item in pools.items() if item["pool"] is pool)
        return copy.deepcopy(next(p for p in snapshot["profiles"] if p["fee"] == fee))

    monkeypatch.setattr(event_snapshot.config, "read_depth", read_depth)
    result = adapter.refresh(scan=True)
    assert scans == [snapshot["block"] + 1] * 2
    assert result["block"] == snapshot["block"] + 1


def fake_executor(tmp_path, mode="live"):
    store = EventStore(tmp_path, {}, mode, clock=lambda: 100)
    ex = Executor.__new__(Executor)
    ex.settings = replace(EventSettings(), mode=mode)
    ex.store, ex.clock = store, lambda: 100
    ex.live_history = store if mode == "live" else None
    ex.history_verified = False
    ex.operator = "0x" + "11" * 20
    ex.account = SimpleNamespace(address=ex.operator)
    ex.bot = SimpleNamespace(address="0x" + "33" * 20)
    ex.entry_guard = lambda action: None
    header = {
        "number": 7,
        "hash": bytes.fromhex("ab" * 32),
        "timestamp": 99,
        "baseFeePerGas": 1,
    }
    ex.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            chain_id=11155111,
            get_block=lambda _: header,
            get_balance=lambda _: 10**20,
            get_transaction_count=lambda *a: 1,
            get_transaction=lambda h: {"blockNumber": None},
        )
    )
    ex.fees = event_fees.FeeCache(ex.w3, ex.settings, ex.clock)
    fn = SimpleNamespace(
        arguments=((True, 500, A, A // 2, header["timestamp"] + 30, 2**96),),
        estimate_gas=lambda _: 100_000,
        build_transaction=lambda tx: tx,
        call=lambda *a, **k: A,
    )
    return ex, fn


def candidate():
    return {
        "hash": HASH,
        "snapshot_block": 7,
        "snapshot_hash": HASH,
        "swap": {"sender": market.MARKET_BOT, "nonce": 1, "deadline": 200},
        "frontrun": {"sell_fee": 10000},
    }


@pytest.mark.parametrize(
    "failure,status,halt",
    [
        (bot_contract.NotSent("private endpoint"), "not_sent", False),
        (RuntimeError("Transaction reverted: 0x00"), "reverted", False),
        (bot_contract.SentUnknown("private endpoint"), None, True),
        (TimeExhausted("private endpoint"), None, True),
        (ValueError("private endpoint"), None, True),
    ],
)
def test_send_classification_and_intent_before_send(
    tmp_path, monkeypatch, failure, status, halt
):
    ex, fn = fake_executor(tmp_path)
    calls = []

    def send(*args, **kwargs):
        assert json.loads(ex.store.path.read_text())["intent"]["nonce"] == 1
        assert kwargs["gas_buffer"] == 2.0
        calls.append(1)
        raise failure

    monkeypatch.setattr(bot_contract, "send_tx", send)
    if halt:
        with pytest.raises(Halt):
            ex.perform("entry:" + HASH, fn, "entry", A, A // 2, candidate())
        assert ex.store.data["intent"]
        assert "private endpoint" not in ex.store.halt_path.read_text()
    else:
        result = ex.perform("entry:" + HASH, fn, "entry", A, A // 2, candidate())
        assert result["status"] == status and ex.store.data["intent"] is None
        with pytest.raises(ValueError, match="already attempted"):
            ex.perform("entry:" + HASH, fn, "entry", A, A // 2, candidate())
    assert calls == [1]


def test_simulate_does_not_call_sender_or_invent_inventory(tmp_path, monkeypatch):
    ex, fn = fake_executor(tmp_path, "simulate")
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: pytest.fail("simulation sent")
    )
    result = ex.perform("entry:" + HASH, fn, "entry", A, A // 2, candidate())
    assert result["status"] == "simulated" and ex.store.data["position"] is None


def test_entry_receipt_is_persisted_and_exit_uses_receipt_profit(tmp_path, monkeypatch):
    ex, fn = fake_executor(tmp_path)

    def receipt(token, value):
        return {
            "blockNumber": 7,
            "blockHash": bytes.fromhex("ab" * 32),
            "transactionHash": bytes.fromhex("cd" * 32),
            "gasUsed": 50000,
            "effectiveGasPrice": 10,
            "logs": [
                {
                    "address": token,
                    "topics": [
                        Web3.keccak(text="Transfer(address,address,uint256)"),
                        bytes(32),
                        bytes.fromhex("00" * 12 + "33" * 20),
                    ],
                    "data": value.to_bytes(32, "big"),
                }
            ],
        }

    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: receipt(market.BASE, 2 * A)
    )
    ex.perform("entry:" + HASH, fn, "entry", 1000 * A, A, candidate())
    position = ex.store.data["position"]
    assert position["base_raw"] == 2 * A and position["quote_raw"] == 1000 * A
    assert EventStore(tmp_path, {}, "live").data["position"] == position
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: receipt(market.QUOTE, 1005 * A)
    )
    result = ex.perform("exit:" + HASH, fn, "exit", 2 * A, 1000 * A, candidate())
    assert result["profit_raw"] == 5 * A and result["gas_wei"] == 500000
    assert ex.store.data["position"] is None


@pytest.mark.parametrize("failures,degraded,reads", [(1, False, 2), (5, True, 3)])
def test_confirmed_fill_never_halts_on_header_read_failures(
    tmp_path, monkeypatch, failures, degraded, reads
):
    # Nov 19 15:41: a mined +22.98 f564 cycle halted because the single
    # get_block after the send hit a transient provider error inside the
    # accounting catch-all. The receipt alone proves the fill; the header is
    # metadata, so the read is retried and then degraded, never halted.
    ex, fn = fake_executor(tmp_path)
    header = {
        "number": 7,
        "hash": bytes.fromhex("ab" * 32),
        "timestamp": 99,
        "baseFeePerGas": 1,
    }
    counted = {"failed": 0, "total": 0}

    def get_block(selector):
        # Only the post-send accounting read uses an integer block number;
        # freshness checks keep reading "latest" and must not fail.
        if isinstance(selector, int):
            counted["total"] += 1
            if counted["failed"] < failures:
                counted["failed"] += 1
                raise OSError("provider unavailable")
        return header

    ex.w3.eth.get_block = get_block
    monkeypatch.setattr("event_execution.time.sleep", lambda seconds: None)

    def receipt(value):
        return {
            "blockNumber": 7,
            "blockHash": bytes.fromhex("ab" * 32),
            "transactionHash": bytes.fromhex("cd" * 32),
            "gasUsed": 50000,
            "effectiveGasPrice": 10,
            "logs": [
                {
                    "address": market.BASE,
                    "topics": [
                        Web3.keccak(text="Transfer(address,address,uint256)"),
                        bytes(32),
                        bytes.fromhex("00" * 12 + "33" * 20),
                    ],
                    "data": value.to_bytes(32, "big"),
                }
            ],
        }

    monkeypatch.setattr(bot_contract, "send_tx", lambda *a, **k: receipt(2 * A))
    result = ex.perform("entry:" + HASH, fn, "entry", 1000 * A, A, candidate())
    assert result["status"] == "confirmed" and counted["total"] == reads
    assert not ex.store.halt_path.exists() and ex.store.data["intent"] is None
    position = ex.store.data["position"]
    assert position["base_raw"] == 2 * A
    if degraded:
        assert result["block_time"] is None
        assert result["inclusion_error"] == "Confirmed block metadata unavailable."
        assert position["entry_time"] is None
    else:
        assert result["block_time"] == 99 and "inclusion_error" not in result
        assert position["entry_time"] == 99


def test_feed_change_during_preparation_prevents_send(tmp_path, monkeypatch):
    ex, fn = fake_executor(tmp_path)

    def guard(action):
        raise ValueError("feed changed")

    ex.entry_guard = guard
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: pytest.fail("stale send")
    )
    with pytest.raises(ValueError, match="feed changed"):
        ex.perform("entry:" + HASH, fn, "entry", A, A // 2, candidate())
    assert ex.store.data["intent"] is None


def test_front_entry_refuses_an_unpermitted_exit_route(tmp_path):
    ex, _ = fake_executor(tmp_path)
    ex.bot.functions = SimpleNamespace(feeAllowed=lambda fee: Call(False))
    with pytest.raises(ValueError, match="exit route"):
        ex.enter(candidate(), {})


def test_removed_receipt_or_missing_output_halts_without_inventing_position(
    tmp_path, monkeypatch
):
    ex, fn = fake_executor(tmp_path)
    monkeypatch.setattr(
        bot_contract,
        "send_tx",
        lambda *a, **k: {"blockNumber": 7, "blockHash": bytes.fromhex("cd" * 32)},
    )
    with pytest.raises(Halt):
        ex.perform("entry:" + HASH, fn, "entry", A, 1, candidate())
    assert ex.store.data["intent"] is not None and ex.store.data["position"] is None


def restored_position(now):
    return {
        "base_raw": A,
        "quote_raw": 400 * A,
        "opened": now - 100,
        "target": HASH,
        "target_sender": market.MARKET_BOT,
        "target_nonce": 1,
        "exit_fee": 10000,
        "entry_block": 7,
        "entry_block_hash": HASH,
        "entry_tx": "0x" + "cd" * 32,
    }


@pytest.mark.parametrize("failure", ["changed", "missing", "read"])
@pytest.mark.parametrize("trimmed", [False, True])
def test_old_restored_entry_is_validated_before_any_exit(
    tmp_path, snapshot, failure, trimmed
):
    svc = service(tmp_path, snapshot)
    ex, _ = fake_executor(svc.store.directory)
    position = restored_position(svc.clock())
    ex.store.data["position"] = position
    if not trimmed:
        ex.store.data["trades"] = [
            {
                "status": "confirmed",
                "block": 7,
                "block_hash": HASH,
                "tx_hash": position["entry_tx"],
            }
        ]
    ex.store.save()
    ex.store = ex.live_history = EventStore(svc.store.directory, {}, "live")
    svc.executor, svc.store = ex, ex.store
    ex.w3.eth.block_number = 1000
    reads = []

    def get_block(number):
        reads.append(number)
        if failure == "missing":
            raise BlockNotFound("no canonical entry block")
        if failure == "read":
            raise ConnectionError("offline read failure")
        return {"hash": bytes.fromhex("ef" * 32)}

    ex.w3.eth.get_block = get_block
    ex.exit = lambda reason: pytest.fail("exit attempted before entry validation")
    # A missing block is a provider gap, not reorg evidence: like a read
    # failure it must propagate and retry, never halt on verified inventory.
    expected = (
        (ConnectionError if failure == "read" else BlockNotFound)
        if failure != "changed"
        else Halt
    )
    with pytest.raises(expected):
        svc.maintain()
    assert reads == [7] and not ex.history_verified
    assert ex.store.data["position"] == position
    assert ex.store.halt_path.exists() == (failure == "changed")
    assert bool(ex.store.data.get("canonical_invalid")) == (failure == "changed")


def test_canonical_old_position_can_exit_after_restart(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    ex, _ = fake_executor(svc.store.directory)
    ex.store.data["position"] = restored_position(svc.clock())
    ex.store.save()
    ex.store = ex.live_history = EventStore(svc.store.directory, {}, "live")
    svc.executor, svc.store = ex, ex.store
    ex.w3.eth.block_number = 1000
    calls = []
    ex.w3.eth.get_block = lambda n: calls.append(n) or {"hash": bytes.fromhex(HASH[2:])}
    ex.exit = lambda reason: calls.append(reason) or {"status": "not_sent"}
    svc.maintain()
    assert calls == [7, "time_limit"] and ex.history_verified
    # Open inventory is checked every time, even after initial recovery succeeded.
    ex.verify_recent()
    assert calls[-1] == 7


def test_disconnected_inventory_recovery_does_not_repeat_old_closed_history(
    tmp_path, snapshot
):
    svc = service(tmp_path, snapshot)
    ex, _ = fake_executor(svc.store.directory)
    ex.store.data["position"] = restored_position(svc.clock())
    ex.store.data["trades"] = [{"status": "confirmed", "block": 6, "block_hash": HASH}]
    svc.executor, svc.store = ex, ex.store
    ex.w3.eth.block_number = 1000
    reads = []
    ex.w3.eth.get_block = lambda n: reads.append(n) or {"hash": bytes.fromhex(HASH[2:])}
    ex.exit = lambda reason: {"status": "not_sent"}
    svc.feed.gap.set()
    svc.feed.ready.clear()
    svc.maintain()
    svc.maintain()
    assert reads == [7, 6, 7]


@pytest.mark.parametrize("status", ["confirmed", "reverted"])
def test_old_closed_receipts_are_checked_once_per_recovery(tmp_path, status):
    ex, _ = fake_executor(tmp_path)
    ex.store.data["trades"] = [{"status": status, "block": 7, "block_hash": HASH}]
    ex.w3.eth.block_number = 1000
    reads = []
    ex.w3.eth.get_block = lambda n: reads.append(n) or {"hash": bytes.fromhex(HASH[2:])}
    ex.verify_recent()
    ex.verify_recent()
    assert reads == [7]
    ex.history_verified = (
        False  # Reconnect/read recovery must recheck retained history.
    )
    ex.w3.eth.get_block = lambda n: {"hash": bytes.fromhex("ef" * 32)}
    with pytest.raises(Halt, match="reorganized"):
        ex.verify_recent()


def service(tmp_path, snapshot):
    store = EventStore(
        tmp_path / "events", {}, "live", clock=lambda: snapshot["timestamp"] + 1
    )
    feed = Feed("unused", [], threading.Event())
    feed.ready.set()
    feed.gap.clear()
    ex = SimpleNamespace(
        verify_recent=lambda: None,
        w3=SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_count=lambda *a: 2,
                get_block=lambda n: {
                    "hash": Web3.to_bytes(hexstr=snapshot["block_hash"])
                },
            )
        ),
    )
    adapter = SimpleNamespace(
        refresh=lambda **k: copy.deepcopy(snapshot), swaps=[], swaps_dropped=0
    )
    svc = Service(
        EventSettings(),
        store,
        adapter,
        ex,
        feed,
        tmp_path / "control.json",
        clock=lambda: snapshot["timestamp"] + 1,
    )
    svc.generation = feed.generation
    return svc


def test_exit_runs_when_entries_paused_and_feed_disconnected(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.feed.ready.clear()
    svc.feed.gap.set()
    svc.store.data["position"] = {
        "opened": snapshot["timestamp"] - 100,
        "target_sender": market.MARKET_BOT,
        "target_nonce": 1,
    }
    calls = []

    def exit_position(reason):
        calls.append(reason)
        svc.store.data["position"] = None
        return {"status": "confirmed"}

    svc.executor.exit = exit_position
    svc.maintain()
    assert calls == ["time_limit"] and svc.controls.event_paused


def test_competitive_evidence_cannot_bypass_competitor_invalidation(tmp_path, snapshot):
    from test_event_fees import transaction

    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, fee_policy="competitive")
    svc.fees = event_fees.FeeCache(svc.executor.w3, svc.settings, svc.clock)
    svc.executor.fees = svc.fees
    svc.refresh()
    tx = transaction()
    # Seed a prepared engine candidate to verify the real competitor invalidation path.
    svc.engine.candidates[HASH] = {
        "hash": HASH,
        "status": "prepared",
        "expires": svc.clock() + 60,
        "backrun": {"buy_fee": 500},
        "frontrun": None,
    }
    svc.handle(
        {
            "kind": "pending",
            "generation": svc.generation,
            "transaction": tx,
            "received": svc.clock(),
        }
    )
    assert svc.engine.candidates[HASH]["status"] == "stale"
    assert svc.engine.candidates[HASH]["backrun"] is None
    h = {
        "number": snapshot["block"],
        "hash": Web3.to_bytes(hexstr=snapshot["block_hash"]),
        "timestamp": snapshot["timestamp"],
        "baseFeePerGas": 10**9,
    }
    assert svc.fees.decide(h, "entry")["competitor"] is not None
    # Removed notifications also invalidate fee evidence.
    svc.handle({"kind": "mined", "generation": svc.generation, "removed": True})
    assert svc.fees.decide(h, "exit")["competitor"] is None


def test_fee_refresh_failure_does_not_delay_disconnected_exit(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, fee_policy="competitive")
    svc.fees = event_fees.FeeCache(svc.executor.w3, svc.settings, svc.clock)
    svc.executor.fees = svc.fees
    svc.fees.refresh(
        svc.fees.epoch
    )  # Fake header lacks fee metadata; worker fails safely.
    assert svc.fees.error
    svc.feed.ready.clear()
    svc.feed.gap.set()
    svc.store.data["position"] = {"opened": svc.clock() - 100}
    calls = []
    svc.executor.exit = lambda reason: calls.append(reason) or {"status": "not_sent"}
    svc.maintain()
    assert calls == ["time_limit"]


def test_service_records_prices_and_publishes_applied_entry_state(tmp_path, snapshot):
    import event_history

    svc = service(tmp_path, snapshot)
    svc.refresh()
    svc.refresh()
    rows = event_history.read(svc.store.directory / "prices.jsonl")
    assert len(rows) == 1 and rows[0]["block"] == snapshot["block"]
    svc.feed.put({"kind": "pending"}, svc.clock())
    svc.publish()
    saved = dashboard_view(svc.store.directory / "state.json", svc.clock())
    assert saved["controls"]["event_paused"]
    assert saved["start_floor"] == 0 and not saved["intent_pending"]
    assert len(saved["pools"]) == 2
    assert saved["feed"]["activity"]["pending"]["count"] == 1
    assert saved["feed"]["queued"] == 1


def test_chart_storage_failure_does_not_invalidate_snapshot_or_block_exits(
    tmp_path, snapshot
):
    svc = service(tmp_path, snapshot)
    path = svc.store.directory / "prices.jsonl"
    path.write_text("damaged chart history")
    svc.refresh()
    assert svc.engine.snapshot and svc.history_error
    assert path.read_text() == "damaged chart history"
    svc.store.data["position"] = {"opened": svc.clock() - 100}
    calls = []
    svc.executor.exit = lambda reason: calls.append(reason) or {"status": "not_sent"}
    svc.maintain()
    assert calls == ["time_limit"]


@pytest.mark.parametrize("failure", ["snapshot", "removed"])
def test_recovery_scan_at_higher_block_withholds_orphaned_prices(
    tmp_path, snapshot, failure
):
    from dashboard import Dashboard

    svc = service(tmp_path, snapshot)
    svc.refresh()
    path = svc.store.directory / "prices.jsonl"
    old = event_history.read(path)[0]
    if failure == "snapshot":

        def fail(**kwargs):
            raise SnapshotUnavailable("snapshot ancestry changed")

        svc.snapshots.refresh = fail
        with pytest.raises(SnapshotUnavailable):
            svc.refresh()
    else:
        svc.handle({"kind": "mined", "removed": True, "generation": svc.generation})
    assert event_history.read(path) == [old | {"canonical": False}]
    recovered = copy.deepcopy(snapshot)
    recovered.update(block=snapshot["block"] + 2, block_hash="0x" + "ef" * 32)
    for p in recovered["profiles"]:
        p["block"] = recovered["block"]
    svc.snapshots.refresh = lambda **k: recovered
    control.save(svc.control_file, replace(strategy.DEFAULT, event_scan_request=1))
    svc.maintain()
    svc.publish()
    app = Dashboard(
        True,
        state_file=tmp_path / "state.json",
        control_file=svc.control_file,
        clock=svc.clock,
    )
    view = app.event_prices_view({"mode": ["all"]})
    assert view["samples"] == 1 and view["invalidated_samples"] == 1
    assert view["latest"]["block"] == recovered["block"]
    assert len(view["segments"]) == 1 and len(view["segments"][0]) == 1


def test_execution_failure_diagnostic_preserves_reason_and_cooldown(tmp_path, snapshot):
    from runtime import ConfigurationError

    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, mode="simulate")
    control.save(
        svc.control_file,
        replace(strategy.DEFAULT, event_paused=False, event_start_request=1),
    )
    svc.read_controls()

    def reject(*args, **kwargs):
        raise ConfigurationError("operator ETH below maximum transaction cost")

    svc.executor.backrun = reject
    # Expected per-candidate verdicts are recorded and swallowed: the engine,
    # sibling candidates and the loop must survive every model-vs-exact miss.
    svc.maybe_execute(
        {"status": "paper_revalidated", "backrun": {"buy_fee": 500}, "hash": HASH}
    )
    svc.publish()
    view = dashboard_view(svc.store.directory / "state.json", svc.clock())
    assert view["last_execution"]["status"] == "execution_rejected"
    assert (
        view["last_execution"]["reason"]
        == "operator ETH below maximum transaction cost"
    )
    assert view["cooldown_until"] > svc.clock()
    assert svc.store.data["intent"] is None and not svc.store.data["attempts"]
    # Transport failures still propagate for run()'s reset-and-backoff.
    svc.executor.backrun = lambda *a, **k: (_ for _ in ()).throw(
        ConnectionError("provider body")
    )
    svc.next_attempt = 0
    with pytest.raises(ConnectionError):
        svc.maybe_execute(
            {
                "status": "paper_revalidated",
                "backrun": {"buy_fee": 500},
                "hash": "0x" + "ee" * 32,
            }
        )
    assert "provider body" not in json.dumps(svc.store.data)


def test_queued_feed_work_no_longer_blocks_execution(tmp_path, snapshot):
    # Nov 19 15:11: sibling notifications (head/mined bursts, rivals'
    # pendings) arrive exactly when a window is contested. The old
    # queue-emptiness veto refused the send and reset the engine through
    # run()'s transport handler while competitors took the window.
    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, mode="simulate")
    control.save(
        svc.control_file,
        replace(strategy.DEFAULT, event_paused=False, event_start_request=1),
    )
    svc.read_controls()
    svc.feed.queue.put({"kind": "head", "generation": svc.generation})
    svc.feed.queue.put(
        {"kind": "pending", "generation": svc.generation, "received": svc.clock()}
    )
    calls = []

    def backrun(decision, action="backrun"):
        calls.append(action)
        return {"kind": "execution", "status": "simulated"}

    svc.executor.backrun = backrun
    svc.maybe_execute(
        {"status": "paper_revalidated", "backrun": {"buy_fee": 500}, "hash": HASH}
    )
    assert calls == ["backrun"] and svc.feed.queue.qsize() == 2
    assert svc.last_execution["status"] == "simulated"


def test_removed_signer_event_keeps_pinned_snapshot(tmp_path, snapshot):
    # The mined subscription watches signers, not pools: a reorged-out signer
    # transaction that never touched our pools must not park the service in
    # awaiting_scan until a manual ~700-read scan. The pinned block's own
    # ancestry check still parks on a genuine pinned-block reorganization.
    svc = service(tmp_path, snapshot)
    svc.refresh()
    pinned = {"block": snapshot["block"], "block_hash": snapshot["block_hash"]}
    svc.snapshots.snapshot = pinned
    svc.handle({"kind": "mined", "removed": True, "generation": svc.generation})
    assert svc.snapshots.snapshot is pinned
    assert svc.feed.gap.is_set() and svc.engine.snapshot is None


def test_entry_quotes_stop_at_first_passing_rung(tmp_path, monkeypatch):
    # Ranked descending by modeled profit, the first exactly-validated rung
    # is the selection; the remaining bracket quotes could never change the
    # outcome and burn ~6-7 reads each inside the pending-nonce race.
    ex, _ = fake_executor(tmp_path)
    ex.bot = SimpleNamespace(
        address=ex.bot.address,
        functions=SimpleNamespace(feeAllowed=lambda fee: Call(True)),
    )
    monkeypatch.setattr("event_execution.market.models", lambda *a: object())
    monkeypatch.setattr(
        "event_execution.frontrun.evaluate",
        lambda pools, swap, size, buy_fee, sell_fee, base_cap: {
            "profit_f564": 20.0,
            "target_contribution_f564": 1.0,
        },
    )
    quotes = []

    def single(fee, amount, buy, block):
        quotes.append(amount)
        return ("fn", amount, amount + A)

    ex.single = single
    sent = []
    ex.perform = lambda key, fn, action, amount, minimum, cand, **k: (
        sent.append(amount) or {"status": "simulated"}
    )
    plan = candidate() | {"frontrun": {"buy_fee": 500, "sell_fee": 500}}
    ex.enter(plan, {"block": 7, "block_hash": HASH})
    assert len(quotes) == 1 and sent == [quotes[0]]


def test_attempts_are_bounded_and_exit_retry_keys_survive_trim(tmp_path):
    ex, _ = fake_executor(tmp_path)
    for i in range(1200):
        ex.store.data["attempts"][f"cycle:{i}"] = "confirmed"
    ex.store.begin("cycle:new", {})
    assert len(ex.store.data["attempts"]) == 1000
    assert "cycle:new" in ex.store.data["attempts"]
    ex.store.finish("cycle:new", "not_sent", None, {"status": "not_sent"})
    # A retried exit must get a fresh key even after attempts trimming.
    ex.store.data["position"] = restored_position(ex.clock())
    ex.w3.eth.get_block = lambda n: {
        "number": 7,
        "hash": bytes.fromhex("ab" * 32),
        "timestamp": 99,
    }
    ex.single = lambda fee, amount, buy, block: ("fn", amount, amount // 2)
    keys = []
    ex.perform = lambda key, *a, **k: keys.append(key) or {"status": "not_sent"}
    ex.exit("time_limit")
    ex.exit("time_limit")
    assert len(keys) == 2 and keys[0] != keys[1]
    assert all(k.startswith("exit:" + HASH) for k in keys)


def test_pendings_dequeue_in_arrival_order_without_jumping_evidence(tmp_path, snapshot):
    # Pendings carry a 2 s freshness budget, but they must not jump ahead of
    # head/mined evidence: a newer head can invalidate the state a pending
    # plan was built on. Removals are selected first under the queue mutex.
    svc = service(tmp_path, snapshot)
    svc.feed.queue.put({"kind": "head", "generation": svc.generation})
    svc.feed.queue.put({"kind": "mined", "generation": svc.generation})
    svc.feed.queue.put({"kind": "pending", "generation": svc.generation, "received": 1})
    svc.feed.queue.put({"kind": "head", "generation": svc.generation})
    assert svc.next_event(0.1)["kind"] == "head"
    assert svc.next_event(0.1)["kind"] == "mined"
    assert svc.next_event(0.1)["kind"] == "pending"
    assert svc.next_event(0.1)["kind"] == "head"
    svc.feed.queue.put({"kind": "mined", "generation": svc.generation, "removed": True})
    svc.feed.queue.put({"kind": "head", "generation": svc.generation})
    assert svc.next_event(0.1).get("removed") is True
    assert svc.next_event(0.1)["kind"] == "head"
    assert svc.feed.queue.empty()


def test_single_deadline_uses_freshest_chain_time(tmp_path, monkeypatch):
    # current() admits snapshots up to 30 s stale; a deadline anchored to the
    # pinned quote block could already be expired when the transaction lands
    # (a guaranteed revert on an entry that passed every simulation).
    ex, _ = fake_executor(tmp_path)
    monkeypatch.setattr(market, "QUOTE", "0x" + "22" * 20)
    monkeypatch.setattr(market, "BASE", "0x" + "44" * 20)
    captured = {}

    def swap(args):
        captured.update(args=args)
        return SimpleNamespace(call=lambda *a, **k: A)

    ex.bot = SimpleNamespace(
        address=ex.bot.address,
        functions=SimpleNamespace(
            feeAllowed=lambda fee: Call(True),
            tokenCap=lambda token: Call(2**200),
            maxBaseIn=lambda: Call(2**200),
            swap=swap,
        ),
    )
    ex.pools = {
        500: {
            "pool": SimpleNamespace(
                functions=SimpleNamespace(slot0=lambda: Call((2**96, 0)))
            ),
            "quoter": SimpleNamespace(
                functions=SimpleNamespace(
                    quoteExactInputSingle=lambda params: Call((A, 2**96 + 1, 0, 100000))
                )
            ),
        }
    }

    def get_block(selector):
        if selector == "latest":
            return {
                "number": 8,
                "hash": bytes.fromhex("ef" * 32),
                "timestamp": 110,
            }
        return {"number": 7, "hash": bytes.fromhex("ab" * 32), "timestamp": 99}

    ex.w3.eth.get_block = get_block
    ex.single(500, A, True, 7)
    assert captured["args"][4] == 110 + 30  # max(pinned, latest) + 30


def test_verify_recent_missing_block_stays_pending_without_halt(tmp_path):
    # Absence of a block is not evidence of reorganization: a provider that
    # lags archive state must fail like a read (propagate, retry) instead of
    # accusing good inventory and forcing a manual reconciliation.
    ex, _ = fake_executor(tmp_path)
    ex.store.data["trades"] = [{"status": "confirmed", "block": 7, "block_hash": HASH}]
    ex.w3.eth.block_number = 1000
    ex.w3.eth.get_block = lambda n: (_ for _ in ()).throw(BlockNotFound("lag"))
    with pytest.raises(BlockNotFound):
        ex.verify_recent()
    assert not ex.history_verified and not ex.store.halt_path.exists()
    assert not ex.store.data.get("canonical_invalid")


def test_stale_or_old_generation_pending_never_plans(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.refresh()
    svc.handle({"kind": "pending", "generation": -1})
    svc.handle(
        {"kind": "pending", "generation": 0, "received": snapshot["timestamp"] - 10}
    )
    assert not svc.recent


def test_pending_decision_records_queue_wait_not_plumbing(tmp_path, snapshot):
    # The 17:56 Nov 23 window lost tail ordering to a 76 ms queue wait that
    # no record exposed. Decisions must carry how long the event waited
    # between the feed's arrival stamp and dispatch, while the monotonic
    # stamp that carries the measurement to the executor never persists.
    svc = service(tmp_path, snapshot)
    svc.refresh()
    tx = {
        "hash": HASH,
        "from": market.MARKET_BOT,
        "nonce": "0x1",
        "to": market.ROUTER,
        "value": "0x0",
        "chainId": hex(11155111),
        "input": market.router().encode_abi(
            "exactInputSingle",
            args=[
                (
                    Web3.to_checksum_address(market.QUOTE),
                    Web3.to_checksum_address(market.BASE),
                    10000,
                    Web3.to_checksum_address(market.MARKET_BOT),
                    5000 * A,
                    1,
                    0,
                )
            ],
        ),
    }
    svc.handle(
        {
            "kind": "pending",
            "transaction": tx,
            "generation": svc.generation,
            "received": svc.clock(),
            "received_mono": time.monotonic() - 0.075,
        }
    )
    row = svc.recent[-1]
    assert row["status"] == "prepared"
    assert 70 <= row["queue_wait_ms"] <= 90
    assert "_dequeue_mono" not in row
    stored = json.loads(
        (tmp_path / "events" / "decisions.jsonl").read_text().splitlines()[-1]
    )
    assert stored["queue_wait_ms"] == row["queue_wait_ms"]
    assert "_dequeue_mono" not in stored


def test_start_attempt_carries_queue_plumbing(tmp_path):
    ex, _ = fake_executor(tmp_path)
    timing = ex.start_attempt(
        {
            "hash": "0x" + "cd" * 32,
            "queue_wait_ms": 12.5,
            "_dequeue_mono": time.monotonic() - 0.03,
        },
        "backrun",
    )
    assert timing.data["queue_wait_ms"] == 12.5
    assert 25 <= timing.data["dispatch_ms"] <= 40
    assert ex.attempt["timing"]["queue_wait_ms"] == 12.5
    bare = ex.start_attempt({"hash": "0x" + "ce" * 32}, "backrun")
    assert bare.data["queue_wait_ms"] is None
    assert bare.data["dispatch_ms"] is None


@pytest.mark.parametrize("refresh", ["head", "timer", "reconnect"])
def test_service_recovers_receipts_from_engine_owned_targets(
    tmp_path, snapshot, refresh
):
    svc = service(tmp_path, snapshot)
    svc.refresh()
    tx = {
        "hash": HASH,
        "from": market.MARKET_BOT,
        "nonce": "0x1",
        "to": market.ROUTER,
        "value": "0x0",
        "chainId": hex(11155111),
        "input": market.router().encode_abi(
            "exactInputSingle",
            args=[
                (
                    Web3.to_checksum_address(market.QUOTE),
                    Web3.to_checksum_address(market.BASE),
                    10000,
                    Web3.to_checksum_address(market.MARKET_BOT),
                    5000 * A,
                    1,
                    0,
                )
            ],
        ),
    }
    svc.handle(
        {
            "kind": "pending",
            "transaction": tx,
            "generation": svc.generation,
            "received": svc.clock(),
        }
    )
    assert svc.recent[-1]["status"] == "prepared"
    reads = []

    def receipt(tx_hash):
        reads.append(tx_hash)
        return {
            "transactionHash": bytes.fromhex(HASH[2:]),
            "from": market.MARKET_BOT,
            "status": 1,
            "blockNumber": snapshot["block"],
            "blockHash": bytes.fromhex(snapshot["block_hash"][2:]),
        }

    svc.executor.w3.eth.get_transaction_receipt = receipt
    if refresh == "head":
        svc.handle({"kind": "head", "generation": svc.generation})
    else:
        svc.last_refresh = 0
        if refresh == "reconnect":
            svc.feed.gap.set()
        svc.maintain()
    assert not svc.engine.pending_transactions()
    if refresh == "reconnect":
        assert reads == []  # Recovery discarded the old generation's receipt work.
    else:
        assert reads == [HASH]
        assert svc.recent[-1]["status"] == "no_opportunity"
        svc.handle({"kind": "head", "generation": svc.generation})
        assert reads == [HASH]  # Completed candidates no longer cost receipt reads.


def test_snapshot_work_defers_until_inventory_is_closed(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.store.data["position"] = {"base_raw": A}
    svc.snapshots.refresh = lambda **k: pytest.fail("snapshot scan delayed an exit")
    svc.refresh(scan=True)
    assert svc.health == "managing_position"


@pytest.mark.parametrize("connected", [True, False])
def test_dashboard_scan_recovers_before_startup_or_feed_gap(
    tmp_path, snapshot, connected
):
    svc = service(tmp_path, snapshot)
    svc.generation = -1
    svc.feed.gap.set()
    if not connected:
        svc.feed.ready.clear()
    control.save(svc.control_file, replace(strategy.DEFAULT, event_scan_request=10))
    calls = []

    def refresh(*, scan=False):
        calls.append(scan)
        assert scan  # Ordinary recovery would fail without a seed.
        assert svc.store.data["scan_done"] == 10
        return copy.deepcopy(snapshot)

    svc.snapshots.refresh = refresh
    svc.maintain()
    assert calls == [True]
    assert svc.scan_done == 10
    assert svc.engine.snapshot is not None
    if connected:
        assert svc.generation == svc.feed.generation
        assert svc.health == "ready"


def test_failed_dashboard_scan_consumed_across_restart(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.generation = -1
    control.save(svc.control_file, replace(strategy.DEFAULT, event_scan_request=10))
    calls = []

    def refresh(*, scan=False):
        calls.append(scan)
        raise SnapshotUnavailable("invalid layout")

    svc.snapshots.refresh = refresh
    svc.engine.snapshot = copy.deepcopy(snapshot)
    with pytest.raises(SnapshotUnavailable):
        svc.maintain()
    assert svc.engine.snapshot is None
    restarted = service(tmp_path, snapshot)
    restarted.snapshots.refresh = refresh
    with pytest.raises(SnapshotUnavailable):
        restarted.maintain()
    assert calls == [True, False]
    control.save(svc.control_file, replace(strategy.DEFAULT, event_scan_request=11))
    with pytest.raises(SnapshotUnavailable):
        restarted.maintain()
    assert calls == [True, False, True]


def test_long_scan_catches_up_once_without_rescanning(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    calls = []

    def refresh(scan=False):
        calls.append(scan)
        return snapshot | {"timestamp": snapshot["timestamp"] - (120 if scan else 0)}

    svc.snapshots.refresh = refresh
    control.save(svc.control_file, replace(strategy.DEFAULT, event_scan_request=2))
    svc.maintain()
    assert calls == [True, False] and svc.scan_status == "Completed"
    assert svc.engine.snapshot["timestamp"] == snapshot["timestamp"]


def test_scan_request_waits_for_position_exit(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.store.data["position"] = {
        "opened": snapshot["timestamp"],
        "target_sender": market.MARKET_BOT,
        "target_nonce": 3,
    }
    control.save(svc.control_file, replace(strategy.DEFAULT, event_scan_request=10))
    svc.snapshots.refresh = lambda **k: pytest.fail("scan ran with inventory open")
    svc.maintain()
    assert svc.scan_done == 0
    svc.store.data["position"]["opened"] -= 100
    calls = []

    def exit_position(reason):
        calls.append("exit")
        svc.store.data["position"] = None
        return {"status": "confirmed"}

    def refresh(*, scan=False):
        assert scan
        calls.append("scan")
        return copy.deepcopy(snapshot)

    svc.executor.exit = exit_position
    svc.snapshots.refresh = refresh
    svc.maintain()
    assert calls == ["exit", "scan"]


def test_missing_snapshot_publishes_dashboard_instruction(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.feed.start = lambda: None
    stop = threading.Event()

    def unavailable(**kwargs):
        stop.set()
        raise SnapshotUnavailable("no layout")

    svc.snapshots.refresh = unavailable
    states = []
    svc.store.state = lambda value: states.append(value)
    svc.run(stop)
    waiting = next(s for s in states if s["health"] == "awaiting_scan")
    assert "Scan Pool Liquidity" in waiting["message"]
    assert waiting["snapshot_block"] is None


def test_missing_snapshot_still_schedules_bounded_holdings(tmp_path, snapshot):
    from event_observer import Observer

    svc = service(tmp_path, snapshot)
    now = [svc.clock()]
    svc.clock = lambda: now[0]
    svc.feed.start = lambda: None
    stop = threading.Event()
    passes, observed = [], []

    def unavailable(**kwargs):
        assert not kwargs.get("scan")
        passes.append(now[0])
        raise SnapshotUnavailable("no layout")

    svc.snapshots.refresh = unavailable
    svc.observer = Observer(
        svc.executor.w3, {}, svc.store.directory, svc.settings, svc.clock
    )
    svc.observer.update = lambda snap, *args: observed.append(snap)

    def wait(seconds):
        assert seconds == 1
        if svc.observer.thread:
            svc.observer.thread.join(2)
        now[0] += 1
        if len(passes) == 18:
            stop.set()

    stop.wait = wait
    svc.run(stop)
    if svc.observer.thread:
        svc.observer.thread.join(2)
    assert len(passes) == 18 and observed == [None, None]
    assert svc.scan_done == 0


@pytest.mark.parametrize("error", [Halt("reconcile"), TypeError("unexpected bug")])
def test_terminal_failures_do_not_schedule_diagnostics(tmp_path, snapshot, error):
    svc = service(tmp_path, snapshot)
    svc.feed.start = lambda: None
    svc.observer = SimpleNamespace(
        error="",
        schedule=lambda *a: pytest.fail("scheduled after terminal failure"),
    )

    def fail():
        raise error

    svc.maintain = fail
    with pytest.raises(type(error)):
        svc.run(threading.Event())


@pytest.mark.parametrize("mode", ["paper", "simulate"])
def test_scan_acknowledgement_survives_mode_switch(tmp_path, mode):
    live = EventStore(tmp_path, {}, "live")
    live.acknowledge_scan(10)
    other = EventStore(tmp_path, {}, mode)
    assert other.scan_request_done() == 10


def test_scan_acknowledgement_migrates_and_refuses_corruption(tmp_path):
    live = EventStore(tmp_path, {}, "live")
    live.data["scan_done"] = 10
    live.save()
    paper = EventStore(tmp_path, {}, "paper")
    assert paper.scan_request_done() == 10
    (tmp_path / "scan.json").write_text("broken")
    with pytest.raises(Halt, match="scan acknowledgement"):
        paper.scan_request_done()


@pytest.mark.parametrize("mode", ["simulate", "live"])
def test_final_execution_guards_apply_in_both_modes(tmp_path, monkeypatch, mode):
    ex, fn = fake_executor(tmp_path, mode)
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: pytest.fail("sent after pause")
    )

    def reject(action):
        raise ValueError("entries paused")

    ex.entry_guard = reject
    with pytest.raises(ValueError, match="paused"):
        ex.perform("entry:" + HASH, fn, "entry", A, 1, candidate())
    assert not ex.store.data["attempts"] and ex.store.data["intent"] is None


@pytest.mark.parametrize(
    "error", [ConnectionError("private-provider-body"), TypeError("programming bug")]
)
def test_service_read_failure_clears_state_but_bug_exits(tmp_path, snapshot, error):
    svc = service(tmp_path, snapshot)
    svc.feed.start = lambda: None
    stop = threading.Event()
    waits = []

    def wait(seconds):
        waits.append(seconds)
        stop.set()

    stop.wait = wait
    svc.engine.snapshot = copy.deepcopy(snapshot)

    def fail():
        raise error

    svc.maintain = fail
    states = []
    svc.store.state = lambda value: states.append(value)
    if isinstance(error, TypeError):
        with pytest.raises(TypeError):
            svc.run(stop)
        assert states[-1]["health"] == "failed"
    else:
        svc.run(stop)
        assert svc.engine.snapshot is None
        assert waits == [2]
        assert any(s["health"] == "degraded" for s in states)
    assert "private-provider-body" not in json.dumps(states)


def test_disconnected_feed_events_cannot_prepare_candidates(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.feed.ready.clear()
    svc.engine.handle = lambda event: pytest.fail("processed disconnected feed work")
    svc.handle({"kind": "pending", "generation": svc.generation})


@pytest.mark.parametrize(
    "inventory,expected",
    [(False, [2, 4, 8, 16, 32, 60]), (True, [2, 4, 8, 10, 10, 10])],
)
def test_read_backoff_is_bounded_and_keeps_exit_checks_frequent(
    tmp_path, snapshot, inventory, expected
):
    svc = service(tmp_path, snapshot)
    svc.feed.start = lambda: None
    if inventory:
        svc.store.data["position"] = {"base_raw": A}
    stop = threading.Event()
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == len(expected):
            stop.set()

    stop.wait = wait

    def failure():
        raise ConnectionError("provider response")

    svc.maintain = failure
    svc.run(stop)
    assert waits == expected


def test_disconnected_status_reports_safe_transport_diagnostic(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    svc.feed.ready.clear()
    svc.feed.gap.set()
    svc.feed.last_error = "ConnectionError at connection.py:10 (recv)"
    svc.maintain()
    svc.publish()
    value = dashboard_view(svc.store.directory / "state.json", svc.clock())
    assert value["health"] == "disconnected"
    assert value["feed_error"] in value["message"]


def test_missing_cache_block_falls_back_to_research(snapshot, tmp_path, forbid_depth):
    seed, saved = tmp_path / "research.json", tmp_path / "snapshot.json"
    seed.write_text(json.dumps(snapshot))
    bad = copy.deepcopy(snapshot)
    bad["block"] += 1000
    for p in bad["profiles"]:
        p["block"] = bad["block"]
    saved.write_text(json.dumps(bad))
    w3, pools, _ = snapshot_chain(snapshot)
    header = w3.eth.get_block

    def get_block(number):
        if number == bad["block"]:
            raise BlockNotFound("missing")
        return header(number)

    w3.eth.get_block = get_block
    assert (
        Snapshots(w3, pools, saved, seed_path=seed).refresh()["block"]
        == snapshot["block"] + 1
    )


def test_snapshot_catchup_adapts_provider_log_range(snapshot, tmp_path, forbid_depth):
    saved = tmp_path / "snapshot.json"
    saved.write_text(json.dumps(snapshot))
    w3, pools, _ = snapshot_chain(snapshot)
    header = w3.eth.get_block

    def get_block(number):
        value = header(number)
        if number == "latest" or number == snapshot["block"] + 25:
            value["number"] = snapshot["block"] + 25
        return value

    w3.eth.get_block = get_block
    accepted = []

    get_logs = w3.eth.get_logs

    def events(params):
        if params["toBlock"] - params["fromBlock"] + 1 > 10:
            raise Web3RPCError("plan range exceeded")
        accepted.append(params)
        return get_logs(params)

    w3.eth.get_logs = events

    for p in snapshot["profiles"]:
        pool = pools[p["fee"]]["pool"]
        pool.functions.slot0 = lambda p=p: Call((p["sqrt" + "PriceX96"], p["tick"]))
        pool.functions.liquidity = lambda p=p: Call(p["liquidity"])
    adapter = Snapshots(w3, pools, saved)
    assert adapter.refresh()["block"] == snapshot["block"] + 25
    for fee in pools:
        blocks = [
            b
            for r in accepted
            if market.POOLS[fee] in r["address"]
            for b in range(r["fromBlock"], r["toBlock"] + 1)
        ]
        assert blocks == list(range(snapshot["block"] + 1, snapshot["block"] + 26))


@pytest.mark.parametrize(
    "code,message",
    [
        (-32005, "Monthly quota exhausted"),
        (-32005, "Rate limit exceeded"),
        (-32600, "Invalid API key"),
        (-32000, "Unauthorized"),
        (-32602, "Invalid params"),
    ],
)
def test_rpc_quota_auth_errors_preserve_catchup_range(
    snapshot, tmp_path, forbid_depth, code, message
):
    saved = tmp_path / "snapshot.json"
    saved.write_text(json.dumps(snapshot))
    w3, pools, _ = snapshot_chain(snapshot)
    header = w3.eth.get_block
    w3.eth.get_block = lambda n: (
        header(n)
        | {
            "number": snapshot["block"]
            if n == snapshot["block"]
            else snapshot["block"] + 100
        }
    )
    reads = []
    get_logs = w3.eth.get_logs
    error = Web3RPCError(
        "private-provider-body",
        rpc_response={"error": {"code": code, "message": message}},
    )

    def fail(params):
        reads.append(params["toBlock"] - params["fromBlock"] + 1)
        raise error

    w3.eth.get_logs = fail
    adapter = Snapshots(w3, pools, saved)
    with pytest.raises(Web3RPCError) as exc:
        adapter.refresh()
    assert exc.value is error and reads == [100] and adapter.log_chunk == 100
    assert json.loads(saved.read_text()) == snapshot

    def recover(params):
        reads.append(params["toBlock"] - params["fromBlock"] + 1)
        return get_logs(params)

    w3.eth.get_logs = recover
    for p in snapshot["profiles"]:
        pool = pools[p["fee"]]["pool"]
        pool.functions.slot0 = lambda p=p: Call((p["sqrtPriceX96"], p["tick"]))
        pool.functions.liquidity = lambda p=p: Call(p["liquidity"])
    assert adapter.refresh()["block"] == snapshot["block"] + 100
    # The shared full 100-block chunk grows the ceiling for both pools.
    assert reads == [100, 100] and adapter.log_chunk == 200


def test_quiet_catchup_grows_log_chunks_instead_of_re_reading(
    snapshot, tmp_path, forbid_depth
):
    # One getLogs per quiet chunk: after a 350-block gap the adapter must read
    # 100 + 200 + 50 blocks shared by both pools, instead of separate ranges.
    saved = tmp_path / "snapshot.json"
    saved.write_text(json.dumps(snapshot))
    w3, pools, calls = snapshot_chain(snapshot)
    header = w3.eth.get_block
    gap = 350
    w3.eth.get_block = lambda n: (
        header(n)
        | {"number": snapshot["block"] + (0 if n == snapshot["block"] else gap)}
    )
    for p in snapshot["profiles"]:
        pool = pools[p["fee"]]["pool"]
        pool.functions.slot0 = lambda p=p: Call((p["sqrtPriceX96"], p["tick"]))
        pool.functions.liquidity = lambda p=p: Call(p["liquidity"])
    adapter = Snapshots(w3, pools, saved)
    assert adapter.refresh()["block"] == snapshot["block"] + gap
    reads = [c["toBlock"] - c["fromBlock"] + 1 for c in calls]
    assert reads == [100, 200, 50]
    assert adapter.log_chunk == 400


def test_controls_and_status_are_file_only(tmp_path, snapshot):
    svc = service(tmp_path, snapshot)
    control.save(
        svc.control_file,
        replace(strategy.DEFAULT, event_paused=False, event_frontrun=True),
    )
    svc.read_controls()
    svc.publish()
    value = dashboard_view(svc.store.directory / "state.json", svc.clock())
    assert not value["stale"] and value["controls"]["event_frontrun"]
    svc.control_file.write_text("broken")
    svc.read_controls()
    assert svc.controls.event_paused
    assert dashboard_view(svc.store.directory / "state.json", svc.clock() + 31)["stale"]


@pytest.mark.parametrize("key", control.EVENT_FIELDS)
def test_event_controls_reject_invalid_types(key):
    with pytest.raises(ValueError):
        strategy.parse_config({key: "enabled"})


@pytest.mark.parametrize(
    "gate",
    [
        "disabled",
        "paused",
        "position",
        "feed",
        "gap",
        "generation",
        "queue",
        "cooldown",
    ],
)
def test_cross_pool_waits_without_consuming_snapshot(
    tmp_path, snapshot, monkeypatch, gate
):
    svc = service(tmp_path, snapshot)
    svc.refresh()
    settings = replace(strategy.DEFAULT, event_paused=False, event_arbitrage=True)
    if gate == "disabled":
        settings = replace(settings, event_arbitrage=False)
    if gate == "paused":
        settings = replace(settings, event_paused=True)
    control.save(svc.control_file, settings)
    if gate == "position":
        svc.store.data["position"] = {"base_raw": A}
    if gate == "feed":
        svc.feed.ready.clear()
    if gate == "gap":
        svc.feed.gap.set()
    if gate == "generation":
        svc.generation = -1
    if gate == "queue":
        svc.feed.queue.put({"kind": "head"})
    if gate == "cooldown":
        svc.next_attempt = svc.clock() + 10
    monkeypatch.setattr(
        "event_service.backrun.best_cycle", lambda *a: pytest.fail("gated scan")
    )
    svc.cross_pool()
    assert svc.arbitrage_block is None and not svc.recent


def test_cross_pool_once_per_snapshot_and_resumes_after_pause(
    tmp_path, snapshot, monkeypatch
):
    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, mode="simulate")
    svc.refresh()
    controls = replace(strategy.DEFAULT, event_arbitrage=True)
    control.save(svc.control_file, controls)
    evaluations, executions = [], []

    def model(*args):
        evaluations.append(svc.engine.snapshot["block_hash"])
        return {"buy_fee": 500, "sell_fee": 10000}

    def execute(decision, action):
        executions.append((action, decision["hash"]))
        return {"status": "simulated"}

    monkeypatch.setattr("event_service.backrun.best_cycle", model)
    svc.executor.backrun = execute
    svc.cross_pool()
    assert not evaluations
    control.save(
        svc.control_file, replace(controls, event_paused=False, event_start_request=1)
    )
    svc.cross_pool()
    svc.next_attempt = 0
    svc.cross_pool()
    assert evaluations == [snapshot["block_hash"]]
    assert executions == [("arbitrage", snapshot["block_hash"])]
    svc.engine.snapshot["block_hash"] = HASH
    svc.cross_pool()
    assert executions[-1] == ("arbitrage", HASH) and len(executions) == 2


@pytest.mark.parametrize(
    "status,field",
    [
        ("prepared", "frontrun"),
        ("paper_revalidated", "backrun"),
        ("arbitrage_prepared", "backrun"),
    ],
)
def test_shared_position_blocks_every_new_strategy(tmp_path, snapshot, status, field):
    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, mode="simulate")
    svc.controls = replace(
        strategy.DEFAULT, event_paused=False, event_arbitrage=True, event_frontrun=True
    )
    svc.store.data["position"] = {"base_raw": A}
    svc.executor.backrun = svc.executor.enter = lambda *a, **k: pytest.fail(
        "spent shared balance with open position"
    )
    svc.maybe_execute({"status": status, field: {"buy_fee": 500}, "hash": HASH})


def test_cycle_execution_keys_distinguish_backrun_and_independent_scan(
    tmp_path, monkeypatch
):
    ex, _ = fake_executor(tmp_path, mode="simulate")
    ex.pools = {500: {}, 10000: {}}
    ex.bot.functions = SimpleNamespace(execute=lambda args: args)
    monkeypatch.setattr(
        "event_execution.arbitrage.quote_cycle",
        lambda *a, **k: {
            "profit": 1,
            "block": 7,
            "amount_in": A,
            "min_quote_out": A + 1,
            "deadline": 10**12,
        },
    )
    monkeypatch.setattr(
        "event_execution.arbitrage.simulate_cycle", lambda *a: {"args": (1,)}
    )
    ex.current = lambda *a: None
    calls = []
    ex.perform = lambda key, fn, action, *args, **kwargs: calls.append((key, action))
    decision = {
        "hash": HASH,
        "snapshot_block": 7,
        "snapshot_hash": HASH,
        "backrun": {"buy_fee": 500, "sell_fee": 10000},
    }
    ex.backrun(decision)
    ex.backrun(decision, action="arbitrage")
    assert calls == [("backrun:" + HASH, "backrun"), ("arbitrage:" + HASH, "arbitrage")]


def test_in_memory_single_swap_entry_restart_exit_preserves_deposits(
    combined, tmp_path, monkeypatch
):
    env = combined
    ex, _ = fake_executor(tmp_path)
    eth = env.w3.eth

    class TestEth:
        chain_id = 11155111

        def __getattr__(self, key):
            return getattr(eth, key)

        def get_transaction(self, tx_hash):
            return (
                {"blockNumber": None}
                if tx_hash == HASH
                else eth.get_transaction(tx_hash)
            )

        def get_transaction_count(self, address, block):
            return (
                1
                if address.lower() == market.MARKET_BOT
                else eth.get_transaction_count(address, block)
            )

    ex.w3 = SimpleNamespace(eth=TestEth())
    ex.operator, ex.bot = env.operator, env.bot
    ex.account = eth.account.from_key(
        env.w3.provider.ethereum_tester.backend.account_keys[1]
    )
    ex.clock = lambda: eth.get_block("latest")["timestamp"]
    ex.store.clock = ex.clock
    ex.fees = event_fees.FeeCache(ex.w3, ex.settings, ex.clock)
    quoter = SimpleNamespace(
        functions=SimpleNamespace(
            quoteExactInputSingle=lambda params: Call((params[2], 2**96, 0, 100000))
        )
    )
    ex.pools = {
        500: {
            "quoter": quoter,
            "pool": SimpleNamespace(
                functions=SimpleNamespace(slot0=lambda: Call((2**96, 0)))
            ),
        }
    }
    monkeypatch.setattr(market, "QUOTE", env.a.address.lower())
    monkeypatch.setattr(market, "BASE", env.b.address.lower())
    header = eth.get_block("latest")
    c = candidate() | {
        "snapshot_block": header["number"],
        "snapshot_hash": Web3.to_hex(header["hash"]),
        "frontrun": {"sell_fee": 500},
    }
    c["swap"]["deadline"] = header["timestamp"] + 120
    fn, _, minimum = ex.single(500, A, True, header["number"])
    ex.perform("entry:" + HASH, fn, "entry", A, minimum, c)
    assert ex.store.data["position"]["base_raw"] == A
    assert ex.store.data["position"]["entry_fee"] == 500
    assert env.b.functions.balanceOf(env.bot.address).call() == 4 * A
    ex.store = EventStore(tmp_path, {}, "live", clock=ex.clock)
    result = ex.exit("restart_test")
    assert result["status"] == "confirmed" and result["profit_raw"] == 0
    assert ex.store.data["position"] is None
    assert env.b.functions.balanceOf(env.bot.address).call() == 3 * A
    assert env.a.functions.balanceOf(env.bot.address).call() == 5000 * A
