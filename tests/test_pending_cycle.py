"""Same-block pending atomic-cycle submission (EVENT_PENDING_BACKRUN)."""

import copy
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_event_service import HASH, A, candidate, fake_executor, service
from web3.exceptions import TimeExhausted

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


class FakeHash(bytes):
    def to_0x_hex(self):
        return "0x" + bytes(self).hex()


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

        def send_raw_transaction(self, raw):
            calls.append(("send_raw",))
            raws.append(raw)
            return FakeHash(bytes.fromhex("cd" * 32))

        def wait_for_transaction_receipt(self, tx_hash, timeout=None):
            calls.append(("wait_receipt", timeout))
            if not receipts:
                raise TimeExhausted
            return receipts.pop(0)

    ex.w3.eth = Eth()

    def read_batch(batch):
        # One batched round trip servicing the five hot-path calls from the
        # same fakes above, so per-method monkeypatches still apply and the
        # individual ("chain"/"header"/...) tracking entries stay meaningful.
        calls.append(("read_batch", tuple(method for method, _ in batch)))
        out = []
        for method, params in batch:
            if method == "eth_chainId":
                out.append(hex(ex.w3.eth.chain_id))
            elif method == "eth_getBlockByNumber":
                raw = ex.w3.eth.get_block(
                    "latest" if params[0] == "latest" else int(params[0], 16)
                )
                out.append(
                    {
                        "number": hex(raw["number"]),
                        "hash": "0x" + bytes(raw["hash"]).hex(),
                        "timestamp": hex(raw["timestamp"]),
                        "baseFeePerGas": hex(raw["baseFeePerGas"]),
                    }
                )
            elif method == "eth_getBalance":
                out.append(hex(ex.w3.eth.get_balance(params[0])))
            elif method == "eth_getTransactionCount":
                out.append(hex(ex.w3.eth.get_transaction_count(*params)))
            else:
                raise AssertionError(f"unexpected read-batch method {method}")
        return out

    ex.w3.provider = SimpleNamespace(read_batch=read_batch)
    ex.fees = event_fees.FeeCache(ex.w3, ex.settings, ex.clock)
    built = {}
    raws, receipts = [], []

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
    # Read budget: ONE batched round trip carries chain + latest head +
    # canonical pinned block + balance + pending nonce (the batch rides the
    # same fakes, so their tracking entries appear too); perform() reuses
    # the pending header instead of re-reading freshness, and simulate
    # still sends nothing.
    counts = Counter(row[0] for row in f.calls)
    assert counts == {
        "read_batch": 1,
        "chain": 1,
        "header": 2,
        "balance": 1,
        "nonce": 1,
    }


def test_presend_refusal_cancels_the_sweep_too(pending_env):
    # 2026-11-22 restructure: BOTH transactions validate before either is
    # durable or broadcast. A same-block refusal (deadline too tight here)
    # must leave no open intent, no attempt key and nothing in flight —
    # previously the sweep was already broadcast and had to be settled.
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)
    f.ex.account = SimpleNamespace(
        address=f.ex.operator,
        sign_transaction=lambda tx: SimpleNamespace(raw_transaction=b"sweep"),
    )
    f.ex.clock = lambda: 120  # freshness still passes (21s), deadline does not
    with pytest.raises(ValueError, match="deadline"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert f.ex.store.data["attempts"] == {}
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["intent_extra"] is None
    assert f.raws == []


def test_sweep_sign_failure_falls_back_to_plain_nonce(pending_env, monkeypatch):
    # The same-block cycle takes nonce N (not N+1) when the sweep could not
    # be signed: a parked N+1 would wait on a nonce that never fills.
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)

    def boom(tx):
        raise TypeError("signer unavailable")

    f.ex.account = SimpleNamespace(address=f.ex.operator, sign_transaction=boom)
    primary = {}

    def send(*args, **kwargs):
        primary.update(args[2])
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "not_sent"
    assert primary["nonce"] == 1  # the batched pending nonce, unshifted
    assert f.raws == []
    assert "backrun_sweep:" + HASH not in f.ex.store.data["attempts"]


