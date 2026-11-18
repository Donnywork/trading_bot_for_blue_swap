import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_event_service import candidate, fake_executor
from web3 import Web3

import event_fees as fees
import event_market as market
import event_submission
import rpc_metrics as rpc
from event_settings import EventSettings
from legacy_main import Halt

GWEI = 10**9
HASH = "0x" + "ab" * 32
RIVAL = EventSettings.competitors[0]


def header(base=10 * GWEI, number=20, timestamp=100):
    return {
        "number": number,
        "hash": bytes.fromhex(HASH[2:]),
        "timestamp": timestamp,
        "baseFeePerGas": base,
    }


def transaction(**kwargs):
    return {
        "hash": "0x" + "cd" * 32,
        "from": RIVAL,
        "nonce": "0x1",
        "chainId": hex(11155111),
        "to": market.ROUTER,
        "value": "0x0",
        "type": "0x2",
        "maxPriorityFeePerGas": hex(30 * GWEI),
        "maxFeePerGas": hex(35 * GWEI),
        "input": market.router().encode_abi(
            "exactInputSingle",
            args=[
                (
                    Web3.to_checksum_address(market.QUOTE),
                    Web3.to_checksum_address(market.BASE),
                    500,
                    Web3.to_checksum_address(RIVAL),
                    100 * market.UNIT,
                    1,
                    0,
                )
            ],
        ),
    } | kwargs


def cache(settings=None, eth=None):
    clock = [100, 1000]
    instance = fees.FeeCache(
        SimpleNamespace(eth=eth),
        settings or replace(EventSettings(), fee_policy="competitive"),
        lambda: clock[0],
        lambda: clock[1],
    )
    return instance, clock


def network(instance, tip=24 * GWEI):
    instance.network = {
        "tip_wei": tip,
        "block": 20,
        "block_hash": HASH,
        "block_time": 100,
        "at": instance.clock(),
        "mono": instance.monotonic(),
        "source": "fee_history_8_blocks_p90_max",
    }


def history(tip=24 * GWEI):
    return {
        "oldestBlock": hex(13),
        "reward": [[hex(tip)]] * fees.WINDOW,
        "baseFeePerGas": [hex(10 * GWEI)] * (fees.WINDOW + 1),
    }


def test_defaults_and_distinct_fixed_and_competitive_settings():
    fixed = EventSettings.from_env({})
    assert (
        fixed.fee_policy == "fixed"
        and fixed.priority_wei == 2 * GWEI
        and fixed.max_fee_wei == 20 * GWEI
    )
    s = EventSettings.from_env(
        {
            "EVENT_FEE_POLICY": "competitive",
            "EVENT_PRIORITY_FEE_WEI": "1",
            "EVENT_MAX_FEE_WEI": "2",
        }
    )
    assert s.fee_baseline_wei == 20 * GWEI and s.fee_ceiling == 200 * GWEI
    c, _ = cache(s)
    assert c.decide(header(), "entry")["selected_priority_wei"] == 20 * GWEI


@pytest.mark.parametrize(
    "env",
    [
        {"EVENT_FEE_POLICY": "auto"},
        {"EVENT_FEE_BASELINE_WEI": "20.0"},
        {"EVENT_FEE_NETWORK_BPS": "1.25"},
        {"EVENT_FEE_NETWORK_BPS": "9999"},
        {"EVENT_FEE_COMPETITOR_BPS": "50001"},
        {"EVENT_FEE_BASELINE_WEI": "0"},
        {"EVENT_FEE_TIP_CAP_WEI": str(101 * GWEI)},
        {"EVENT_FEE_MAX_CAP_WEI": str(201 * GWEI)},
        {"EVENT_FEE_TIP_CAP_WEI": "1"},
        {"EVENT_FEE_MAX_CAP_WEI": str(99 * GWEI)},
    ],
)
def test_invalid_configuration(env):
    with pytest.raises(ValueError):
        EventSettings.from_env(env)


