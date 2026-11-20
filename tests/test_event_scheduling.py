"""Offline scheduling, canonical notification and execution-verdict regressions."""

import json
import queue
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests.exceptions import HTTPError
from test_event_service import service
from web3.exceptions import ContractLogicError, Web3RPCError

import control
import strategy
from event_feed import Feed
from event_service import Service
from legacy_main import Halt

HASH = "0x" + "ab" * 32
OTHER_HASH = "0x" + "cd" * 32


@pytest.fixture
def svc(tmp_path):
    snapshot = json.loads(
        (
            Path(__file__).parents[1]
            / "research/2026-11-17-phase2-arbitrage/snapshot.json"
        ).read_text()
    )
    value = service(tmp_path, snapshot)
    value.refresh()
    value.snapshots.reconciled_hash = snapshot["block_hash"]
    return value


def enable_execution(svc):
    svc.settings = replace(svc.settings, mode="simulate")
    control.save(
        svc.control_file,
        replace(strategy.DEFAULT, event_paused=False, event_start_request=1),
    )
    svc.read_controls()
    svc.executor.backrun = Mock(
        return_value={"kind": "execution", "status": "simulated"}
    )
    return {"status": "paper_revalidated", "backrun": {"buy_fee": 500}, "hash": HASH}


def test_fifo_beyond_old_drain_limit_preserves_arrival_and_generation(svc):
    events = []
    for index in range(96):
        event = {"kind": ("head", "mined", "pending")[index % 3], "index": index}
        svc.feed.put(event, received=index + 100, received_mono=index + 200)
        events.append(
            event
            | {
                "received": index + 100,
                "received_mono": index + 200,
                "generation": svc.feed.generation,
            }
        )
    assert [svc.next_event(0) for _ in events] == events
    with pytest.raises(queue.Empty):
        svc.next_event(0)


def test_bounded_queue_concurrent_refill_never_requeues_or_blocks():
    feed = Feed("unused", [], threading.Event(), capacity=8)
    svc = SimpleNamespace(feed=feed)
    ready = threading.Event()
    finished = threading.Event()
    received, errors = [], []
    total = 512

    def produce():
        try:
            for index in range(total):
                feed.queue.put({"kind": "mined", "index": index}, timeout=2)
                if index == 7:
                    ready.set()
        except (queue.Full, queue.Empty, AssertionError) as exc:
            errors.append(exc)

    def consume():
        try:
            assert ready.wait(2)
            for _ in range(total):
                received.append(Service.next_event(svc, 2)["index"])
        except (queue.Full, queue.Empty, AssertionError) as exc:
            errors.append(exc)
        finally:
            finished.set()

    producer = threading.Thread(target=produce, daemon=True)
    consumer = threading.Thread(target=consume, daemon=True)
    producer.start()
    consumer.start()
    assert finished.wait(5), "bounded queue consumer deadlocked during refill"
    producer.join(2)
    consumer.join(2)
    assert not producer.is_alive() and not consumer.is_alive()
    assert not errors
    assert received == list(range(total))
    assert feed.queue.empty()
    # get() consumes exactly one item; it never creates new queue tasks by re-putting.
    assert feed.queue.unfinished_tasks == total


@pytest.mark.parametrize(
    "removal", [{"kind": "mined", "removed": True}, {"kind": "reorg"}]
)
def test_removal_behind_pending_prevents_planning(svc, removal):
    svc.feed.put({"kind": "pending", "transaction": {}}, svc.clock())
    for index in range(40):
        svc.feed.put({"kind": "mined", "index": index}, svc.clock())
    svc.feed.put(removal, svc.clock())
    svc.maybe_execute = Mock()
    svc.handle(svc.next_event(0))
    assert svc.feed.gap.is_set()
    assert svc.engine.snapshot is None
    svc.handle(svc.next_event(0))
    svc.maybe_execute.assert_not_called()
    assert [svc.next_event(0)["index"] for _ in range(40)] == list(range(40))


def test_removal_arriving_after_dequeue_also_prevents_planning(svc):
    svc.feed.put({"kind": "pending", "transaction": {}}, svc.clock())
    pending = svc.next_event(0)
    svc.feed.put({"kind": "mined", "removed": True}, svc.clock())
    svc.engine.handle = Mock()
    svc.maybe_execute = Mock()
    svc.handle(pending)
    svc.included({"hash": HASH})  # maintain() polls receipts before dequeue.
    svc.engine.handle.assert_not_called()
    svc.maybe_execute.assert_not_called()