def test_pending_cycle_live_never_estimates_never_doubles_the_override(
    pending_env, monkeypatch
):
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=False)
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
    assert counts == {
        "read_batch": 1,
        "chain": 1,
        "header": 2,
        "balance": 1,
        "nonce": 1,
        "build": 1,
    }
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {"backrun_pending:" + HASH: "not_sent"}


def test_pending_cycle_sends_pending_tip_and_keeps_max_fee(pending_env, monkeypatch):
    # Block-ordering fix (2026-11-21): the market bot's targets pay 0 gwei and
    # builders sort by tip descending, so the configured 10 gwei tip placed
    # the pending cycle at block 12178067 index 1 while the 0-gwei target sat
    # at index 135 - a guaranteed pre-target revert. The pending submission
    # must carry the pending tip (default 0) while the max fee stays at the
    # decided ceiling; every other send path keeps the configured priority.
    f = pending_env
    f.ex.settings = replace(
        f.ex.settings,
        mode="live",
        pending_sweep=False,
        priority_wei=10_000_000_000,
        pending_priority_wei=0,
    )
    sends = []

    def send(*args, **kwargs):
        sends.append(args[2])
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "not_sent"
    assert sends[0]["maxPriorityFeePerGas"] == 0
    assert sends[0]["maxFeePerGas"] == f.ex.settings.max_fee_wei
    assert result["fee_decision"]["selected_priority_wei"] == 0
    assert result["fee_decision"]["selected_max_fee_wei"] == f.ex.settings.max_fee_wei
    assert result["fee_decision"]["pending_tip"] is True


def test_pending_cycle_honors_configured_pending_tip(pending_env, monkeypatch):
    f = pending_env
    f.ex.settings = replace(
        f.ex.settings,
        mode="live",
        pending_sweep=False,
        pending_priority_wei=1_500_000_000,
    )
    sends = []

    def send(*args, **kwargs):
        sends.append(args[2])
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "not_sent"
    assert sends[0]["maxPriorityFeePerGas"] == 1_500_000_000
    assert result["fee_decision"]["selected_priority_wei"] == 1_500_000_000


def test_dual_submission_sends_sweep_then_same_block(pending_env, monkeypatch):
    # Competitor replication: nonce N carries the 16 gwei next-block sweep
    # (fired first, raw), nonce N+1 carries the same-block cycle through the
    # normal perform path. Both attempt keys settle; the primary keeps the
    # pending tip and its own nonce.
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)
    f.ex.account = SimpleNamespace(
        address=f.ex.operator,
        sign_transaction=lambda tx: SimpleNamespace(raw_transaction=b"sweep"),
    )
    primary = {}

    def send(*args, **kwargs):
        primary.update(args[2])
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    f.receipts.append(
        {
            "status": 0,
            "blockNumber": 8,
            "blockHash": bytes.fromhex("ef" * 32),
            "transactionHash": bytes.fromhex("cd" * 32),
            "gasUsed": 700_000,
            "effectiveGasPrice": 1,
            "transactionIndex": 4,
        }
    )
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "not_sent"
    assert f.raws == [b"sweep"]  # exactly one raw sweep broadcast
    assert primary["nonce"] == 2  # get_transaction_count returned 1; primary takes N+1
    assert primary["maxPriorityFeePerGas"] == f.ex.settings.pending_priority_wei
    attempts = f.ex.store.data["attempts"]
    assert attempts["backrun_pending:" + HASH] == "not_sent"
    assert attempts["backrun_sweep:" + HASH] == "reverted"
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["intent_extra"] is None
    sweep_row = next(t for t in f.ex.store.data["trades"] if t.get("sweep"))
    assert sweep_row["status"] == "reverted"
    assert sweep_row["block"] == 8
    assert sweep_row["reason"] == "next-block sweep"


