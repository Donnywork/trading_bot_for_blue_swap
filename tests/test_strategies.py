from decimal import Decimal

import pytest

import strategy
from strategy import STRATEGIES, Config, ConfigError, Position, decide, parse_config

FLAT = [100.0] * 30
HAS_QUOTE = Position(base=0.0, quote=1.0)


def cfg(name, **params):
    # the pattern guard is tested on its own below: these tests are about the entry rules
    return Config(
        strategy=name,
        params={name: params} if params else {},
        guard_flip_pct=0,
        guard_drift_pct=0,
    )


def buys(name, prices, **params):
    return decide(prices, HAS_QUOTE, cfg(name, **params)) == "buy"


# ---------------------------------------------------------------- entry rules


def test_mean_reversion_buys_a_dip_below_the_threshold():
    assert buys("mean_reversion", FLAT[:-1] + [90.0])
    assert not buys(
        "mean_reversion", FLAT[:-1] + [98.5]
    )  # 1.4% down: inside the 2% default
    assert buys("mean_reversion", FLAT[:-1] + [98.5], threshold_pct=1.0)


def test_mean_reversion_waits_for_a_full_window():
    assert not buys("mean_reversion", [100.0] * 18 + [90.0])


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_no_strategy_buys_on_a_flat_market_or_without_history(name):
    assert not buys(name, FLAT)
    assert not buys(name, [])
    assert not buys(name, [100.0])


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_no_strategy_buys_without_quote(name):
    falling = [100.0 - i for i in range(30)]
    assert decide(falling, Position(base=1.0, quote=0.0), cfg(name)) == "hold"


def test_bollinger_buys_below_the_band_and_ignores_a_dip_inside_fees():
    # calm market, then a 5% drop: far outside two standard deviations
    assert buys("bollinger", FLAT[:-1] + [95.0])
    # a jumpy market: the same 5% dip is inside the band, so no buy
    jumpy = [100.0, 108.0, 92.0, 106.0, 94.0] * 4
    assert not buys("bollinger", jumpy[:-1] + [95.0])
    # a dip under 1% is never a signal, however calm the market
    assert not buys("bollinger", FLAT[:-1] + [99.5], deviations=0.5)


def test_rsi_buys_after_a_run_of_falls():
    falling = [100.0 - 0.5 * i for i in range(20)]  # steadily down, ~9.5%
    assert buys("rsi", falling)
    rising = [100.0 + 0.5 * i for i in range(20)]
    assert not buys("rsi", rising)


def test_rsi_ignores_oversold_readings_that_are_just_noise():
    # every move is down, but the whole fall is 0.1%: smaller than fees
    tiny = [100.0 - 0.0001 * i for i in range(20)]
    assert not buys("rsi", tiny)


def test_sharp_drop_buys_after_one_big_step_down_and_ignores_slow_drift():
    assert buys("sharp_drop", FLAT[:-1] + [96.5])  # -3.5% in one step
    assert not buys("sharp_drop", FLAT[:-1] + [98.0])  # under the 3% default
    drift = [
        100.0 - 0.2 * i for i in range(30)
    ]  # -5.8% in total, never 3% within 4 samples
    assert not buys("sharp_drop", drift)


def test_sharp_drop_only_looks_at_the_last_few_samples():
    old_high = [110.0] + [100.0] * 29  # the high is older than the look-back
    assert not buys("sharp_drop", old_high, lookback=4)
    assert buys("sharp_drop", old_high, lookback=29)


def test_sharp_drop_waits_for_enough_samples():
    assert not buys("sharp_drop", [100.0, 90.0, 80.0, 70.0])  # needs lookback + 1 = 5


def test_breakout_buys_above_the_recent_high_only():
    assert buys("breakout", FLAT[:-1] + [101.5])
    assert not buys("breakout", FLAT[:-1] + [100.5])  # under the 1% buffer
    assert not buys("breakout", FLAT[:-1] + [90.0])