def test_head_is_processed_before_later_pending(svc):
    old = svc.engine.snapshot
    newer = old | {"block": old["block"] + 1, "block_hash": OTHER_HASH}
    calls = []

    def refresh():
        calls.append("refresh")
        svc.engine.snapshot = newer

    svc.refresh = refresh
    svc.engine.pending_transactions = list
    svc.cross_pool = lambda: None
    svc.fees.observe = lambda *a, **k: None

    def plan(event):
        assert svc.engine.snapshot is newer
        calls.append(event["kind"])
        return {"kind": "pending", "status": "ignored"}

    svc.engine.handle = plan
    svc.feed.put(
        {"kind": "head", "header": {"number": hex(newer["block"]), "hash": OTHER_HASH}},
        svc.clock(),
    )
    svc.feed.put({"kind": "pending", "transaction": {}}, svc.clock())
    svc.handle(svc.next_event(0))
    svc.handle(svc.next_event(0))
    assert calls == ["refresh", "pending"]


def test_control_refusal_is_recorded_without_reset_or_executor_call(svc):
    decision = enable_execution(svc)
    snapshot = svc.engine.snapshot
    control.save(svc.control_file, replace(strategy.DEFAULT, event_paused=True))
    svc.maybe_execute(decision)
    svc.executor.backrun.assert_not_called()
    assert svc.engine.snapshot is snapshot
    assert svc.last_execution["status"] == "execution_rejected"
    assert svc.next_attempt > svc.clock()


@pytest.mark.parametrize(
    "failure, propagates",
    [
        (ContractLogicError("execution reverted: insufficient profit"), False),
        (HTTPError("private provider body"), True),
        (Web3RPCError("private provider body"), True),
    ],
)
def test_execution_revert_is_a_verdict_but_transport_failure_propagates(
    svc, failure, propagates
):
    decision = enable_execution(svc)
    snapshot = svc.engine.snapshot
    svc.executor.backrun.side_effect = failure
    if propagates:
        with pytest.raises(type(failure)):
            svc.maybe_execute(decision)
    else:
        svc.maybe_execute(decision)
        assert "insufficient profit" in svc.last_execution["reason"]
    assert svc.last_execution["status"] == "execution_rejected"
    assert "private provider body" not in svc.last_execution["reason"]
    assert svc.engine.snapshot is snapshot
    assert svc.next_attempt > svc.clock()


@pytest.mark.parametrize("case", ["control", "revert", "transport"])
def test_run_backs_off_only_for_transport_failures(svc, case):
    decision = enable_execution(svc)
    snapshot = svc.engine.snapshot
    if case == "control":
        control.save(svc.control_file, replace(strategy.DEFAULT, event_paused=True))
    else:
        svc.executor.backrun.side_effect = (
            ContractLogicError("execution reverted: insufficient profit")
            if case == "revert"
            else HTTPError("private provider body")
        )
    stop = threading.Event()
    waits = []

    def backoff(seconds):
        waits.append(seconds)
        stop.set()

    stop.wait = backoff
    svc.maintain = lambda: svc.maybe_execute(decision)
    svc.next_event = lambda timeout: stop.set()
    svc.publish = Mock()
    svc.feed.start = Mock()
    svc.run(stop)
    assert svc.last_execution["status"] == "execution_rejected"
    if case == "transport":
        assert waits == [2]
        assert svc.engine.snapshot is None
    else:
        assert not waits
        assert svc.engine.snapshot is snapshot


def test_unknown_send_halt_is_never_swallowed(svc):
    decision = enable_execution(svc)
    svc.executor.backrun.side_effect = Halt("uncertain send")
    with pytest.raises(Halt, match="uncertain send"):
        svc.maybe_execute(decision)
    assert svc.last_execution is None


@pytest.mark.parametrize("kind", ["head", "mined"])
def test_matching_notification_reuses_only_canonically_verified_snapshot(svc, kind):
    snapshot = svc.engine.snapshot
    block, block_hash = snapshot["block"], snapshot["block_hash"]
    event = (
        {"kind": "head", "header": {"number": hex(block), "hash": block_hash}}
        if kind == "head"
        else {
            "kind": "mined",
            "transaction": {"blockNumber": hex(block), "blockHash": block_hash},
        }
    )
    svc.executor.w3.eth.get_block = Mock(
        return_value={"number": block, "hash": bytes.fromhex(block_hash[2:])}
    )
    svc.refresh = Mock()
    svc.refresh_for(event)
    svc.refresh.assert_not_called()
    svc.executor.w3.eth.get_block.assert_called_once_with("latest")
    assert svc.engine.snapshot is snapshot