def test_arithmetic_uses_effective_tip_and_rounds_up_in_wei():
    c, _ = cache()
    network(c)
    c.observe(transaction())
    d = c.decide(header(), "backrun")
    assert d["competitor"]["tip_wei"] == 25 * GWEI  # min(30, 35 - 10), never 35
    assert d["desired_priority_wei"] == d["selected_priority_wei"] == 37_500_000_000
    assert d["selected_max_fee_wei"] == 57_500_000_000
    assert d["selected_tip_above_observed_estimate"] is True
    assert not d["ordering_guaranteed"]
    c.settings = replace(c.settings, fee_baseline_wei=1)
    c.observations.clear()
    network(c, 3)
    assert c.decide(header(base=1), "exit")["selected_priority_wei"] == 4
    assert fees.multiply(2**200 + 1, 15000) == ((2**200 + 1) * 3 + 1) // 2


@pytest.mark.parametrize(
    "base,tip,maximum,allowed",
    [
        (10, 100, 120, True),
        (120, 80, 200, True),
        (195, 5, 200, True),
        (200, 0, 200, False),
        (201, 0, 200, False),
    ],
)
def test_extreme_observation_never_escapes_ceilings(base, tip, maximum, allowed):
    c, _ = cache()
    c.observe(transaction(maxPriorityFeePerGas=hex(2**250), maxFeePerGas=hex(2**251)))
    d = c.decide(header(base=base * GWEI), "exit")
    assert d["desired_priority_wei"] > 200 * GWEI
    assert d["selected_priority_wei"] == tip * GWEI
    assert d["selected_max_fee_wei"] == maximum * GWEI
    assert d["allowed"] == allowed and "priority_ceiling" in d["reasons"]
    assert d["selected_tip_above_observed_estimate"] is (False if allowed else None)
    if allowed:
        assert d["selected_max_fee_wei"] >= base * GWEI + d["selected_priority_wei"]


@pytest.mark.parametrize(
    "fields,base,expected",
    [
        (
            {"type": "0x2", "maxFeePerGas": "0x64", "maxPriorityFeePerGas": "0x50"},
            50,
            50,
        ),
        (
            {
                "type": 2,
                "maxFeePerGas": 100,
                "maxPriorityFeePerGas": 20,
                "gasPrice": 999,
            },
            50,
            20,
        ),
        ({"gasPrice": "0x64"}, 60, 40),
        ({"type": 1, "gasPrice": 50}, 60, 0),
        ({"type": 2, "maxFeePerGas": 50, "maxPriorityFeePerGas": 20}, 60, 0),
    ],
)
def test_eip1559_and_legacy_effective_tip(fields, base, expected):
    assert fees.effective_tip(fees.fee_fields(fields), base) == expected


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"gasPrice": True},
        {"gasPrice": 1.5},
        {"gasPrice": -1},
        {"gasPrice": 2**256},
        {"type": 3, "gasPrice": 1},
        {"maxFeePerGas": 100},
        {"maxFeePerGas": 1, "maxPriorityFeePerGas": 2},
    ],
)
def test_invalid_fee_fields(fields):
    with pytest.raises(ValueError):
        fees.fee_fields(fields)


def test_relevance_replacements_duplicate_expiry_and_mined_winner():
    c, now = cache()
    first = transaction()
    c.observe(first)
    second = transaction(hash="0x" + "ef" * 32, maxFeePerGas=hex(12 * GWEI))
    second["maxPriorityFeePerGas"] = hex(11 * GWEI)
    now[0] += 1
    now[1] += 1
    c.observe(second)
    c.observe(first)  # Late pending duplicate cannot undo replacement.
    assert len(c.observations) == 1
    assert c.decide(header(), "entry")["competitor"]["hash"] == second["hash"]
    # The original can still win inclusion. Its fields remain an estimate at our reference base.
    c.observe(
        first | {"blockNumber": "0x14", "blockHash": HASH, "transactionIndex": "0x3"}
    )
    assert c.decide(header(), "entry")["competitor"]["hash"] == first["hash"]
    c.observe(second | {"hash": "0x" + "01" * 32})
    assert c.decide(header(), "entry")["competitor"]["hash"] == first["hash"]
    now[0] += 61
    now[1] += 61
    assert c.decide(header(timestamp=now[0]), "entry")["competitor"] is None
    assert not c.observations and not c.seen


