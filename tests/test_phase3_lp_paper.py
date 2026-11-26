import json
import time

import pytest

import phase3_lp_paper as lp
from phase3_monitor import Journal


def scenario():
    price = lp.root(-69080)
    raw = int(price * 2**96)
    net = {lp.FULL[0]: 20 * lp.WAD, lp.FULL[1]: -20 * lp.WAD}
    start = {
        "version": 1,
        "block": 100,
        "hash": "a",
        "timestamp": 42,
        "pool": lp.POOL,
        "nft": lp.NFT,
        "band": list(lp.BAND),
        "liquidity_raw": 2 * lp.WAD,
        "sqrt_x96": raw,
        "tick": -69080,
        "protocol": 0,
        "net": net,
    }
    market = {
        "block": 100,
        "tick": -69080,
        "liquidity": 20 * lp.WAD,
        "spot": float(1 / price**2),
        "twap": 1000,
        "twap_seconds": 1800,
        "liquidity_net_by_tick": net,
        "borrower_health_wad": {},
    }
    return start, market


def test_crossing_fee_share_splits_path_and_marks_inventory_at_twap():
    start, m = scenario()
    base = lp.evaluate(start, [], m)
    assert abs(base["split_minus_full_f564"]) < 1e-16
    p = lp.D(start["sqrt_x96"]) / 2**96
    raw = int(lp.root(-68900) * 2**96)
    end = lp.D(raw) / 2**96
    border = lp.root(lp.BAND[1])
    r = {
        "event": "Swap",
        "signer": "0xabc",
        "args": {
            lp.SQRT_FIELD: raw,
            "tick": -68900,
            "amount0": -3000 * lp.WAD,
            "amount1": 3 * lp.WAD,
            "liquidity": 20 * lp.WAD,
        },
    }
    m.update(block=101, tick=-68900, spot=float(1 / end**2), twap=970)
    result = lp.evaluate(start, [r], m)
    old, kept, narrow, _ = lp.allocation(start)
    inside = (border - p) / (end - p)
    fraction = inside * (kept + narrow) / (20 - old + kept + narrow) + (
        1 - inside
    ) * kept / (20 - old + kept)
    assert result["full_fees_f564"] == pytest.approx(0.9 * 970 / 1000)
    assert result["split_fees_f564"] == pytest.approx(float(fraction) * 0.009 * 970)
    assert result["inventory_delta_f564"] < 0 and not result["in_narrow_range"]
    assert result["edge_excluding_top_participant_fees_f564"] == pytest.approx(
        result["inventory_delta_f564"]
    )
    assert result["confirmed_sales"] == 1 and result["forward_swaps"] == 1
    with pytest.raises(ValueError, match="liquidity mismatch"):
        r["args"]["liquidity"] += 1
        lp.evaluate(start, [r], m)


def test_forward_start_replay_and_reorg(tmp_path, monkeypatch):
    start, m = scenario()
    db = Journal(tmp_path / "events.sqlite3")
    db.commit(99, "past", [{"event": "Swap", "block": 99, "index": 1}])
    db.commit(100, "a", [])

    def publish(block, block_hash):
        m["block"] = block
        state = {
            "health": "ready",
            "phase": 3,
            "pool": lp.POOL,
            "updated": int(time.time()),
            "head": block + 2,
            "indexed_block": block,
            "indexed_hash": block_hash,
            "market": m,
        }
        (tmp_path / "state.json").write_text(json.dumps(state))

    monkeypatch.setattr(lp, "begin", lambda *args: start)
    publish(100, "a")
    assert lp.update(tmp_path, True, object())["forward_swaps"] == 0
    r = {
        "event": "Swap",
        "block": 102,
        "contract": "pool",
        "index": 1,
        "signer": "0xaaa",
        "args": {
            lp.SQRT_FIELD: start["sqrt_x96"],
            "tick": start["tick"],
            "amount0": 12,
            "amount1": 0,
            "liquidity": m["liquidity"],
        },
    }
    db.commit(102, "b", [r])
    publish(102, "b")
    res = lp.update(tmp_path)
    assert res["forward_swaps"] == 1 and lp.update(tmp_path) == res
    db.rewind(101)
    db.commit(102, "c", [])
    publish(102, "c")
    assert lp.update(tmp_path)["forward_swaps"] == 0
    db.rewind(100)
    db.commit(100, "orphan", [])
    publish(100, "orphan")
    with pytest.raises(ValueError, match="start block moved"):
        lp.update(tmp_path)


def test_sale_to_entry_crosses_a_liquidity_boundary_and_includes_fee():
    p = lp.root(-69090)
    end = lp.root(-68930)
    mid = lp.root(-69000)
    net = {-69120: 300 * lp.WAD, -69000: -200 * lp.WAD, -68880: -100 * lp.WAD}
    mark = lp.D(10000) / ((10000 - lp.ENTRY_DISCOUNT_BPS) * end**2)
    expected = (300 * (mid - p) + 100 * (end - mid)) / (1 - lp.FRACTION)
    assert lp.sale_to_entry(p, mark, net) == pytest.approx(float(expected))
    assert lp.sale_to_entry(end * 2, mark, net) == 0
    assert lp.sale_to_entry(p, mark / 2, net) is None


def test_flash_donations_net_of_protocol_fees_do_not_count_as_spot_signals():
    start, market = scenario()
    rows = [
        {
            "event": "SetFeeProtocol",
            "args": {"feeProtocol0New": 4, "feeProtocol1New": 0},
        },
        {
            "event": "Flash",
            "signer": "0xabc",
            "args": {"paid0": 4 * lp.WAD, "paid1": lp.WAD},
        },
    ]
    out = lp.evaluate(start, rows, market)
    assert out["full_fees_f564"] == pytest.approx((3 + 1000) * 0.1)
    assert out["forward_flashes"] == 1 and out["distinct_swap_signers"] == 0
    assert out["edge_excluding_top_participant_fees_f564"] == pytest.approx(0)
