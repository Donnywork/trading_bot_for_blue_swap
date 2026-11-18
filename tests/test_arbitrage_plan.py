import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import arbitrage
import v3pool

QUOTE = "0x" + "11" * 20
BASE = "0x" + "22" * 20


@pytest.mark.parametrize("zero_for_one", [True, False])
def test_price_limits_bound_raw_price_with_inward_rounding(zero_for_one):
    sqrt = 2**96
    limit = arbitrage.price_limit(sqrt, zero_for_one, 100)
    if zero_for_one:
        assert limit < sqrt and limit**2 * 10000 >= sqrt**2 * 9900
    else:
        assert limit > sqrt and limit**2 * 10000 <= sqrt**2 * 10100


def test_plan_uses_actual_first_output_and_same_block_for_both_pools():
    calls = []
    header = {"number": 42, "timestamp": 123456, "hash": bytes.fromhex("11" * 32)}
    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_block=lambda n: header, chain_id=11155111)
    )

    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, block_identifier):
            calls.append(("block", block_identifier))
            return self.value

    def quote(params):
        tin, _tout, amount, _fee, limit = params
        calls.append(params)
        out = amount // 1000 if tin == QUOTE else amount * 1020
        return Call((out, (2**96 + limit) // 2, 1, 90000))

    quoter = SimpleNamespace(functions=SimpleNamespace(quoteExactInputSingle=quote))
    tokens = {"f564": (QUOTE, 18, "f564"), "feth": (BASE, 18, "fETH")}

    def market(address, fee):
        pool = SimpleNamespace(
            address=address, functions=SimpleNamespace(slot0=lambda: Call((2**96,)))
        )
        return {"pool": pool, "fee": fee, "tokens": tokens, "quoter": quoter, "w3": w3}

    buy, sell = market("0x" + "33" * 20, 500), market("0x" + "44" * 20, 10000)
    result = arbitrage.quote_cycle(buy, sell, 1000 * 10**18, 10**18, 10 * 10**18)
    assert result["profit"] == 20 * 10**18
    assert result["deadline"] == 123516
    assert [r[1] for r in calls if r[0] == "block"] == [42] * 4
    quotes = [r for r in calls if r[0] != "block"]
    assert quotes[1][:4] == (BASE, QUOTE, 10**18, 10000)
    assert result["min_quote_out"] >= result["amount_in"] + result["min_profit"]
    with pytest.raises(ValueError, match="profit"):
        arbitrage.quote_cycle(buy, sell, 1000 * 10**18, 21 * 10**18, 10 * 10**18)
    with pytest.raises(ValueError, match="cap"):
        arbitrage.quote_cycle(buy, sell, 1000 * 10**18, 10**18, 1)
    with pytest.raises(ValueError, match="distinct"):
        arbitrage.quote_cycle(buy, buy, 1000 * 10**18, 10**18, 10**18)

    def partial(params):
        return Call((10**18, params[-1], 1, 90000))

    quoter.functions.quoteExactInputSingle = partial
    with pytest.raises(ValueError, match="price limit"):
        arbitrage.quote_cycle(buy, sell, 1000 * 10**18, 10**18, 10 * 10**18)


def test_saved_live_quotes_match_reflected_tick_math_across_range_boundaries():
    path = (
        Path(__file__).parents[1] / "research/2026-11-17-phase2-arbitrage/snapshot.json"
    )
    saved = json.loads(path.read_text())
    profiles = {p["fee"]: p for p in saved["profiles"]}
    for row in saved["cycles"]:
        assert "error" not in row
        buy, sell = profiles[row["buy_fee"]], profiles[row["sell_fee"]]
        base = v3pool.V3Pool(buy, buy["price_f564"]).buy_with_quote(row["input_f564"])
        out = v3pool.V3Pool(sell, sell["price_f564"]).sell_base(base)
        assert out == pytest.approx(int(row["quote_raw"]) / 1e18, abs=1e-6, rel=0)
    assert any(sum(row["ticks_crossed"]) > 0 for row in saved["cycles"])