@pytest.mark.parametrize(
    "change",
    [
        {"to": "0x" + "11" * 20},
        {"input": "0xdeadbeef"},
        {"gasPrice": 1, "maxFeePerGas": None},
        {"value": 1},
        {"maxPriorityFeePerGas": -1},
    ],
)
def test_unsupported_or_bad_replacement_removes_prior_bid(change):
    c, _ = cache()
    c.observe(transaction())
    c.observe(transaction(hash="0x" + "ef" * 32) | change)
    assert c.decide(header(), "entry")["competitor"] is None


def test_unknown_signers_and_other_pair_cannot_supply_bid():
    c, _ = cache()
    c.observe(transaction(**{"from": "0x" + "22" * 20}))
    c.observe(transaction(chainId="0x1"))
    tx = transaction()
    tx["input"] = market.router().encode_abi(
        "exactInputSingle",
        args=[
            (
                Web3.to_checksum_address(market.QUOTE),
                Web3.to_checksum_address(market.BASE),
                3000,
                Web3.to_checksum_address(RIVAL),
                10,
                1,
                0,
            )
        ],
    )
    c.observe(tx)
    assert c.decide(header(), "exit")["competitor"] is None


def test_observations_are_bounded_and_old_arrivals_are_not_fresh():
    c, _ = cache()
    tx = transaction()
    for n in range(600):
        c.observe(tx | {"hash": "0x" + f"{n:064x}", "nonce": n})
    assert len(c.observations) == fees.MAX_OBSERVATIONS
    assert len(c.seen) == fees.MAX_HASHES
    c.invalidate()
    c.observe(tx, received=0, received_mono=900)
    assert not c.seen
    c.observe(tx | {"blockNumber": 20, "blockHash": HASH, "transactionIndex": 0})
    for n in range(600):
        c.observe(tx | {"hash": "0x" + f"{n:064x}"})
    assert len(c.seen) <= fees.MAX_HASHES and len(c.observations) == 1


def test_fallback_age_block_distance_zero_and_reference_checks():
    c, now = cache()
    d = c.decide(header(), "entry")
    assert d["reasons"] == ["network_missing", "competitor_missing"]
    assert (
        d["selected_priority_wei"] == 20 * GWEI
        and d["selected_max_fee_wei"] == 40 * GWEI
    )
    network(c, 100 * GWEI)
    now[1] += 37
    d = c.decide(header(), "exit")
    assert d["network_status"] == "stale" and not d["network"]["usable"]
    assert d["selected_priority_wei"] == 20 * GWEI
    network(c, 100 * GWEI)
    assert c.decide(header(number=24), "entry")["network_status"] == "stale"
    network(c, None)
    assert c.decide(header(), "entry")["network_status"] == "zero_samples"
    for bad in (header(timestamp=69), header(timestamp=101), header(base=-1)):
        with pytest.raises(ValueError):
            c.decide(bad, "exit")
    for bad in ({}, None, header() | {"baseFeePerGas": None}):
        with pytest.raises(ValueError, match="unavailable or invalid"):
            c.decide(bad, "exit")


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"oldestBlock": 12},
        {"reward": [[0]] * 7},
        {"reward": [[-1]] * 8},
        {"reward": [[1.5]] * 8},
        {"reward": [[]] * 8},
        {"baseFeePerGas": [1] * 9},
    ],
)
def test_network_malformed_samples_fail_safely(malformed):
    value = malformed if not malformed else history() | malformed
    c, _ = cache(
        eth=SimpleNamespace(get_block=lambda _: header(), fee_history=lambda *a: value)
    )
    c.refresh(c.epoch)
    assert c.network is None and "failed" in c.error
    assert c.decide(header(), "exit")["selected_priority_wei"] == 20 * GWEI


def test_history_zeros_and_high_percentile_aggregation():
    assert fees.history_sample(history(0), header()) is None
    value = history(0)
    value["reward"][1] = [5]
    value["reward"][6] = [9]
    assert fees.history_sample(value, header()) == 9


