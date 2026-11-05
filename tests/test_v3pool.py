import json
from decimal import Decimal

import pytest

import strategy
import v3pool

PROFILE = v3pool.load_profile()
PRICE = 1043.0  # inside the saved liquidity layout


def round_trip_pct(quote, price=PRICE):
    # buy and sell at the same price: what the pool fee and our own price impact cost
    return v3pool.loss_at_stop(PROFILE, price, 0, quote) / quote * 100


# ---------------------------------------------------------------- the maths


def test_the_saved_layout_loads_and_is_for_the_fee_tier_the_maths_assumes():
    assert PROFILE["fee"] == 3000
    assert PROFILE["d0"] == PROFILE["d1"]
    assert PROFILE["ticks"]


def test_a_layout_for_another_fee_tier_is_refused(tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({**PROFILE, "fee": 500}))
    with pytest.raises(ValueError, match="does not match"):
        v3pool.load_profile(other)


@pytest.mark.parametrize(
    "ticks",
    [
        None,
        "x",
        [1],
        [{"tick": 1}],
        [{"tick": "1", "net": 5}],
        [{"tick": 1, "net": 0.5}],
    ],
)
def test_a_layout_with_damaged_ticks_is_refused_not_left_to_crash_a_trade(
    tmp_path, ticks
):
    damaged = tmp_path / "damaged.json"
    damaged.write_text(json.dumps({**PROFILE, "ticks": ticks}))
    with pytest.raises(ValueError, match="malformed"):
        v3pool.load_profile(damaged)
    # the scanned file with the right keys but damaged ticks is ignored for the shipped layout
    assert v3pool.current_profile(damaged) == PROFILE


def test_a_missing_layout_is_an_error_not_a_default(tmp_path):
    with pytest.raises(OSError):
        v3pool.load_profile(tmp_path / "missing.json")


def test_small_buys_cost_about_what_the_live_trades_cost():
    # the live trades of 2026-11-04 (about 1000 f564 each) paid 0.64 to 0.78% for the round trip
    assert 0.64 <= round_trip_pct(1000) <= 0.78


def test_the_cost_per_unit_grows_with_the_size():
    costs = [round_trip_pct(q) for q in (1000, 5000, 10000, 14000, 20000)]
    assert costs == sorted(costs)
    # the exact table of the research: 0.731 at 1000 and 2.256 at 14000
    assert costs[0] == pytest.approx(0.731, abs=0.01)
    assert costs[3] == pytest.approx(2.256, abs=0.02)


def test_a_stop_loss_costs_the_price_move_plus_the_round_trip_costs():
    loss = v3pool.loss_at_stop(PROFILE, PRICE, 5, 1000)
    assert (
        50 < loss < 50 + 10
    )  # 5% of 1000 from the price alone, a little over 1% on top


def test_a_buy_the_pool_cannot_absorb_is_an_error():
    # one liquidity position from tick 69000 to 70000 and nothing above it
    one_range = {
        **PROFILE,
        "ticks": [
            {"tick": 69000, "net": 10**22},
            {"tick": 70000, "net": -(10**22)},
        ],
    }
    assert v3pool.loss_at_stop(one_range, PRICE, 4, 1000) > 0
    with pytest.raises(ValueError, match="no liquidity"):
        v3pool.loss_at_stop(one_range, PRICE, 4, 10**9)


# ---------------------------------------------------------------- sizing on it


def exact_loss(quote, stop=5.0, price=PRICE):
    return v3pool.loss_at_stop(PROFILE, price, stop, quote)


def sized(equity, ceiling, risk, stop=5.0, price=PRICE):
    def loss_at_stop(quote):
        return exact_loss(quote, stop, price)

    return strategy.trade_size(
        Decimal(equity), Decimal(ceiling), Decimal(risk), stop, loss_at_stop
    )


def test_the_buy_is_the_biggest_one_whose_stop_loss_fits_the_budget():
    size = sized(40000, 10**6, 2)
    budget = 40000 * 0.02
    assert exact_loss(float(size)) <= budget
    assert exact_loss(float(size) + 1) > budget


def test_a_big_vault_gets_a_smaller_buy_than_the_fixed_cost_would_give():
    fixed = strategy.trade_size(Decimal(40000), Decimal(10**6), Decimal(2), 5.0)
    assert fixed == Decimal("13913.043478")  # 800 / (5% + 0.75%)
    assert sized(40000, 10**6, 2) < fixed - 1000