def test_ma_cross_buys_on_the_cross_not_while_already_above():
    down_then_up = [100.0] * 20 + [
        99.0,
        98.0,
        97.0,
        96.0,
        100.0,
        105.0,
        110.0,
        115.0,
        120.0,
    ]
    crossings = [
        i
        for i in range(21, len(down_then_up) + 1)
        if buys("ma_cross", down_then_up[:i])
    ]
    assert len(crossings) == 1  # exactly one tick triggers, then it is already above
    assert not buys("ma_cross", [100.0 + i for i in range(30)] + [200.0, 201.0])


# ---------------------------------------------------------------- exits are shared


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_every_strategy_shares_the_same_take_profit_and_stop_loss(name):
    held = Position(base=1.0, quote=1.0, entry_price=100.0)
    config = Config(strategy=name, take_profit_pct=5.0, stop_loss_pct=3.0)
    assert decide([105.0], held, config) == "sell"
    assert decide([96.9], held, config) == "sell"
    assert decide([101.0], held, config) == "hold"


def test_changed_take_profit_and_stop_loss_apply_to_an_open_position():
    held = Position(base=1.0, quote=1.0, entry_price=100.0)
    assert decide([102.0], held, Config(take_profit_pct=1.5)) == "sell"
    assert decide([102.0], held) == "hold"
    assert decide([98.0], held, Config(stop_loss_pct=1.0)) == "sell"


# ---------------------------------------------------------------- validation


def test_empty_settings_are_the_defaults():
    assert parse_config({}) == strategy.DEFAULT