def test_dual_submission_confirmed_sweep_records_profit(pending_env, monkeypatch):
    import event_execution

    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)
    f.ex.account = SimpleNamespace(
        address=f.ex.operator,
        sign_transaction=lambda tx: SimpleNamespace(raw_transaction=b"sweep"),
    )

    def send(*args, **kwargs):
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    f.receipts.append(
        {
            "status": 1,
            "blockNumber": 9,
            "blockHash": bytes.fromhex("ee" * 32),
            "transactionHash": bytes.fromhex("cd" * 32),
            "gasUsed": 600_000,
            "effectiveGasPrice": 2,
            "transactionIndex": 0,
        }
    )
    rung = sizes_near(f.ex.settings.quote_cap, 100 * A)[0][0]
    sweep_received = rung + f.ex.settings.min_profit + 2 * A
    monkeypatch.setattr(
        event_execution, "received_from_receipt", lambda r, t, a: sweep_received
    )
    f.ex.pending_cycle(f.decision, f.pinned)
    sweep_row = next(t for t in f.ex.store.data["trades"] if t.get("sweep"))
    assert sweep_row["status"] == "confirmed"
    assert sweep_row["received_raw"] == sweep_received
    assert sweep_row["transaction_index"] == 0


def test_sweep_unsettled_when_no_receipt_arrives(pending_env, monkeypatch):
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)
    f.ex.account = SimpleNamespace(
        address=f.ex.operator,
        sign_transaction=lambda tx: SimpleNamespace(raw_transaction=b"sweep"),
    )

    def send(*args, **kwargs):
        raise bot_contract.NotSent("refused before broadcast")

    monkeypatch.setattr(bot_contract, "send_tx", send)
    # no receipt queued: the bounded wait exhausts and records "unsettled"
    result = f.ex.pending_cycle(f.decision, f.pinned)
    assert result["status"] == "not_sent"
    assert f.ex.store.data["attempts"]["backrun_sweep:" + HASH] == "unsettled"
    assert f.ex.store.data["intent_extra"] is None


def test_dual_affordability_covers_two_cycles(pending_env, monkeypatch):
    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)
    f.ex.w3.eth.get_balance = lambda _: 1  # far below 2 * max_gas * ceiling
    with pytest.raises(ValueError, match="two-cycle"):
        f.ex.pending_cycle(f.decision, f.pinned)
    assert "backrun_sweep:" + HASH not in f.ex.store.data["attempts"]
    assert f.raws == []


def test_sweep_uncertain_broadcast_halts(pending_env, monkeypatch):
    from legacy_main import Halt

    f = pending_env
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=True)
    f.ex.account = SimpleNamespace(
        address=f.ex.operator,
        sign_transaction=lambda tx: SimpleNamespace(raw_transaction=b"sweep"),
    )
    f.ex.w3.eth.send_raw_transaction = lambda raw: (_ for _ in ()).throw(
        RuntimeError("transport died")
    )
    with pytest.raises(Halt):
        f.ex.pending_cycle(f.decision, f.pinned)
    # the durable sweep intent blocks any restart until reconciled
    assert f.ex.store.data["intent_extra"]["key"] == "backrun_sweep:" + HASH
    assert f.ex.store.data["attempts"]["backrun_sweep:" + HASH] == "inflight"


def test_open_intent_extra_halts_reload(pending_env):
    import event_store as store_module
    from legacy_main import Halt

    f = pending_env
    f.ex.store.begin_extra("backrun_sweep:" + HASH, {"action": "backrun"})
    with pytest.raises(Halt, match="unfinished execution intent"):
        store_module.EventStore(
            f.ex.store.directory,
            f.ex.store.data["identity"],
            "simulate",
            clock=lambda: 100,
        )


