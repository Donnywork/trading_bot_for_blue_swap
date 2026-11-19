import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from web3 import Web3

import backrun
import event_market as market
import frontrun
from event_engine import PaperEngine
from event_replay import replay

ROOT = Path(__file__).parents[1]
A = 10**18
COMPETITOR = "0xd067640b2e032b1a878bd87b4d85cb5a87ef64ef"
LIMITS = {"max_quote_raw": 1000 * A, "max_base_raw": 10 * A, "min_profit_raw": A // 100}


@pytest.fixture
def snapshot():
    return json.loads(
        (ROOT / "research/2026-11-17-phase2-arbitrage/snapshot.json").read_text()
    )


def transaction(snapshot, **changes):
    params = (
        Web3.to_checksum_address(market.QUOTE),
        Web3.to_checksum_address(market.BASE),
        10000,
        Web3.to_checksum_address(market.MARKET_BOT),
        5000 * A,
        1,
        0,
    )
    data = market.router().encode_abi("exactInputSingle", args=[params])
    wrapped = market.router().encode_abi(
        "multicall(uint256,bytes[])", args=[snapshot["timestamp"] + 120, [data]]
    )
    return {
        "hash": "0x" + "ab" * 32,
        "from": market.MARKET_BOT,
        "nonce": "0x1",
        "chainId": "0xaa36a7",
        "to": market.ROUTER,
        "value": "0x0",
        "input": wrapped,
        **changes,
    }


def engine(snapshot):
    e = PaperEngine(LIMITS, competitors=[COMPETITOR])
    assert (
        e.handle(
            {"kind": "snapshot", "snapshot": snapshot, "now": snapshot["timestamp"]}
        )["status"]
        == "snapshot_loaded"
    )
    return e


def after_snapshot(snapshot, pools):
    result = copy.deepcopy(snapshot)
    result.update(
        block=snapshot["block"] + 1,
        block_hash="0x" + "cd" * 32,
        timestamp=snapshot["timestamp"] + 12,
    )
    for p in result["profiles"]:
        pool = pools[p["fee"]]
        p["sqrtPriceX96"] = int(2**96 / pool.s)
        p["tick"] = math.floor(math.log(1 / pool.s**2) / math.log(1.0001))
        p["liquidity"] = sum(t["net"] for t in p["ticks"] if t["tick"] <= p["tick"])
        p["block"] = result["block"]
    return result


def receipt(tx, snapshot, status=1):
    return {
        "transactionHash": tx["hash"],
        "from": tx["from"],
        "status": hex(status),
        "blockNumber": hex(snapshot["block"]),
        "blockHash": snapshot["block_hash"],
    }


def test_decode_recorded_market_bot_pending_transaction():
    path = (
        ROOT
        / "research/2026-11-17-phase2-flow/pending_observations-20261117T223346035819Z.jsonl"
    )
    row = json.loads(path.read_text().splitlines()[0])
    swap = market.decode_swap(row["transaction"], 1794954900)
    assert (swap["side"], swap["fee"], swap["amount_in"]) == ("buy", 10000, 4433 * A)
    assert swap["minimum_out"] == 4182728171617000199


@pytest.mark.parametrize(
    "changes",
    [
        {"chainId": "0x1"},
        {"to": market.BASE},
        {"value": "0x1"},
        {"blockNumber": "0x1"},
        {"input": "0x12345678"},
    ],
)
def test_decoder_refuses_unsupported_semantics(snapshot, changes):
    with pytest.raises(ValueError):
        market.decode_swap(transaction(snapshot, **changes), snapshot["timestamp"] + 1)


def test_rejection_explains_local_reason_and_never_exposes_external_exception(
    snapshot, monkeypatch
):
    e = engine(snapshot)
    now = snapshot["timestamp"] + 1
    result = e.handle(
        {
            "kind": "pending",
            "now": now,
            "transaction": transaction(snapshot, to=market.BASE),
        }
    )
    assert result["status"] == "rejected"
    assert result["stage"] == "target decoding"
    assert result["reason"] == "unsupported destination or native value"

    def fail(*args):
        raise ValueError("https://provider.invalid/private-placeholder")

    monkeypatch.setattr(market, "decode_swap", fail)
    result = engine(snapshot).handle(
        {"kind": "pending", "now": now, "transaction": transaction(snapshot)}
    )
    assert "ValueError" in result["reason"]
    assert "private-placeholder" not in json.dumps(result)


def test_multicall_multiple_swaps_and_expiry_are_rejected(snapshot):
    tx = transaction(snapshot)
    _, args = market.router().decode_function_input(tx["input"])
    tx["input"] = market.router().encode_abi(
        "multicall(uint256,bytes[])", args=[args["deadline"], args["data"] * 2]
    )
    with pytest.raises(ValueError, match="multicall"):
        market.decode_swap(tx, snapshot["timestamp"])
    with pytest.raises(ValueError, match="expired"):
        market.decode_swap(transaction(snapshot), snapshot["timestamp"] + 121)


@pytest.mark.parametrize(
    "damage",
    ["wrong_market", "wrong_block", "negative_liquidity", "stale", "duplicate_ticks"],
)
def test_snapshot_fails_closed(snapshot, damage):
    now = snapshot["timestamp"]
    p = snapshot["profiles"][0]
    if damage == "wrong_market":
        p["pool_address"] = market.BASE
    elif damage == "wrong_block":
        p["block"] -= 1
    elif damage == "negative_liquidity":
        p["ticks"][0]["net"] *= -1
    elif damage == "duplicate_ticks":
        p["ticks"].append(p["ticks"][0].copy())
    else:
        now += 31
    with pytest.raises(ValueError):
        market.models(snapshot, now)


def test_snapshot_cycle_matches_recorded_quoter_outputs(snapshot):
    pools = market.models(snapshot, snapshot["timestamp"])
    for row in snapshot["cycles"]:
        result = backrun.cycle(pools, row["buy_fee"], row["input_f564"], 100)
        assert result["output_f564"] == pytest.approx(
            int(row["quote_raw"]) / A, abs=1e-6, rel=0
        )
    assert backrun.best_cycle(pools, LIMITS) is None


@pytest.mark.parametrize(
    "low,high",
    [(0.30, 0.32), (0.45, 0.47), (0.62, 0.66), (0.71, 0.73), (0.09, 0.11)],
)
def test_optimize_finds_narrow_profit_bands_a_plain_ternary_search_walks_past(
    low, high
):
    # Lumpy liquidity (discrete positions at discrete ticks) can leave a ~2%-of-range
    # profitable band. A pure ternary search missed four of these five windows in
    # development; the coarse grid sweep plus bracket refinement must catch each.

    def evaluate(size):
        if low < size < high:
            return {"profit_f564": 1.0 - abs(size - (low + high) / 2)}
        raise ValueError("infeasible")

    row = backrun.optimize(evaluate, 1.0)
    assert row and row["profit_f564"] > 0.98


def test_both_strategies_include_impact_and_preserve_canonical_pools(snapshot):
    now = snapshot["timestamp"]
    pools = market.models(snapshot, now)
    initial = copy.deepcopy(pools)
    swap = market.decode_swap(transaction(snapshot), now)
    b = backrun.prepare(pools, swap, LIMITS)
    f = frontrun.prepare(pools, swap, LIMITS)
    assert b["profit_f564"] > 0 and f["profit_f564"] > 0
    assert b["buy_fee"] == 500 and b["sell_fee"] == 10000
    assert f["target_contribution_f564"] > 0
    assert f["assumed_order"] == ["our_buy", "target_buy", "our_sell"]
    assert b["input_f564"] <= 1000 and f["input_f564"] <= 1000
    for fee in pools:
        assert vars(pools[fee]) == vars(initial[fee])


def test_front_run_respects_target_minimum_and_base_cap(snapshot):
    pools = market.models(snapshot, snapshot["timestamp"])
    swap = market.decode_swap(transaction(snapshot), snapshot["timestamp"])
    _, original_output = market.apply_target(pools, swap)
    tight = swap | {"minimum_out": int(original_output * A * 0.999999)}
    with pytest.raises(ValueError, match="minimum"):
        frontrun.evaluate(pools, tight, 1000, 10000, 10000, 10)
    with pytest.raises(ValueError, match="cap"):
        frontrun.evaluate(pools, swap, 1000, 500, 10000, 0.001)
    assert frontrun.prepare(pools, swap | {"side": "sell"}, LIMITS) is None


def test_pending_replacement_even_unsupported_cancels_old_candidate(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    assert (
        e.handle({"kind": "pending", "transaction": tx, "now": now + 1})["status"]
        == "prepared"
    )
    assert (
        e.handle({"kind": "pending", "transaction": tx, "now": now + 2})["status"]
        == "duplicate"
    )
    replacement = transaction(snapshot, hash="0x" + "ef" * 32, input="0x")
    assert (
        e.handle({"kind": "pending", "transaction": replacement, "now": now + 3})[
            "status"
        ]
        == "rejected"
    )
    assert e.candidates[tx["hash"]]["status"] == "superseded"
    assert e.candidates[tx["hash"]]["backrun"] is None
    # Late rebroadcast of the original doesn't resurrect its prediction.
    assert (
        e.handle({"kind": "pending", "transaction": tx, "now": now + 4})["status"]
        == "duplicate"
    )


def test_competitor_and_expiry_clear_preparations(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    e.handle(
        {
            "kind": "pending",
            "transaction": transaction(snapshot, **{"from": COMPETITOR}),
            "now": now + 2,
        }
    )
    assert e.candidates[tx["hash"]]["status"] == "stale"
    assert e.candidates[tx["hash"]]["frontrun"] is None
    e.handle({"kind": "tick", "now": now + 62})
    assert e.candidates[tx["hash"]]["status"] == "expired"
    assert e.pending_transactions() == []
    e.handle({"kind": "tick", "now": now + 182})
    assert not e.candidates
    # Pruning the displayed candidate does not forget an unresolved earlier nonce.
    assert (market.MARKET_BOT, 1) in e.nonces


def test_engine_keeps_replaced_target_for_receipt_recovery(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    replacement = transaction(snapshot, hash="0x" + "ef" * 32)
    e.handle({"kind": "pending", "transaction": replacement, "now": now + 2})
    assert len(e.pending_transactions()) == 2
    # Callers cannot mutate the engine's saved transaction through a returned view.
    e.pending_transactions()[0]["nonce"] = "0xff"
    assert e.pending_transactions()[0]["nonce"] == tx["nonce"]
    result = e.handle(
        {
            "kind": "mined",
            "transaction": tx,
            "receipt": receipt(tx, snapshot),
            "now": now + 3,
        }
    )
    assert result["status"] == "no_opportunity"
    assert e.candidates[replacement["hash"]]["status"] == "invalidated"
    assert e.pending_transactions() == []


@pytest.mark.parametrize("kind", ["reorg", "removed", "bad_snapshot"])
def test_engine_reset_discards_receipt_work_and_nonce_settlement(snapshot, kind):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    e.settle_nonces(market.MARKET_BOT, 2)
    assert e.settled_nonces
    if kind == "removed":
        event = {"kind": "mined", "transaction": tx, "removed": True}
    elif kind == "bad_snapshot":
        event = {"kind": "snapshot", "snapshot": {}}
    else:
        event = {"kind": "reorg"}
    e.handle(event | {"now": now + 2})
    assert e.snapshot is None and not e.settled_nonces
    assert not e.pending_transactions()
    assert e.candidates[tx["hash"]]["status"] == "invalidated"


def test_canonical_nonce_read_unblocks_later_pending_target(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    e.handle(
        {
            "kind": "pending",
            "transaction": transaction(snapshot, input="0x"),
            "now": now + 1,
        }
    )
    e.settle_nonces(market.MARKET_BOT, 2)
    later = transaction(snapshot, nonce="0x2", hash="0x" + "ef" * 32)
    assert (
        e.handle({"kind": "pending", "transaction": later, "now": now + 2})["status"]
        == "prepared"
    )


def test_nonce_history_compaction_invalidates_predictions(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    # Simulate a long-running feed without thousands of expensive paper quotes.
    e.nonces.update({(market.MARKET_BOT, n): hex(n) for n in range(4097)})
    e.seen.update(hex(n) for n in range(4097))
    e.handle({"kind": "tick", "now": now + 2})
    assert not e.nonces and not e.settled_nonces and not e.pending_transactions()
    assert e.candidates[tx["hash"]]["status"] == "invalidated"
    assert e.seen == {tx["hash"]}


def test_mined_notice_requires_receipt_and_fresh_exact_block_snapshot(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    result = e.handle({"kind": "mined", "transaction": tx, "now": now + 12})
    assert result["status"] == "awaiting_snapshot" and result["backrun"] is None
    after, _ = market.apply_target(
        market.models(snapshot, now), market.decode_swap(tx, now)
    )
    fresh = after_snapshot(snapshot, after)
    result = e.handle(
        {
            "kind": "mined",
            "transaction": tx,
            "receipt": receipt(tx, fresh),
            "now": now + 13,
        }
    )
    assert result["status"] == "awaiting_snapshot"
    e.handle({"kind": "snapshot", "snapshot": fresh, "now": now + 13})
    result = e.handle(
        {
            "kind": "mined",
            "transaction": tx,
            "receipt": receipt(tx, fresh),
            "now": now + 14,
        }
    )
    assert result["status"] == "paper_revalidated"
    expected = backrun.best_cycle(market.models(fresh, now + 14), LIMITS)
    assert result["backrun"] == expected
    assert result["frontrun"] is None
    assert result["backrun"]["exact_validation_required"]


def test_competitor_consumed_edge_revalidated_as_no_opportunity(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    # Fresh end-of-block state has returned to the original unprofitable spread.
    fresh = after_snapshot(snapshot, market.models(snapshot, now))
    e.handle({"kind": "snapshot", "snapshot": fresh, "now": now + 12})
    result = e.handle(
        {
            "kind": "mined",
            "transaction": tx,
            "receipt": receipt(tx, fresh),
            "now": now + 13,
        }
    )
    assert result["status"] == "no_opportunity" and result["backrun"] is None


def test_revert_receipt_and_reorg_never_leave_a_ready_plan(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    result = e.handle(
        {
            "kind": "mined",
            "transaction": tx,
            "receipt": receipt(tx, snapshot, 0),
            "now": now + 2,
        }
    )
    assert result["status"] == "reverted" and e.snapshot is None
    e = engine(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    e.handle({"kind": "mined", "transaction": tx, "removed": True, "now": now + 2})
    assert e.candidates[tx["hash"]]["status"] == "invalidated"
    assert e.snapshot is None


def test_earlier_unsupported_nonce_prevents_unsequenced_prediction(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    e.handle(
        {
            "kind": "pending",
            "transaction": transaction(snapshot, input="0x"),
            "now": now + 1,
        }
    )
    later = transaction(snapshot, nonce="0x2", hash="0x" + "ef" * 32)
    assert (
        e.handle({"kind": "pending", "transaction": later, "now": now + 2})["status"]
        == "rejected"
    )


def test_replay_does_not_manufacture_snapshot_from_observer_notifications(snapshot):
    tx = transaction(snapshot)
    decisions = list(
        replay(
            PaperEngine(LIMITS),
            [
                {"kind": "pending", "transaction": tx, "now": snapshot["timestamp"]},
            ],
        )
    )
    assert decisions[0]["status"] == "rejected"


def test_receipt_hash_mismatch_never_revalidates(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    bad = receipt(tx, snapshot) | {"transactionHash": "0x" + "ff" * 32}
    result = e.handle(
        {"kind": "mined", "transaction": tx, "receipt": bad, "now": now + 2}
    )
    assert result["status"] == "awaiting_snapshot" and result["backrun"] is None
    assert not e.settled_nonces


def test_same_hash_cannot_change_nonce_during_inclusion(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    result = e.handle(
        {
            "kind": "mined",
            "transaction": tx | {"nonce": "0x2"},
            "receipt": receipt(tx, snapshot),
            "now": now + 2,
        }
    )
    assert result["status"] == "rejected" and not e.settled_nonces


def test_bad_snapshot_invalidates_instead_of_preserving_old_state(snapshot):
    e, now = engine(snapshot), snapshot["timestamp"]
    tx = transaction(snapshot)
    e.handle({"kind": "pending", "transaction": tx, "now": now + 1})
    bad = copy.deepcopy(snapshot)
    bad["profiles"][0]["tick"] += 20
    assert (
        e.handle({"kind": "snapshot", "snapshot": bad, "now": now + 2})["status"]
        == "rejected"
    )
    assert e.snapshot is None and e.candidates[tx["hash"]]["status"] == "invalidated"


def test_backrun_respects_both_caps(snapshot):
    now = snapshot["timestamp"]
    pools, _ = market.apply_target(
        market.models(snapshot, now), market.decode_swap(transaction(snapshot), now)
    )
    limits = LIMITS | {"max_base_raw": A // 100, "min_profit_raw": 1}
    result = backrun.best_cycle(pools, limits)
    assert result and result["base_feth"] <= 0.01
    assert result["input_f564"] < 1000


def test_cli_offline_journal_and_no_overwrite(snapshot, tmp_path):
    events = tmp_path / "events.jsonl"
    journal = tmp_path / "journal.jsonl"
    events.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {
                    "kind": "snapshot",
                    "snapshot": snapshot,
                    "now": snapshot["timestamp"],
                },
                {
                    "kind": "pending",
                    "transaction": transaction(snapshot),
                    "now": snapshot["timestamp"] + 1,
                },
            ]
        )
        + "\n"
    )
    command = [
        sys.executable,
        str(ROOT / "event_replay.py"),
        "--events",
        str(events),
        "--output",
        str(journal),
        "--quote-cap",
        "1000",
        "--min-profit",
        "0.01",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "no transactions submitted" in result.stdout
    saved = journal.read_text()
    decisions = [json.loads(line) for line in saved.splitlines()]
    assert (
        decisions[-1]["status"] == "prepared"
        and decisions[-1]["backrun"]["profit_f564"] > 0
    )
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    assert journal.read_text() == saved
