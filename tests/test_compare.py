import itertools

import pytest

import compare
import strategy
import v3pool
from strategy import Config

# a pool where one bot sells (-4%) and buys (+4%) in turn, a sample every 30 s with a few quiet
# ones between: the shape the strategies were built for
NO_GUARD = Config(
    strategy="sharp_drop", guard_flip_pct=0, guard_drift_pct=0, take_profit_pct=3.0
)


def sawtooth(cycles, quiet=10, start=1000.0):
    points, price, ts = [], start, 1_000_000
    for _ in range(cycles):
        for factor in (1 / 1.04, 1.04):
            price *= factor
            for _ in range(quiet):
                points.append((ts, price))
                ts += 30
    return points


def test_a_pattern_market_makes_the_dip_buyers_win():
    points = sawtooth(10)
    row = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=1000)
    assert row["trades"] >= 5 and row["wins"] == row["trades"]
    assert row["profit"] > 0 and row["worst_drawdown"] == 0


def test_the_round_trip_cost_is_taken_off_every_trade():
    points = sawtooth(4)
    row = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=1000)
    # a +4% step minus the 0.75% a round trip costs, per 1000 spent
    per_trade = 1000 * (1.04 * (1 - strategy.FRICTION_PCT / 100) - 1)
    assert row["profit"] / row["trades"] == pytest.approx(per_trade, rel=0.02)


DEPTH = v3pool.load_profile()


def test_with_the_pool_depth_a_big_trade_pays_what_its_size_really_costs():
    points = sawtooth(4)
    fixed = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=15000)
    exact = compare.replay(
        points, NO_GUARD, points[0][0], cooldown=0, size=15000, depth=DEPTH
    )
    assert exact["trades"] == fixed["trades"] > 0
    assert exact["gross"] == pytest.approx(fixed["gross"])  # the price moved the same
    # a buy of 15,000 costs about 2.4% for the round trip, not 0.75%
    assert exact["profit"] < fixed["profit"] - 150 * exact["trades"]
    assert exact["impact"] > 3 * fixed["impact"]
    assert exact["fees"] == pytest.approx(
        fixed["fees"], rel=0.1
    )  # the 0.3% fee is the same
    assert exact["profit"] == pytest.approx(
        exact["gross"] - exact["fees"] - exact["impact"]
    )


def test_with_the_pool_depth_a_small_trade_costs_about_what_the_fixed_cost_says():
    points = sawtooth(4)
    fixed = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=1000)
    exact = compare.replay(
        points, NO_GUARD, points[0][0], cooldown=0, size=1000, depth=DEPTH
    )
    assert exact["profit"] == pytest.approx(fixed["profit"], rel=0.05)


def test_an_open_position_is_valued_with_its_real_exit_cost_too():
    points = sawtooth(2)[:-3]
    cfg = Config(
        strategy="sharp_drop",
        guard_flip_pct=0,
        guard_drift_pct=0,
        take_profit_pct=100.0,
    )
    fixed = compare.replay(points, cfg, points[0][0], cooldown=0, size=15000)
    exact = compare.replay(
        points, cfg, points[0][0], cooldown=0, size=15000, depth=DEPTH
    )
    assert exact["holding"] and exact["open"] < fixed["open"]


def test_the_round_trip_cost_grows_with_the_size_and_is_the_fixed_one_without_depth():
    point = [(1, 1043.0)]
    small = compare.friction_pct(DEPTH, point, 1000)
    big = compare.friction_pct(DEPTH, point, 14000)
    assert small == pytest.approx(0.73, abs=0.02)
    assert big == pytest.approx(2.26, abs=0.05)
    assert compare.friction_pct(None, point, 14000) == strategy.FRICTION_PCT
    assert compare.friction_pct(DEPTH, [], 14000) == strategy.FRICTION_PCT


def test_the_warnings_use_the_cost_of_the_size_when_the_depth_is_known():
    points = sawtooth(4)

    def first_note(depth):
        rows = compare.compare(points, NO_GUARD, points[0][0], 0, 14000, None, depth)
        return next(r for r in rows if r["strategy"] == "sharp_drop")["warnings"][0]

    assert "about 0.15% price impact" in first_note(None)["text"]
    assert "about 0.15% price impact" not in first_note(DEPTH)["text"]


def test_prices_before_the_start_only_warm_the_history_up():
    points = sawtooth(10)
    late = points[len(points) // 2][0]
    whole = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=1000)
    half = compare.replay(points, NO_GUARD, late, cooldown=0, size=1000)
    assert 0 < half["trades"] < whole["trades"]


def test_the_cooldown_holds_back_a_second_entry():
    points = sawtooth(10)
    fast = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=1000)
    slow = compare.replay(points, NO_GUARD, points[0][0], cooldown=10**9, size=1000)
    assert slow["trades"] == 1 < fast["trades"]


