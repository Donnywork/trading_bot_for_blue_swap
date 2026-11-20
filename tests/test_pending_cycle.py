"""Same-block pending atomic-cycle submission (EVENT_PENDING_BACKRUN)."""

import copy
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_event_service import HASH, A, candidate, fake_executor, service

import arbitrage
import bot_contract
import control
import event_fees
import event_market as market
import strategy
from event_execution import sizes_near
from event_settings import EventSettings
from runtime import ConfigurationError

ROOT = Path(__file__).parents[1]


@pytest.fixture
def snapshot():
    return json.loads(
        (ROOT / "research/2026-11-17-phase2-arbitrage/snapshot.json").read_text()
    )


@pytest.fixture
def pending_env(tmp_path, snapshot):
    ex, _ = fake_executor(tmp_path, "simulate")
    ex.settings = replace(
        ex.settings, mode="simulate", quote_cap=640 * A, min_profit=A // 100
    )
    calls = []

    class Eth:
        @property
        def chain_id(self):
            calls.append(("chain",))
            return 11155111

        def get_block(self, block):
            calls.append(("header", block))
            return {
                "number": 7,
                "hash": bytes.fromhex("ab" * 32),
                "timestamp": 99,
                "baseFeePerGas": 1,
            }

        def get_balance(self, address):
            calls.append(("balance",))
            return 10**20

        def get_transaction_count(self, *args):
            calls.append(("nonce",))
            return 1

        def get_transaction(self, tx_hash):
            calls.append(("transaction",))
            return {"blockNumber": None}

    ex.w3.eth = Eth()
    ex.fees = event_fees.FeeCache(ex.w3, ex.settings, ex.clock)
    built = {}

    class Execute:
        def __init__(self, args):
            self.arguments = (args,)

        def estimate_gas(self, tx):
            # A pending cycle estimates against pre-target state and would
            # revert; the override path must never reach this.
            calls.append(("estimate", "latest"))
            return 100_000

        def build_transaction(self, tx):
            calls.append(("build",))
            return tx

        def call(self, *args, **kwargs):
            calls.append(("execute_sim",))
            return A

    def execute(args):
        built["args"] = args
        return Execute(args)

    ex.bot = SimpleNamespace(
        address=ex.bot.address, functions=SimpleNamespace(execute=execute)
    )
    # Any slot0/quoter access is a bug: the pinned snapshot already carries
    # the reconciled prices the cycle needs.
    ex.pools = {
        fee: {
            "pool": SimpleNamespace(
                functions=SimpleNamespace(
                    slot0=lambda: pytest.fail("pending cycle re-read slot0")
                )
            ),
            "quoter": SimpleNamespace(
                functions=SimpleNamespace(
                    quoteExactInputSingle=lambda params: pytest.fail(
                        "pending cycle quoted pre-target state"
                    )
                )
            ),
        }
        for fee in market.POOLS
    }
    pinned = copy.deepcopy(snapshot)
    pinned.update(block=7, block_hash=HASH, timestamp=95)
    for profile in pinned["profiles"]:
        profile["block"] = 7
    decision = {
        "hash": HASH,
        "status": "prepared",
        "snapshot_block": 7,
        "snapshot_hash": HASH,
        "backrun": {"buy_fee": 500, "sell_fee": 10000, "input_f564": 100},
        "frontrun": None,
        "swap": {"sender": market.MARKET_BOT, "nonce": 1, "deadline": 200},
    }
    return SimpleNamespace(**locals())


def snapshot_sqrt(pinned, fee):
    return next(p["sqrtPriceX96"] for p in pinned["profiles"] if p["fee"] == fee)