@pytest.mark.parametrize(
    "case",
    ["changed_hash", "newer", "unvalidated", "malformed", "stale", "read_failure"],
)
def test_notification_reuse_cannot_bypass_canonical_guards(svc, case):
    snapshot = svc.engine.snapshot
    block, block_hash = snapshot["block"], snapshot["block_hash"]
    event = {"kind": "head", "header": {"number": hex(block), "hash": block_hash}}
    latest = {"number": block, "hash": block_hash}
    if case == "changed_hash":
        latest["hash"] = OTHER_HASH
    elif case == "newer":
        latest["number"] += 1
    elif case == "unvalidated":
        svc.snapshots.reconciled_hash = None
    elif case == "malformed":
        event["header"]["number"] = "not-a-number"
    elif case == "stale":
        svc.clock = lambda: snapshot["timestamp"] + 31
    latest["hash"] = bytes.fromhex(latest["hash"][2:])
    svc.executor.w3.eth.get_block = Mock(return_value=latest)
    svc.refresh = Mock()
    if case == "read_failure":
        svc.executor.w3.eth.get_block.side_effect = HTTPError("private provider body")
        with pytest.raises(HTTPError):
            svc.refresh_for(event)
        svc.refresh.assert_not_called()
    else:
        svc.refresh_for(event)
        svc.refresh.assert_called_once_with()


def receipt_setup(svc, ahead=True):
    snapshot = svc.engine.snapshot
    receipt = {
        "transactionHash": bytes.fromhex(HASH[2:]),
        "from": "0x" + "11" * 20,
        "status": 1,
        "blockNumber": snapshot["block"] - int(ahead),
        "blockHash": bytes.fromhex((HASH if ahead else snapshot["block_hash"])[2:]),
        "transactionIndex": 3,
    }
    svc.executor.w3.eth.get_transaction_receipt = Mock(return_value=receipt)
    svc.executor.w3.eth.get_block = Mock(
        side_effect=[
            {"hash": receipt["blockHash"]},
            {"hash": bytes.fromhex(snapshot["block_hash"][2:])},
        ]
    )
    svc.fees.confirm = Mock()
    svc.engine.handle = Mock(return_value={"status": "paper_revalidated"})
    svc.maybe_execute = Mock()
    return snapshot, receipt


def test_ahead_receipt_supplies_anchor_only_after_both_canonical_reads(svc):
    snapshot, receipt = receipt_setup(svc)
    svc.included({"hash": HASH})
    assert [call.args[0] for call in svc.executor.w3.eth.get_block.call_args_list] == [
        receipt["blockNumber"],
        snapshot["block"],
    ]
    event = svc.engine.handle.call_args.args[0]
    assert event["receipt_anchor"] == {
        "block": receipt["blockNumber"],
        "block_hash": "0x" + receipt["blockHash"].hex(),
        "snapshot_block": snapshot["block"],
        "snapshot_hash": snapshot["block_hash"],
    }
    assert (
        svc.recent[-1]["target_inclusion"]["source"]
        == "receipt_canonical_before_snapshot"
    )
    svc.maybe_execute.assert_called_once()


@pytest.mark.parametrize(
    "failure", ["receipt_hash", "snapshot_hash", "receipt_read", "snapshot_read"]
)
def test_ahead_receipt_mismatch_and_read_failure_never_reach_engine(svc, failure):
    snapshot, receipt = receipt_setup(svc)
    reads = [
        {"hash": receipt["blockHash"]},
        {"hash": bytes.fromhex(snapshot["block_hash"][2:])},
    ]
    index = int(failure.startswith("snapshot"))
    reads[index] = (
        HTTPError("private provider body")
        if failure.endswith("read")
        else {"hash": bytes.fromhex(OTHER_HASH[2:])}
    )
    svc.executor.w3.eth.get_block.side_effect = reads
    with pytest.raises(HTTPError if failure.endswith("read") else ValueError):
        svc.included({"hash": HASH})
    assert svc.executor.w3.eth.get_block.call_count == index + 1
    svc.engine.handle.assert_not_called()
    svc.fees.confirm.assert_not_called()
    svc.maybe_execute.assert_not_called()


def test_same_block_receipt_needs_no_extra_header_reads(svc):
    receipt_setup(svc, ahead=False)
    svc.included({"hash": HASH})
    svc.executor.w3.eth.get_block.assert_not_called()
    assert "receipt_anchor" not in svc.engine.handle.call_args.args[0]
    assert svc.recent[-1]["target_inclusion"]["source"] == "receipt_matching_snapshot"