def test_worker_rate_bound_thread_safety_metrics_and_invalidation():
    entered, release = threading.Event(), threading.Event()
    calls = []
    metrics = rpc.RpcMetrics()

    def get_block(n):
        calls.append(("block", n))
        metrics.attempt()
        return header()

    def fee_history(*args):
        calls.append(("history", args))
        metrics.attempt()
        entered.set()
        assert release.wait(2)
        return history()

    c, now = cache(eth=SimpleNamespace(get_block=get_block, fee_history=fee_history))
    c.schedule()
    assert entered.wait(2)
    completed = threading.Event()

    def use_cache():
        c.observe(transaction())
        assert c.decide(header(), "exit")["allowed"]
        completed.set()

    reader = threading.Thread(target=use_cache)
    reader.start()
    try:
        assert completed.wait(1)  # No fee-cache lock is held during HTTP waits.
        for _ in range(10):
            c.schedule()
        assert len(calls) == 2
        c.invalidate()  # An in-flight response from the old branch is discarded.
    finally:
        release.set()
        reader.join(2)
        c.thread.join(2)
    assert c.network is None
    c.schedule()
    assert len(calls) == 3
    now[1] += 12
    c.schedule()
    c.thread.join(2)
    assert c.network["tip_wei"] == 24 * GWEI and len(calls) == 6
    old_at = c.network["at"]
    now[1] += 12
    c.schedule()
    c.thread.join(2)
    assert (
        len(calls) == 7 and c.network["at"] == old_at
    )  # Same block: just the header read.
    assert metrics.snapshot()["by_purpose"][rpc.FEES] == 7


def test_refresh_failure_retains_original_age_and_suppresses_provider_body():
    def fail(*args):
        raise ConnectionError("https://private-provider.invalid/credential")

    c, now = cache(eth=SimpleNamespace(get_block=fail))
    network(c)
    before = dict(c.network)
    c.refresh(c.epoch)
    assert c.network == before
    assert "credential" not in c.error
    now[0] += 37
    now[1] += 37
    d = c.decide(header(timestamp=137), "exit")
    assert d["network_status"] == "stale" and d["allowed"]


def test_fixed_uses_exact_old_fields_without_background_rpc():
    c, _ = cache(EventSettings())
    c.schedule()
    c.observe(transaction())
    assert c.thread is None
    d = c.decide(header(base=18 * GWEI), "entry")
    assert (
        d["selected_priority_wei"] == 2 * GWEI
        and d["selected_max_fee_wei"] == 20 * GWEI
    )
    assert (
        d["allowed"] and d["network_status"] == "not_used" and d["competitor"] is None
    )
    assert not c.decide(header(base=18 * GWEI + 1), "exit")["allowed"]


def test_monotonic_durations_and_unavailable_evidence():
    now = [100, 1000]
    t = event_submission.Timing({"at": 99, "mono": 999}, lambda: now[0], lambda: now[1])
    now[1] += 0.25
    t.mark("validation_complete")
    now[0] -= 20  # Wall-clock adjustments do not change local durations.
    now[1] += 0.1
    t.mark("broadcast_start")
    now[1] += 0.2
    t.mark("broadcast_return")
    assert t.data["validation_ms"] == 250
    assert t.data["validation_to_broadcast_ms"] == 100
    assert t.data["first_seen_to_broadcast_ms"] == 1350
    assert t.data["broadcast_ms"] == 200
    assert t.data["provider_accepted"]
    assert event_submission.Timing().data["first_seen_at"] is None