def test_a_position_still_open_is_valued_at_the_last_price():
    points = sawtooth(2)[:-3]  # ends just after a buy leg began? at least holding
    cfg = Config(
        strategy="sharp_drop",
        guard_flip_pct=0,
        guard_drift_pct=0,
        take_profit_pct=100.0,
    )
    row = compare.replay(points, cfg, points[0][0], cooldown=0, size=1000)
    assert row["holding"] and row["trades"] == 0
    assert row["open"] == pytest.approx(
        1000 * (points[-1][1] / entry_of(points, cfg) * 0.9925 - 1), rel=0.01
    )


def entry_of(points, cfg):
    # the price the replay bought at: the first sample after the first big drop
    for (_, a), (_, b) in itertools.pairwise(points):
        if b < a * 0.97:
            return b
    raise AssertionError("no drop in the test prices")


def test_the_time_stop_closes_a_flat_position_in_the_replay():
    quiet = [(1_000_000 + 30 * i, 1000.0) for i in range(40)]
    drop = [(1_000_000 + 30 * 40, 950.0)]  # a sharp drop: buys at 950
    flat = [(1_000_000 + 30 * (41 + i), 951.0) for i in range(60)]  # 30 minutes
    points = quiet + drop + flat
    off = Config(
        strategy="sharp_drop", guard_flip_pct=0, guard_drift_pct=0, take_profit_pct=50
    )
    on = Config(
        strategy="sharp_drop",
        guard_flip_pct=0,
        guard_drift_pct=0,
        take_profit_pct=50,
        time_stop_on=True,
        time_stop_min=10,
    )
    assert compare.replay(points, off, 0, 0, 1000)["trades"] == 0
    row = compare.replay(points, on, 0, 0, 1000)
    assert row["trades"] == 1 and row["reasons"]["time_stop"] == 1


def test_compare_gives_one_row_per_strategy_in_declaration_order():
    rows = compare.compare(sawtooth(5), NO_GUARD, 0, 0, 1000)
    assert [r["strategy"] for r in rows] == list(strategy.STRATEGIES)
    assert all({"label", "trades", "profit", "open"} <= set(r) for r in rows)


def test_a_single_price_makes_no_trade():
    row = compare.replay([(1, 1000.0)], NO_GUARD, 0, 0, 1000)
    assert row["trades"] == 0 and not row["holding"] and row["profit"] == 0


# ---------------------------------------------------------------- cost breakdown and warnings


def test_the_result_adds_up_after_the_pool_fee_and_the_price_impact():
    points = sawtooth(10)
    row = compare.replay(points, NO_GUARD, points[0][0], cooldown=0, size=1000)
    assert row["fees"] > 0 and row["impact"] > 0
    assert row["gross"] - row["fees"] - row["impact"] == pytest.approx(row["profit"])
    # the pool's fee is 0.3% a leg and the allowance for impact 0.075%: four to one
    assert row["fees"] / row["impact"] == pytest.approx(
        strategy.POOL_FEE_PCT / strategy.IMPACT_PCT
    )
    # about 0.75% of the money in every round trip
    assert (row["fees"] + row["impact"]) / row["trades"] == pytest.approx(
        1000 * strategy.FRICTION_PCT / 100, rel=0.06
    )


def texts(items, level=None):
    return [w["text"] for w in items if level is None or w["level"] == level]


def advice_for(cfg, **result):
    base = {
        "trades": 10,
        "wins": 9,
        "profit": 100.0,
        "open": 0.0,
        "win_hold_p90": None,
        "reasons": {"take_profit": 9, "stop_loss": 1, "time_stop": 0},
    }
    return compare.advice(cfg, base | result, STEPS)


STEPS = {
    "count": 20,
    "median": 3.6,
    "up_median": 3.8,
    "up_p90": 4.8,
    "down_median": 3.5,
    "p90": 4.8,
}


def test_advice_always_says_what_a_win_and_a_loss_net_after_costs():
    (first, *_) = advice_for(Config(take_profit_pct=3.0, stop_loss_pct=4.0))
    assert "+2.25%" in first["text"] and "-4.75%" in first["text"]
    assert "68%" in first["text"]  # 4.75 / (2.25 + 4.75) of the trades must win


def test_advice_warns_when_the_win_rate_is_near_break_even():
    weak = advice_for(Config(guard_flip_pct=0, guard_drift_pct=0), wins=7)
    assert any("won 70%" in t for t in texts(weak, "warn"))
    strong = advice_for(Config(guard_flip_pct=0, guard_drift_pct=0), wins=9)
    assert not any("won 90%" in t for t in texts(strong, "warn"))


def test_advice_warns_about_a_take_profit_the_costs_would_eat():
    warned = advice_for(Config(take_profit_pct=1.2))
    assert any("close to the costs" in t for t in texts(warned, "warn"))


def test_advice_warns_about_a_stop_loss_smaller_than_the_pools_jumps():
    tight = advice_for(Config(stop_loss_pct=2.0))
    assert any("single adverse jump" in t for t in texts(tight, "warn"))
    assert not any(
        "single adverse jump" in t for t in texts(advice_for(Config(stop_loss_pct=4.0)))
    )