def test_pending_sweep_settings_parse():
    # Default OFF since 2026-11-22 (0-for-4 live: every firing landed index 0
    # BEFORE the target in the target's own block — pure gas); explicit
    # opt-in restores the dual submission.
    assert EventSettings.from_env({}).pending_sweep is False
    assert EventSettings.from_env({"EVENT_PENDING_SWEEP": "true"}).pending_sweep is True
    assert (
        EventSettings.from_env({"EVENT_PENDING_SWEEP": "false"}).pending_sweep is False
    )
    tuned = EventSettings.from_env({"EVENT_SWEEP_PRIORITY_FEE_WEI": "17000000000"})
    assert tuned.sweep_priority_wei == 17_000_000_000
    with pytest.raises(ValueError, match="EVENT_SWEEP_PRIORITY_FEE_WEI"):
        EventSettings.from_env({"EVENT_SWEEP_PRIORITY_FEE_WEI": "0"})
    with pytest.raises(ValueError, match="sweep priority fee exceeds"):
        EventSettings.from_env({"EVENT_SWEEP_PRIORITY_FEE_WEI": "50000000000"})


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
    f.ex.settings = replace(f.ex.settings, mode="live", pending_sweep=False)
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
    # Armed by default: the contested windows are lost without it, so a bare
    # deployment must race; an explicit 0/false remains the working opt-out.
    assert EventSettings.from_env({}).pending_backrun is True
    assert EventSettings.from_env({"EVENT_PENDING_BACKRUN": ""}).pending_backrun is True
    for value in ("1", "true", "TRUE", " True "):
        assert EventSettings.from_env({"EVENT_PENDING_BACKRUN": value}).pending_backrun
    for value in ("0", "false"):
        assert not EventSettings.from_env(
            {"EVENT_PENDING_BACKRUN": value}
        ).pending_backrun
    with pytest.raises(ValueError, match="EVENT_PENDING_BACKRUN"):
        EventSettings.from_env({"EVENT_PENDING_BACKRUN": "yes"})


def test_pending_priority_fee_parses_and_validates():
    # Default 1 wei, one BELOW the target's tip=2 group (2026-11-23): at
    # 53-85 ms broadcasts a matching tip landed the cycle BEFORE the target
    # itself (block 12193519 idx 210 vs the target's 212 - pre-target
    # revert), while competitor-2 won the 07:30 window with tip 1 directly
    # behind the target. A tip below the group can never sort pre-target;
    # tip 0 stays the old zero-tail failure.
    assert EventSettings.from_env({}).pending_priority_wei == 1
    env = {"EVENT_PENDING_PRIORITY_FEE_WEI": "3000000000"}
    assert EventSettings.from_env(env).pending_priority_wei == 3_000_000_000
    # An explicit 0 stays a valid opt-back-out to the zero-tail group.
    assert (
        EventSettings.from_env(
            {"EVENT_PENDING_PRIORITY_FEE_WEI": "0"}
        ).pending_priority_wei
        == 0
    )
    # Other send paths keep the configured priority fee untouched.
    assert EventSettings.from_env(env).priority_wei == 2_000_000_000
    with pytest.raises(ValueError, match="EVENT_PENDING_PRIORITY_FEE_WEI"):
        EventSettings.from_env({"EVENT_PENDING_PRIORITY_FEE_WEI": "1.5"})
    with pytest.raises(ValueError, match="EVENT_PENDING_PRIORITY_FEE_WEI"):
        EventSettings.from_env({"EVENT_PENDING_PRIORITY_FEE_WEI": "-1"})
    with pytest.raises(ValueError, match="pending priority fee exceeds"):
        EventSettings.from_env({"EVENT_PENDING_PRIORITY_FEE_WEI": "50000000000"})


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
    assert counts == {
        "read_batch": 1,
        "chain": 1,
        "header": 2,
        "balance": 1,
        "nonce": 1,
    }


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