@pytest.mark.parametrize("policy", ["fixed", "competitive"])
@pytest.mark.parametrize("action", ["entry", "backrun", "arbitrage", "exit"])
def test_selected_fee_reaches_real_sender_fake_signer_boundary(
    tmp_path, policy, action
):
    ex, fn = fake_executor(tmp_path)
    ex.settings = replace(ex.settings, fee_policy=policy)
    ex.fees = fees.FeeCache(ex.w3, ex.settings, ex.clock)
    calls, signed = [], []
    old_get = ex.w3.eth.get_block
    ex.w3.eth.get_block = lambda n: calls.append(("block", n)) or old_get(n)
    ex.w3.eth.get_transaction_count = lambda *a: calls.append(("nonce", a[1])) or 1
    ex.w3.eth.fee_history = lambda *a: pytest.fail("fee history in submission path")

    def sign(tx):
        intent = json.loads(ex.store.path.read_text())["intent"]
        d = intent["fee_decision"]
        assert intent["nonce"] == tx["nonce"]
        assert tx["maxPriorityFeePerGas"] == d["selected_priority_wei"]
        assert tx["maxFeePerGas"] == d["selected_max_fee_wei"]
        assert tx["gas"] == intent["gas_limit"] == 200_000
        signed.append(dict(tx))
        return SimpleNamespace(raw_transaction=b"frozen-test-bytes")

    ex.account.sign_transaction = sign

    def send(raw):
        assert raw == b"frozen-test-bytes"
        calls.append(("broadcast", raw))
        return SimpleNamespace(to_0x_hex=lambda: "0x" + "cd" * 32)

    ex.w3.eth.send_raw_transaction = send
    receipt = {
        "status": 1,
        "blockNumber": 7,
        "blockHash": bytes.fromhex(HASH[2:]),
        "transactionHash": bytes.fromhex("cd" * 32),
        "transactionIndex": 4,
        "gasUsed": 50_000,
        "effectiveGasPrice": (2 if policy == "fixed" else 20) * GWEI + 1,
        "logs": [
            {
                "address": market.BASE if action == "entry" else market.QUOTE,
                "topics": [
                    Web3.keccak(text="Transfer(address,address,uint256)"),
                    bytes(32),
                    bytes.fromhex("00" * 12 + "33" * 20),
                ],
                "data": (2 * market.UNIT).to_bytes(32, "big"),
            }
        ],
    }
    ex.w3.eth.wait_for_transaction_receipt = lambda _: receipt
    c = candidate() | {
        "target_inclusion": {
            "block": 7,
            "block_hash": HASH,
            "transaction_index": 2,
            "source": "receipt_matching_snapshot",
        }
    }
    if action == "exit":
        ex.store.data["position"] = {"quote_raw": market.UNIT, "exit_fee": 500}
        ex.entry_guard = lambda a: pytest.fail("exit hit entry control")
    result = ex.perform(action + ":" + HASH, fn, action, market.UNIT, 1, c)
    assert len(signed) == 1
    assert signed[0]["maxPriorityFeePerGas"] == (2 if policy == "fixed" else 20) * GWEI
    assert (
        result["inclusion"]["effective_priority_wei"]
        == (2 if policy == "fixed" else 20) * GWEI
    )
    assert result["inclusion"]["target"]["index_delta"] == 2
    assert result["inclusion"]["competitor"] is None
    assert result["timing"]["first_seen_at"] is None
    assert (
        result["timing"]["broadcast_ms"] >= 0 and result["timing"]["provider_accepted"]
    )
    assert (
        len([c for c in calls if c[0] == "block"]) == 2
    )  # Final freshness + receipt, no fee-only read.
    assert len([c for c in calls if c[0] == "nonce"]) == (2 if action == "entry" else 1)


@pytest.mark.parametrize(
    "gate", ["gas", "balance", "snapshot", "target", "nonce", "controls"]
)
def test_competitive_fees_cannot_override_execution_guards(tmp_path, gate):
    ex, fn = fake_executor(tmp_path)
    ex.settings = replace(ex.settings, fee_policy="competitive")
    ex.fees = fees.FeeCache(ex.w3, ex.settings, ex.clock)
    ex.account.sign_transaction = lambda tx: pytest.fail("signed after failed guard")
    if gate == "gas":
        fn.estimate_gas = lambda _: 750_001
    elif gate == "balance":
        ex.w3.eth.get_balance = lambda _: 0
    elif gate == "snapshot":
        ex.w3.eth.get_block = lambda _: header(number=8)
    elif gate == "target":
        ex.w3.eth.get_transaction = lambda _: {"blockNumber": 7}
    elif gate == "nonce":
        ex.w3.eth.get_transaction_count = lambda *a: 2
    else:

        def reject(action):
            raise ValueError("controls changed")

        ex.entry_guard = reject
    with pytest.raises(ValueError):
        ex.perform("entry:" + HASH, fn, "entry", 1, 1, candidate())
    assert ex.store.data["intent"] is None