def test_pending_cycle_submits_bracket_rung_with_snapshot_limits(pending_env):
    f = pending_env
    result = f.ex.pending_cycle(f.decision, f.pinned)
    args = f.built["args"]
    rung = sizes_near(f.ex.settings.quote_cap, int(100 * A))[0][0]
    assert result["status"] == "simulated"
    assert result["stage"] == "pending_submission"
    assert result["action"] == "backrun"
    assert result["mode"] == "simulate"
    assert result["snapshot_block"] == 7
    assert result["quote_rungs"] == 0  # nothing exact-quoted pre-target
    assert args[0] is True  # buy leg runs through pool0 (fee 500)
    assert args[1] == rung == result["amount_raw"]
    assert rung in sizes_near(f.ex.settings.quote_cap, 0)[0]
    assert args[2] == 1  # no quote exists: only the contract's nonzero minimum
    assert args[3] == rung + f.ex.settings.min_profit == result["minimum_raw"]
    assert args[4] == f.ex.settings.min_profit
    assert args[5] == 129  # max(latest header 99, snapshot 95) + 30
    assert args[6] == arbitrage.price_limit(
        snapshot_sqrt(f.pinned, 500),
        int(market.QUOTE, 16) < int(market.BASE, 16),
        f.ex.settings.move_bps,
    )
    assert args[7] == arbitrage.price_limit(
        snapshot_sqrt(f.pinned, 10000),
        int(market.BASE, 16) < int(market.QUOTE, 16),
        f.ex.settings.move_bps,
    )
    assert f.ex.store.data["position"] is None
    assert f.ex.store.data["attempts"] == {"backrun_pending:" + HASH: "simulated"}
    # Read budget: latest head + canonical pinned block (pending_header) +
    # chain + balance. Simulate skips the operator nonce read; perform()
    # reuses the pending header instead of re-reading freshness.
    counts = Counter(row[0] for row in f.calls)
    assert counts == {"chain": 1, "header": 2, "balance": 1}


def test_pending_cycle_live_never_estimates_never_doubles_the_override(
    pending_env, monkeypatch
):
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live")
    f.decision["backrun"] = {"buy_fee": 10000, "sell_fee": 500, "input_f564": 100}
    sends = []

    def send(*args, **kwargs):
        sends.append((kwargs["gas_buffer"], args[2]["gas"]))
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "not_sent"
    assert f.built["args"][0] is False  # buy leg flips with the plan's fees
    assert result["estimated_gas"] is None
    assert result["gas_limit"] == f.ex.settings.max_gas
    assert sends == [(1.0, f.ex.settings.max_gas)]
    counts = Counter(row[0] for row in f.calls)
    assert counts == {"chain": 1, "header": 2, "balance": 1, "nonce": 1, "build": 1}
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {"backrun_pending:" + HASH: "not_sent"}


