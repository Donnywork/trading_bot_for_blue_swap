import pytest

from phase3_chain import SQRT_FIELD, WAD
from phase3_monitor import Journal, notices, risk_levels, sync
from phase3_notices import digest
from phase_markets import PHASE3, PHASE3_START


def lp(block, amount, kind="Mint"):
    return {
        "block": block,
        "index": 0,
        "event": kind,
        "contract": "pool",
        "args": {"amount": amount, "tickLower": -60, "tickUpper": 60},
        "signer": "0x1",
        "tx": "0xaa",
    }


class Fork:
    def __init__(self):
        self.fork = False
        self.base = PHASE3_START

    def header(self, block):
        return (
            block,
            str(block) + ("b" if self.fork and block > self.base else "a"),
            1000,
        )

    def events_in(self, start, end):
        rows = [
            lp(self.base, 2**90),
            lp(self.base + 1, 888 if self.fork else 555, "Burn"),
        ]
        return [r for r in rows if start <= r["block"] <= end]


def test_restart_and_reorg_rebuild_canonical_liquidity(tmp_path):
    path = tmp_path / "events.sqlite3"
    journal, chain = Journal(path), Fork()
    sync(chain, journal, chain.base)
    sync(chain, journal, chain.base + 1)
    journal.db.close()
    journal = Journal(path)
    chain.fork = True
    new, changed = sync(chain, journal, chain.base + 2)
    assert changed and new == [lp(chain.base + 1, 888, "Burn")]
    assert journal.depth(0, 2**90 - 888)[60] == -(2**90 - 888)
    assert sync(chain, journal, chain.base + 2) == ([], False)
    with pytest.raises(ValueError, match="incomplete"):
        journal.depth(60, 1)


def test_moving_anchor_discards_entire_chunk(tmp_path):
    journal, chain = Journal(tmp_path / "events.sqlite3"), Fork()

    def reorganized(start, end):
        chain.fork = True
        return [lp(end, 99)]

    chain.events_in = reorganized
    with pytest.raises(ValueError, match="moved"):
        sync(chain, journal, chain.base + 1)
    assert journal.tip() is None
    assert journal.depth(0, 0) == {}


def test_new_signers_and_boundary_health():
    approval = {
        "block": 2,
        "contract": "f564",
        "event": "Approval",
        "args": {
            "spender": PHASE3.credit,
            "owner": "0x123456789000000000000000000000000000abcd",
            "value": 500 * WAD,
        },
    }
    text = next(notices([approval]))
    assert "500 f564" in text and "0x1234…abcd" in text
    assert risk_levels(
        {"a": WAD - 1, "b": WAD, "c": 11 * WAD // 10, "d": 2**256 - 1}
    ) == {"a": "liquidatable", "b": "near liquidation"}


@pytest.mark.parametrize("buy", [True, False])
def test_swap_alert_uses_trader_direction_tokens_and_clickable_receipt(buy):
    tx = "0x" + "ae" * 32
    row = {
        "contract": "pool",
        "event": "Swap",
        "tx": tx,
        "signer": PHASE3.market_bot,
        "args": {
            "amount0": 84 * WAD * (1 if buy else -1),
            "amount1": 84 * WAD // 1000 * (-1 if buy else 1),
            SQRT_FIELD: 2**96,
        },
    }
    line = next(notices([row]))
    assert (
        "Market bot bought 0.084 fETH for 84 f564"
        if buy
        else "Market bot sold 0.084 fETH for 84 f564"
    ) in line
    assert ("0.252 f564" if buy else "0.000252 fETH") in line
    assert "[View transaction](<https://sepolia.etherscan.io/tx/" + tx + ">)" in line
    batch = digest([line] * 20)
    assert len("[FIN564 Phase 3] " + batch) < 2000 and "more events" in batch


def test_lp_alert_converts_reversed_ticks_and_ignores_accounting_burn():
    row = lp(1, 1)
    row["args"].update(tickLower=-69060, tickUpper=-69000, amount0=700 * WAD, amount1=0)
    line = next(notices([row]))
    assert "700 f564 and 0 fETH" in line and "991.93–997.90" in line
    row["event"] = "Burn"
    row["args"]["amount"] = 0
    assert list(notices([row])) == []