def test_unknown_broadcast_retains_frozen_decision_and_never_retries(tmp_path):
    ex, fn = fake_executor(tmp_path)
    ex.settings = replace(ex.settings, fee_policy="competitive")
    ex.fees = fees.FeeCache(ex.w3, ex.settings, ex.clock)
    signed, sent = [], []
    ex.account.sign_transaction = lambda tx: (
        signed.append(tx) or SimpleNamespace(raw_transaction=b"fixed")
    )

    def fail(raw):
        sent.append(raw)
        raise ConnectionError("private endpoint")

    ex.w3.eth.send_raw_transaction = fail
    with pytest.raises(Halt):
        ex.perform("entry:" + HASH, fn, "entry", 1, 1, candidate())
    intent = json.loads(ex.store.path.read_text())["intent"]
    assert intent["fee_decision"]["selected_priority_wei"] == 20 * GWEI
    assert intent["timing"]["broadcast_start_at"] is not None
    assert intent["timing"]["broadcast_return_at"] is None
    assert not intent["timing"]["provider_accepted"]
    with pytest.raises(ValueError, match="already attempted"):
        ex.perform("entry:" + HASH, fn, "entry", 1, 1, candidate())
    assert len(signed) == len(sent) == 1
    assert "private endpoint" not in ex.store.path.read_text()


def test_inclusion_missing_fields_and_conflicting_branches():
    receipt = {"blockNumber": 20, "blockHash": bytes.fromhex(HASH[2:])}
    d = event_submission.inclusion(receipt)
    assert d["effective_priority_wei"] is None and d["transaction_index"] is None
    other = {"block": 20, "block_hash": "0x" + "11" * 32, "transaction_index": 1}
    assert event_submission.relationship(receipt, other) is None


def test_mined_evidence_uses_existing_receipt_and_excludes_old_blocks():
    c, _ = cache()
    tx = transaction(blockNumber="0x14", blockHash=HASH, transactionIndex="0x2")
    c.observe(tx)
    receipt = {
        "transactionHash": bytes.fromhex(tx["hash"][2:]),
        "from": RIVAL,
        "status": 1,
        "blockNumber": 20,
        "blockHash": bytes.fromhex(HASH[2:]),
        "transactionIndex": 2,
    }
    snapshot = {"block": 20, "block_hash": HASH}
    c.confirm(tx, receipt, snapshot)
    d = c.decide(header(), "entry")
    assert d["competitor"]["inclusion"]["source"] == "receipt_matching_snapshot"
    assert c.decide(header(number=26), "entry")["competitor"] is None
    c.invalidate()
    c.observe(tx)
    c.confirm(tx, receipt | {"blockHash": bytes(32)}, snapshot)
    assert (
        c.decide(header(), "entry")["competitor"]["inclusion"]["source"]
        == "mined_feed_unverified"
    )


def test_pending_deadline_expires_bid_before_observation_ttl():
    c, now = cache()
    tx = transaction()
    tx["input"] = market.router().encode_abi(
        "multicall(uint256,bytes[])", args=[101, [Web3.to_bytes(hexstr=tx["input"])]]
    )
    c.observe(tx)
    assert c.decide(header(), "entry")["competitor"] is not None
    now[0] += 2
    now[1] += 2
    assert c.decide(header(), "entry")["competitor"] is None


def test_slow_or_reorganized_refresh_is_not_published():
    c, now = cache()

    def slow(*args):
        now[0] += 31
        now[1] += 31
        return history()

    c.w3.eth = SimpleNamespace(get_block=lambda _: header(), fee_history=slow)
    c.refresh(c.epoch)
    assert c.network is None
    now[:] = [100, 1000]
    c.w3.eth.get_block = lambda n: (
        header() if n == "latest" else header() | {"hash": bytes(32)}
    )
    c.w3.eth.fee_history = lambda *a: history()
    c.refresh(c.epoch)
    assert c.network is None