def test_gas_override_skips_estimate_and_keeps_estimated_buffer(
    pending_env, monkeypatch
):
    f = pending_env
    # The estimated path (no override) keeps the 2x swap buffer untouched.
    estimated = []

    fn = SimpleNamespace(
        arguments=((True, 500, A, A // 2, 99 + 30, 2**96),),
        estimate_gas=lambda tx: estimated.append(True) or 100_000,
        build_transaction=lambda tx: tx,
        call=lambda *a, **k: A,
    )
    row = f.ex.perform("entry:estimated", fn, "entry", A, A // 2, candidate())
    assert row["estimated_gas"] == 100_000 and row["gas_limit"] == 200_000
    assert estimated == [True]
    # The override path still enforces operator affordability.
    f.ex.w3.eth.get_balance = lambda address: 1
    with pytest.raises(
        ConfigurationError, match="operator ETH below maximum transaction cost"
    ):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["intent"] is None
    assert estimated == [True]  # the pending cycle added no estimate


def test_invalid_plan_or_advanced_snapshot_rejects_without_intent(pending_env):
    f = pending_env
    broken = f.decision | {"backrun": {"buy_fee": 500, "sell_fee": 10000}}
    with pytest.raises(ValueError, match="plan is invalid"):
        f.ex.pending_cycle(broken, f.pinned)
    with pytest.raises(ValueError, match="plan is invalid"):
        f.ex.pending_cycle(f.decision | {"backrun": {}}, f.pinned)
    moved = copy.deepcopy(f.pinned)
    moved.update(block=8, block_hash="0x" + "cd" * 32)
    with pytest.raises(ConfigurationError, match="snapshot advanced"):
        f.ex.pending_cycle(f.decision, moved)
    with pytest.raises(ConfigurationError, match="snapshot advanced"):
        f.ex.pending_cycle(f.decision, None)
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}


def test_pending_cycle_deadline_guards_reject_slow_paths(pending_env, monkeypatch):
    f = pending_env
    monkeypatch.setattr(
        f.ex.store, "begin", lambda *a: pytest.fail("unexpected durable intent")
    )
    f.ex.clock = lambda: 125  # deadline 129 leaves 4 s: under send + next block
    with pytest.raises(ConfigurationError, match="deadline cannot cover inclusion"):
        f.ex.pending_cycle(f.decision, f.pinned)
    # A slow final control guard advances the wall clock inside perform().
    f.ex.clock = lambda: 100

    def slow_guard(action):
        f.ex.clock = lambda: 125

    f.ex.entry_guard = slow_guard
    with pytest.raises(ConfigurationError, match="deadline cannot cover inclusion"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}
    assert f.ex.attempt["stage"] == "pending_submission"


def test_duplicate_pending_submission_blocked_by_distinct_key(pending_env):
    f = pending_env
    f.ex.pending_cycle(f.decision, f.pinned)
    with pytest.raises(ValueError, match="already attempted"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["trades"][-1]["status"] == "simulated"


def test_reverted_pending_cycle_records_gas_only_without_position(
    pending_env, monkeypatch
):
    # Target replaced before inclusion: our cycle lands against different
    # state and reverts. The existing revert accounting keeps gas only.
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live")
    receipt = {
        "blockNumber": 7,
        "blockHash": bytes.fromhex("ab" * 32),
        "transactionHash": bytes.fromhex("cd" * 32),
        "status": 0,
        "gasUsed": 50000,
        "effectiveGasPrice": 10,
        "transactionIndex": 2,
    }

    def send(*args, **kwargs):
        raise bot_contract.Reverted("0x" + "cd" * 32, receipt)

    monkeypatch.setattr(bot_contract, "send_tx", send)
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "reverted"
    assert result["gas_wei"] == 500000
    assert result["estimated_gas"] is None
    assert f.ex.store.data["position"] is None
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {"backrun_pending:" + HASH: "reverted"}


def prepared(snapshot, hash_=HASH):
    return {
        "kind": "pending",
        "status": "prepared",
        "hash": hash_,
        "snapshot_block": snapshot["block"],
        "snapshot_hash": snapshot["block_hash"],
        "backrun": {
            "buy_fee": 500,
            "sell_fee": 10000,
            "input_f564": 100,
            "profit_f564": 12.3,
        },
        "frontrun": None,
    }


def armed_service(tmp_path, snapshot, *, pending_flag=True):
    svc = service(tmp_path, snapshot)
    svc.settings = replace(svc.settings, mode="simulate", pending_backrun=pending_flag)
    control.save(
        svc.control_file,
        replace(strategy.DEFAULT, event_paused=False, event_start_request=1),
    )
    svc.read_controls()
    svc.refresh()
    return svc


def test_service_attempts_pending_submission_immediately(tmp_path, snapshot):
    svc = armed_service(tmp_path, snapshot)
    calls = []

    def pending_cycle(decision, engine_snapshot):
        calls.append((decision["hash"], engine_snapshot["block"]))
        assert engine_snapshot["block"] == decision["snapshot_block"]
        return {
            "kind": "execution",
            "status": "simulated",
            "action": "backrun",
            "hash": decision["hash"],
        }

    svc.executor.pending_cycle = pending_cycle
    # No mined receipt exists: the prepared decision itself triggers the send.
    svc.maybe_execute(prepared(svc.engine.snapshot))
    assert calls == [(HASH, svc.engine.snapshot["block"])]
    assert svc.last_execution["status"] == "simulated"
    # Duplicate submission of the same hash is blocked by the attempt key.
    svc.store.data["attempts"]["backrun_pending:" + HASH] = "simulated"
    svc.next_attempt = 0
    svc.maybe_execute(prepared(svc.engine.snapshot))
    assert len(calls) == 1


def test_service_pending_gate_burns_no_cooldown(tmp_path, snapshot):
    svc = armed_service(tmp_path, snapshot)
    svc.executor.pending_cycle = lambda *a: pytest.fail("submission attempted")
    svc.executor.backrun = lambda *a, **k: pytest.fail("mined path attempted")
    # Floor not met: the engine records no plan at all.
    decision = prepared(svc.engine.snapshot) | {"backrun": None}
    svc.maybe_execute(decision)
    # Module off via settings.
    svc.settings = replace(svc.settings, pending_backrun=False)
    svc.maybe_execute(prepared(svc.engine.snapshot))
    # Module off via the dashboard control.
    svc.settings = replace(svc.settings, pending_backrun=True)
    svc.controls = replace(svc.controls, event_backrun=False)
    svc.maybe_execute(prepared(svc.engine.snapshot))
    # Paused entries.
    svc.controls = replace(svc.controls, event_backrun=True, event_paused=True)
    svc.maybe_execute(prepared(svc.engine.snapshot))
    # Cooldown not elapsed.
    svc.controls = replace(svc.controls, event_paused=False)
    svc.next_attempt = svc.clock() + 10
    svc.maybe_execute(prepared(svc.engine.snapshot))
    assert svc.next_attempt == svc.clock() + 10  # nothing above burned a cooldown
    # A guard refusal (feed gap) records a rejection without a cooldown too.
    svc.next_attempt = 0
    svc.feed.gap.set()
    svc.maybe_execute(prepared(svc.engine.snapshot))
    assert svc.next_attempt == 0
    assert svc.last_execution["status"] == "execution_rejected"


def test_service_records_expected_pending_rejections_without_raising(
    tmp_path, snapshot
):
    svc = armed_service(tmp_path, snapshot)

    def reject(decision, engine_snapshot):
        raise ValueError("pending backrun plan is invalid")

    svc.executor.pending_cycle = reject
    decision = prepared(svc.engine.snapshot)
    svc.maybe_execute(decision)
    assert svc.last_execution["status"] == "execution_rejected"
    # Plain ValueError verdicts publish type + location, never provider bodies.
    assert svc.last_execution["reason"].startswith("ValueError")
    assert "pending_cycle" in svc.last_execution["reason"]
    assert svc.store.data["intent"] is None and not svc.store.data["attempts"]
    # A definite pre-send verdict frees the gate after the short floor only.
    assert svc.next_attempt == svc.clock() + 2
    # Transport failures still propagate for run()'s reset-and-backoff.
    svc.executor.pending_cycle = lambda *a: (_ for _ in ()).throw(
        ConnectionError("provider body")
    )
    svc.next_attempt = 0
    with pytest.raises(ConnectionError):
        svc.maybe_execute(prepared(svc.engine.snapshot, "0x" + "ee" * 32))
    assert "provider body" not in json.dumps(svc.store.data)


def test_mined_confirmation_fallback_still_runs_after_pending_submission(
    tmp_path, snapshot
):
    svc = armed_service(tmp_path, snapshot)
    calls = []

    def pending_cycle(decision, engine_snapshot):
        calls.append("pending")
        return {"kind": "execution", "status": "simulated", "action": "backrun"}

    def backrun(decision, action="backrun"):
        calls.append(("mined", action))
        return {"kind": "execution", "status": "simulated", "action": action}

    svc.executor.pending_cycle = pending_cycle
    svc.executor.backrun = backrun
    svc.maybe_execute(prepared(svc.engine.snapshot))
    # The pending key must not block the distinct mined-confirmation key.
    svc.next_attempt = 0
    svc.maybe_execute(
        {
            "kind": "mined",
            "status": "paper_revalidated",
            "hash": HASH,
            "backrun": {"buy_fee": 500, "sell_fee": 10000},
        }
    )
    assert calls == ["pending", ("mined", "backrun")]
    # With the module disabled or the pending attempt failed pre-send, the
    # mined path continues to work on its own.
    svc.next_attempt = 0
    svc.executor.pending_cycle = lambda *a: pytest.fail("pending re-attempted")
    svc.maybe_execute(
        {
            "kind": "mined",
            "status": "paper_revalidated",
            "hash": "0x" + "ee" * 32,
            "backrun": {"buy_fee": 500, "sell_fee": 10000},
        }
    )
    assert calls[-1] == ("mined", "backrun")


def test_pending_backrun_flag_parses_and_fails_closed():
    assert EventSettings.from_env({}).pending_backrun is False
    for value in ("1", "true", "TRUE", " True "):
        assert EventSettings.from_env({"EVENT_PENDING_BACKRUN": value}).pending_backrun
    for value in ("", "0", "false", None):
        assert not EventSettings.from_env(
            {} if value is None else {"EVENT_PENDING_BACKRUN": value}
        ).pending_backrun
    with pytest.raises(ValueError, match="EVENT_PENDING_BACKRUN"):
        EventSettings.from_env({"EVENT_PENDING_BACKRUN": "yes"})


def test_pending_submission_survives_head_advance(pending_env):
    # 2026-11-20 16:20 regression: the head moved past the pin while the
    # target sat pending and strict pinned==latest equality refused a
    # +21.7 f564 plan 16 ms in. A bounded window (here two blocks) must
    # accept the advanced head, and the deadline must follow the NEWER
    # head timestamp rather than the pinned snapshot.
    f = pending_env
    f.ex.clock = lambda: 110
    # The fee cache bound the fixture's original clock at construction; keep
    # it in sync with the executor clock the test controls.
    f.ex.fees = event_fees.FeeCache(f.ex.w3, f.ex.settings, f.ex.clock)
    original = f.ex.w3.eth.get_block

    def advanced(block):
        if block == "latest":
            f.calls.append(("header", "latest"))
            return {
                "number": 9,
                "hash": bytes.fromhex("ef" * 32),
                "timestamp": 108,
                "baseFeePerGas": 1,
            }
        assert block == 7
        return original(block)

    f.ex.w3.eth.get_block = advanced
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "simulated"
    assert f.built["args"][5] == 108 + 30
    counts = Counter(row[0] for row in f.calls)
    assert counts == {"chain": 1, "header": 2, "balance": 1}


def test_pending_submission_rejects_head_beyond_window(pending_env):
    f = pending_env
    f.ex.clock = lambda: 110
    original = f.ex.w3.eth.get_block

    def beyond(block):
        if block == "latest":
            return {
                "number": 10,
                "hash": bytes.fromhex("ef" * 32),
                "timestamp": 110,
                "baseFeePerGas": 1,
            }
        return original(block)

    f.ex.w3.eth.get_block = beyond
    with pytest.raises(ConfigurationError, match="outside the submission window"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}


def test_pending_submission_rejects_reorganized_pin(pending_env):
    f = pending_env
    f.ex.clock = lambda: 110
    original = f.ex.w3.eth.get_block

    def reorganized(block):
        if block == 7:  # canonical chain no longer carries the pinned hash
            return {
                "number": 7,
                "hash": bytes.fromhex("cd" * 32),
                "timestamp": 95,
                "baseFeePerGas": 1,
            }
        return original(block)

    f.ex.w3.eth.get_block = reorganized
    with pytest.raises(ConfigurationError, match="reorganized"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["intent"] is None


def test_pending_submission_rejects_stale_head(pending_env):
    f = pending_env
    f.ex.clock = lambda: 140  # latest header timestamp 99 is 41 s old

    def stale(block):
        return {
            "number": 7 if block != "latest" else 8,
            "hash": bytes.fromhex("ab" * 32)
            if block != "latest"
            else bytes.fromhex("ef" * 32),
            "timestamp": 99,
            "baseFeePerGas": 1,
        }

    f.ex.w3.eth.get_block = stale
    with pytest.raises(ConfigurationError, match="not fresh"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["intent"] is None