def test_advice_warns_about_a_take_profit_the_pool_rarely_reaches():
    far = advice_for(Config(take_profit_pct=9.0))
    assert any("rarely be reached" in t for t in texts(far, "warn"))


def test_advice_warns_when_the_time_stop_would_cut_winners():
    cfg = Config(time_stop_on=True, time_stop_min=5)
    assert any("cuts winners" in t for t in texts(advice_for(cfg, win_hold_p90=600)))
    fine = Config(time_stop_on=True, time_stop_min=30)
    assert not any(
        "cuts winners" in t for t in texts(advice_for(fine, win_hold_p90=600))
    )
    off = Config(time_stop_on=False)
    assert not any("time stop" in t for t in texts(advice_for(off, win_hold_p90=600)))


def test_advice_warns_when_the_guard_is_off_or_very_strict():
    off = advice_for(Config(guard_flip_pct=0, guard_drift_pct=0))
    assert any("guard is off" in t for t in texts(off, "warn"))
    strict = advice_for(Config(guard_flip_pct=90))
    assert any("at most one break" in t for t in texts(strict, "warn"))
    assert not any(
        "guard" in t for t in texts(advice_for(Config(guard_flip_pct=60)), "warn")
    )


def test_advice_checks_each_strategys_own_numbers():
    noisy = Config(
        strategy="mean_reversion",
        params={"mean_reversion": {"threshold_pct": 1.0, "window": 6}},
    )
    got = texts(advice_for(noisy), "warn")
    assert any("within twice the costs" in t for t in got)
    assert any("window of 6" in t for t in got)
    small = Config(strategy="sharp_drop", params={"sharp_drop": {"drop_pct": 1.5}})
    assert any("also buys on noise" in t for t in texts(advice_for(small), "warn"))
    huge = Config(strategy="sharp_drop", params={"sharp_drop": {"drop_pct": 8.0}})
    assert any("rarely buy" in t for t in texts(advice_for(huge), "warn"))
    trend = Config(strategy="breakout")
    assert any("Trend following" in t for t in texts(advice_for(trend), "info"))


def test_advice_says_when_the_replay_loses_or_never_trades():
    losing = advice_for(Config(), profit=-80.0)
    assert any("lose 80.00" in t for t in texts(losing, "warn"))
    idle = advice_for(Config(), trades=0, wins=0, profit=0.0)
    assert any("No trade" in t for t in texts(idle, "info"))


def test_step_stats_measures_the_pools_jumps_and_needs_enough_of_them():
    stats = compare.step_stats(sawtooth(10))
    assert stats["count"] == 19  # the first jump has no sample before it
    assert stats["median"] == pytest.approx(4.0, abs=0.2)
    assert compare.step_stats(sawtooth(1)) is None
    assert compare.step_stats([(i, 100.0) for i in range(50)]) is None


def test_compare_rows_carry_their_warnings():
    rows = compare.compare(
        sawtooth(5), NO_GUARD, 0, 0, 1000, compare.step_stats(sawtooth(5))
    )
    for row in rows:
        # the first thing every strategy is told is what its exits net after the costs
        assert "break even" in row["warnings"][0]["text"]
        assert row["strategy"] in {
            "mean_reversion",
            "bollinger",
            "rsi",
            "sharp_drop",
            "breakout",
            "ma_cross",
        }
    assert any(
        "Trend following" in w["text"]
        for r in rows
        if r["strategy"] == "breakout"
        for w in r["warnings"]
    )


# ---------------------------------------------------------------- gaps in the recorded prices


def test_the_replay_forgets_its_history_across_a_gap_like_the_bot_does():
    # 25 flat samples, then a 10% drop: a mean-reversion buy, if the bot still has its history
    flat = [(1_000_000 + 30 * i, 1000.0) for i in range(25)]
    cfg = Config(guard_flip_pct=0, guard_drift_pct=0, take_profit_pct=50.0)
    next_ts = flat[-1][0]
    continuous = flat + [(next_ts + 30, 900.0)]
    after_gap = flat + [(next_ts + 3600, 900.0)]
    assert compare.replay(continuous, cfg, 0, 0, 1000)["holding"] is True
    assert compare.replay(after_gap, cfg, 0, 0, 1000)["holding"] is False


def test_a_jump_across_a_gap_is_not_a_step():
    # ten 4% jumps, each one over an hour of silence: nothing a bot ever saw as one step
    points, price, ts = [], 1000.0, 1_000_000
    for i in range(10):
        points.append((ts, price))
        price *= 1.04 if i % 2 == 0 else 1 / 1.04
        ts += 3600
    assert compare.step_stats(points) is None


def test_a_take_profit_below_the_costs_is_the_only_thing_said():
    out = compare.advice(
        Config(take_profit_pct=0.5),
        {
            "trades": 3,
            "wins": 1,
            "profit": 1.0,
            "open": 0.0,
            "win_hold_p90": None,
            "reasons": {"take_profit": 1, "stop_loss": 0, "time_stop": 0},
        },
        None,
    )
    assert [w["level"] for w in out] == ["warn"] and "cover the costs" in out[0]["text"]