def test_a_small_vault_gets_about_what_the_fixed_cost_gives():
    # small buys are where the 0.75% was measured
    fixed = strategy.trade_size(Decimal(1386), Decimal(10**6), Decimal(2), 4.0)
    exact = sized(1386, 10**6, 2, stop=4.0)
    assert float(exact) == pytest.approx(float(fixed), rel=0.03)


def test_the_ceiling_and_the_vault_still_cap_the_buy():
    assert sized(40000, 3000, 2) == Decimal(3000)
    assert sized(50, 10**6, 20) == Decimal("50.000000")


def test_the_size_never_loses_more_than_the_budget_on_a_stop_loss_at_any_price():
    for price in (990.0, 1043.0, 1100.0):
        size = sized(40000, 10**6, 2, price=price)
        assert exact_loss(float(size), price=price) <= 800


def test_a_stand_in_loss_function_is_searched_exactly():
    # loss = 10% of the buy, budget 1% of 1000 = 10 -> a buy of 100
    size = strategy.trade_size(
        Decimal(1000), Decimal(500), Decimal(1), 4.0, lambda quote: quote * 0.1
    )
    assert Decimal("99.999") < size <= Decimal(100)


def test_a_pool_that_cannot_take_a_size_limits_the_buy_to_what_it_can():
    def loss_at_stop(quote):
        if quote > 50:
            raise ValueError("no liquidity left")
        return 0.0

    size = strategy.trade_size(
        Decimal(1000), Decimal(500), Decimal(5), 4.0, loss_at_stop
    )
    assert Decimal("49.999") < size <= Decimal(50)


# ---------------------------------------------------------------- the round trip


def test_a_round_trip_at_the_same_price_is_what_a_stop_loss_of_zero_loses():
    back = v3pool.round_trip(PROFILE, PRICE, PRICE, 10000)
    assert 10000 - back == pytest.approx(v3pool.loss_at_stop(PROFILE, PRICE, 0, 10000))


def test_selling_higher_brings_back_more_and_lower_less():
    up = v3pool.round_trip(PROFILE, PRICE, PRICE * 1.03, 10000)
    down = v3pool.round_trip(PROFILE, PRICE, PRICE * 0.97, 10000)
    assert down < v3pool.round_trip(PROFILE, PRICE, PRICE, 10000) < up


# ---------------------------------------------------------------- how deep the pool is now

# the price the saved layout was read at, where its in-range liquidity is the pool's own figure
SAVED_PRICE = 1.0001 ** PROFILE["tick"]


def test_a_pool_as_deep_as_saved_has_a_scale_of_one():
    scale = v3pool.depth_scale(PROFILE, SAVED_PRICE, PROFILE["liquidity"])
    assert scale == pytest.approx(1.0, rel=1e-6)


def test_the_scale_follows_the_liquidity_the_pool_reports():
    half = v3pool.depth_scale(PROFILE, SAVED_PRICE, PROFILE["liquidity"] // 2)
    assert half == pytest.approx(0.5, rel=1e-6)
    assert v3pool.depth_scale(PROFILE, SAVED_PRICE, 0) == 0


def test_the_scale_compares_like_with_like_at_another_price():
    # the saved layout holds different liquidity at 1000 than at the price it was read at, and
    # a pool that is exactly as saved reports exactly that: scale 1
    raw = v3pool.V3Pool(PROFILE, 1000.0).L * 10 ** PROFILE["d0"]
    assert v3pool.depth_scale(PROFILE, 1000.0, raw) == pytest.approx(1.0)


def test_a_thinner_pool_costs_more_and_a_deeper_one_less():
    base = round_trip_pct(10000)
    thin = v3pool.loss_at_stop(v3pool.scaled(PROFILE, 0.5), PRICE, 0, 10000) / 100
    deep = v3pool.loss_at_stop(v3pool.scaled(PROFILE, 2.0), PRICE, 0, 10000) / 100
    assert deep < base < thin
    # the pool fee (0.6%) is the same whatever the depth: only the impact changes
    assert thin - base > 0.5 and base - deep > 0.3


def test_scaling_by_one_changes_nothing():
    same = v3pool.scaled(PROFILE, 1.0)
    assert v3pool.loss_at_stop(same, PRICE, 5, 10000) == pytest.approx(
        v3pool.loss_at_stop(PROFILE, PRICE, 5, 10000)
    )


def test_a_pool_that_is_half_as_deep_gets_a_smaller_buy():
    def sized_on(profile):
        return strategy.trade_size(
            Decimal(40000),
            Decimal(10**6),
            Decimal(2),
            5.0,
            lambda quote: v3pool.loss_at_stop(profile, PRICE, 5.0, quote),
        )

    assert sized_on(v3pool.scaled(PROFILE, 0.5)) < sized_on(PROFILE)