def test_a_full_round_trip_through_to_dict():
    config = parse_config(
        {"strategy": "rsi", "take_profit_pct": 6, "params": {"rsi": {"period": 10}}}
    )
    assert config.values()["period"] == 10 and config.values()["oversold"] == 30
    assert parse_config(strategy.to_dict(config)) == config


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"strategy": "yolo"},
        {"strategies": "rsi"},  # a typo is an error, not silently ignored
        {"take_profit_pct": 0.5},  # below the fee floor
        {"take_profit_pct": 500},
        {"stop_loss_pct": 0},
        {"take_profit_pct": "5"},
        {"take_profit_pct": True},
        {"take_profit_pct": float("nan")},
        {"take_profit_pct": float("inf")},
        {"params": []},
        {"params": {"nope": {}}},
        {"params": {"rsi": {"nope": 1}}},
        {"params": {"rsi": []}},
        {"params": {"rsi": {"period": 1}}},  # under the minimum
        {"params": {"rsi": {"period": 14.5}}},  # must be whole
        {"params": {"mean_reversion": {"threshold_pct": 0.1}}},  # inside the fees
        {"params": {"ma_cross": {"fast": 30, "slow": 20}}},
    ],
)
def test_bad_settings_are_rejected(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_config_error_is_a_value_error():
    assert issubclass(ConfigError, ValueError)


def test_every_default_passes_its_own_validation():
    parse_config(strategy.to_dict(strategy.DEFAULT))


def test_history_is_longer_than_any_window_a_person_can_set():
    biggest = max(
        p.high for s in STRATEGIES.values() for p in s.params.values() if p.integer
    )
    assert strategy.HISTORY > biggest + 1


def test_schema_describes_every_strategy_and_parameter():
    schema = strategy.schema()
    assert set(schema["strategies"]) == set(STRATEGIES)
    for name, entry in schema["strategies"].items():
        assert entry["label"] and entry["about"]
        assert set(entry["params"]) == set(STRATEGIES[name].params)


# ---------------------------------------------------------------- settings that can never work


def test_bollinger_that_can_never_buy_is_rejected_with_a_reason():
    # the newest price is in the window: at most sqrt(window - 1) deviations below the mean
    with pytest.raises(ConfigError, match="would never buy"):
        parse_config({"params": {"bollinger": {"window": 5, "deviations": 2.0}}})
    with pytest.raises(ConfigError, match="would never buy"):
        parse_config({"params": {"bollinger": {"window": 20, "deviations": 4.5}}})
    parse_config({"params": {"bollinger": {"window": 20, "deviations": 4.0}}})
    parse_config({"params": {"bollinger": {"window": 5, "deviations": 1.5}}})


def test_the_bollinger_limit_is_real_not_just_conservative():
    # the most extreme possible dip in a 5-sample window still cannot reach 2 deviations
    extreme = [100.0, 100.0, 100.0, 100.0, 1.0]
    assert not buys("bollinger", extreme, window=5, deviations=2.0)
    assert buys("bollinger", extreme, window=5, deviations=1.5)


@pytest.mark.parametrize(
    "raw",
    [
        {"strategy": []},
        {"strategy": {}},
        {"strategy": 5},
        {"take_profit_pct": 10**400},  # an int too large for a float
        {"params": {"rsi": {"period": 10**400}}},
        {"params": {"rsi": {"period": -(10**400)}}},
    ],
)
def test_unusual_json_values_are_a_config_error_not_a_crash(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


# ---------------------------------------------------------------- the pattern guard


def stepped(directions, quiet=5, size=1.04):
    # a price that sits still, then jumps up or down by 4% at each given direction (+1 / -1)
    prices, price = [100.0] * quiet, 100.0
    for d in directions:
        price *= size if d > 0 else 1 / size
        prices += [price] * quiet
    return prices


ALTERNATING = [1, -1] * 4  # eight steps, every neighbouring pair flips
GUARDED = Config(strategy="mean_reversion")  # the default guard (80% / 5%)


def test_only_big_jumps_count_as_steps():
    wiggle = [100.0, 101.0, 100.0, 101.5, 100.0] * 6  # about 1.5% moves: noise
    assert strategy.big_steps(wiggle) == []
    assert strategy.big_steps(stepped([1, -1, -1])) == [1, -1, -1]


def test_the_guard_allows_a_pattern_that_alternates():
    status = strategy.guard_status(stepped(ALTERNATING), GUARDED)
    assert status["steps"] == 8 and status["flip_pct"] == 100
    assert status["reason"] is None


def test_the_guard_pauses_when_the_steps_stop_alternating():
    # a downtrend: eight drops in a row
    down = strategy.guard_status(stepped([-1] * 8), GUARDED)
    assert down["flip_pct"] == 0
    assert "alternate" in down["reason"]
    # a bot that flips only about half the time is paused too
    mixed = strategy.guard_status(stepped([1, 1, -1, 1, -1, -1, 1, 1]), GUARDED)
    assert mixed["flip_pct"] < 80 and mixed["reason"]


def test_the_guard_fails_closed_without_enough_steps():
    for prices in ([], FLAT, stepped([1, -1, 1])):
        status = strategy.guard_status(prices, GUARDED)
        assert "only" in status["reason"]
    assert strategy.guard_status(stepped(ALTERNATING[:7]), GUARDED)["reason"]


def test_one_odd_step_among_eight_is_tolerated_but_two_are_not():
    one_off = [1, -1, 1, -1, 1, -1, 1, 1]  # 6 of 7 pairs flip = 86%
    two_off = [1, -1, 1, 1, 1, -1, 1, -1]  # 5 of 7 = 71%
    assert strategy.guard_status(stepped(one_off), GUARDED)["reason"] is None
    assert strategy.guard_status(stepped(two_off), GUARDED)["reason"]


def test_old_steps_fall_out_of_the_window():
    old_trend = stepped([-1] * 8, quiet=1)
    recent_pattern = stepped(ALTERNATING, quiet=strategy.GUARD_WINDOW // 9)
    assert strategy.guard_status(old_trend + recent_pattern, GUARDED)["reason"] is None


def test_the_guard_pauses_on_a_sliding_price_even_when_steps_alternate():
    n = strategy.DRIFT_SAMPLES
    slide = stepped(ALTERNATING) + [100.0] * n + [92.0] * n  # the average fell 8%
    status = strategy.guard_status(slide, GUARDED)
    assert status["drift_pct"] == pytest.approx(-8.0)
    assert "sliding" in status["reason"]
    rising = stepped(ALTERNATING) + [100.0] * n + [108.0] * n
    assert strategy.guard_status(rising, GUARDED)["reason"] is None


def test_each_guard_check_can_be_switched_off_with_zero():
    n = strategy.DRIFT_SAMPLES
    trend = stepped([-1] * 8)
    assert strategy.guard_status(trend, Config(guard_flip_pct=0))["reason"] is None
    slide = stepped(ALTERNATING) + [100.0] * n + [90.0] * n
    assert strategy.guard_status(slide, Config(guard_drift_pct=0))["reason"] is None
    off = Config(guard_flip_pct=0, guard_drift_pct=0)
    assert strategy.guard_status([], off)["reason"] is None


def test_the_guard_holds_back_a_buy_but_never_a_sale():
    dip = stepped(
        [-1] * 8
    )  # a downtrend: the entry rule wants a buy, the guard says no
    assert (
        decide(
            dip,
            HAS_QUOTE,
            Config(strategy="mean_reversion", guard_flip_pct=0, guard_drift_pct=0),
        )
        == "buy"
    )
    assert decide(dip, HAS_QUOTE, GUARDED) == "hold"
    held = Position(base=1.0, quote=1.0, entry_price=100.0)
    assert decide([95.0], held, GUARDED) == "sell"  # stop loss at -4%: guard or not


def test_the_buy_goes_ahead_when_the_pattern_is_healthy():
    prices = stepped(ALTERNATING) + [100.0] * 30
    prices[-1] = 90.0  # a dip below the average
    assert decide(prices, HAS_QUOTE, GUARDED) == "buy"


def test_guard_settings_are_validated_and_listed():
    cfg_ = parse_config({"guard_flip_pct": 90, "guard_drift_pct": 0})
    assert (cfg_.guard_flip_pct, cfg_.guard_drift_pct) == (90, 0)
    for bad in (
        {"guard_flip_pct": 101},
        {"guard_flip_pct": -1},
        {"guard_drift_pct": 50},
        {"guard_flip_pct": "80"},
    ):
        with pytest.raises(ConfigError):
            parse_config(bad)
    assert strategy.to_dict(Config())["guard_flip_pct"] == strategy.GUARD_FLIP_PCT
    assert set(strategy.schema()) >= {"guard_flip_pct", "guard_drift_pct"}
    assert parse_config(strategy.to_dict(Config())) == Config()


def test_the_guard_says_which_check_paused_buying():
    n = strategy.DRIFT_SAMPLES
    assert strategy.guard_status([], GUARDED)["kind"] == "few_steps"
    assert strategy.guard_status(stepped([-1] * 8), GUARDED)["kind"] == "flip"
    slide = stepped(ALTERNATING) + [100.0] * n + [90.0] * n
    assert strategy.guard_status(slide, GUARDED)["kind"] == "slide"
    assert strategy.guard_status(stepped(ALTERNATING), GUARDED)["kind"] is None


def test_the_guard_survives_zero_and_not_a_number_prices():
    n = strategy.DRIFT_SAMPLES
    zeros = stepped(ALTERNATING) + [0.0] * n + [5.0] * n
    assert (
        strategy.guard_status(zeros, GUARDED)["drift_pct"] is None
    )  # no division by zero
    nan = stepped(ALTERNATING) + [100.0] * n + [float("nan")] * n
    assert strategy.guard_status(nan, GUARDED)[
        "reason"
    ]  # a NaN average pauses, it does not pass


def test_a_step_of_exactly_the_limit_counts_whatever_the_float_rounding():
    for a, b in ((100.0, 102.0), (0.1, 0.102), (1e-9, 1.02e-9), (3.3, 3.366)):
        assert strategy.big_steps([a, b]) == [1], (a, b)


# ---------------------------------------------------------------- time stop


def test_the_time_stop_is_off_unless_it_is_ticked():
    held = Position(base=1.0, quote=1.0, entry_price=100.0, held_seconds=99 * 3600)
    assert decide([100.0], held, Config()) == "hold"
    on = Config(time_stop_on=True, time_stop_min=15)
    assert decide([100.0], held, on) == "sell"


def test_the_time_stop_waits_for_its_minutes():
    on = Config(time_stop_on=True, time_stop_min=15)
    assert strategy.exit_reason(100.0, 100.0, on, 14 * 60 + 59) is None
    assert strategy.exit_reason(100.0, 100.0, on, 15 * 60) == "time_stop"
    # an age that is not known cannot trigger it
    assert strategy.exit_reason(100.0, 100.0, on, None) is None


def test_a_price_target_is_the_reason_even_when_the_time_is_up_too():
    on = Config(time_stop_on=True, time_stop_min=15)
    assert strategy.exit_reason(104.0, 100.0, on, 3600) == "take_profit"
    assert strategy.exit_reason(90.0, 100.0, on, 3600) == "stop_loss"


def test_time_stop_settings_are_validated_and_round_trip():
    config = parse_config({"time_stop_on": True, "time_stop_min": 20})
    assert (config.time_stop_on, config.time_stop_min) == (True, 20.0)
    assert parse_config(strategy.to_dict(config)) == config
    assert strategy.DEFAULT.time_stop_on is False
    assert strategy.schema()["time_stop_min"]["default"] == strategy.TIME_STOP_MIN
    for bad in (
        {"time_stop_on": "yes"},
        {"time_stop_on": 1},
        {"time_stop_min": 0},
        {"time_stop_min": 10_000},
        {"time_stop_min": "15"},
    ):
        with pytest.raises(ConfigError):
            parse_config(bad)


# ---------------------------------------------------------------- trade size


def test_trade_size_is_the_ceiling_when_risk_sizing_is_off():
    ceiling = Decimal(1000)
    assert strategy.trade_size(Decimal(5000), ceiling, Decimal(0), 4.0) == ceiling
    assert strategy.trade_size(Decimal(10), ceiling, Decimal(0), 4.0) == ceiling


def test_trade_size_risks_a_share_of_the_vault_on_the_stop_loss():
    # 2% of 1386 = 27.72; a 4% stop plus 0.75% costs = 4.75% per unit -> 583.578...
    size = strategy.trade_size(Decimal(1386), Decimal(1000), Decimal(2), 4.0)
    assert size == Decimal("583.578947")
    assert size * Decimal("0.0475") <= Decimal(1386) * Decimal("0.02")


def test_trade_size_never_goes_over_the_ceiling_or_the_vault():
    assert strategy.trade_size(
        Decimal(1386), Decimal(1000), Decimal(5), 4.0
    ) == Decimal(1000)
    # a high risk share on a small vault would otherwise ask for more than the vault holds
    assert strategy.trade_size(Decimal(50), Decimal(1000), Decimal(20), 4.0) == Decimal(
        "50.000000"
    )


def test_a_tighter_stop_loss_allows_a_bigger_trade():
    wide = strategy.trade_size(Decimal(1000), Decimal(5000), Decimal(1), 8.0)
    tight = strategy.trade_size(Decimal(1000), Decimal(5000), Decimal(1), 2.0)
    assert tight > wide


def test_the_guard_reports_the_step_pattern_it_judged():
    steps = [1, -1, 1, -1, 1, 1, -1, 1]
    status = strategy.guard_status(stepped(steps), GUARDED)
    assert status["sequence"] == steps
    assert (status["alternating"], status["pairs"]) == (6, 7)
