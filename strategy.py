import itertools
import math
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

# PLACEHOLDER strategies. Every one is an ENTRY rule ("when do I buy?"); the exit is shared: sell
# at a take profit or a stop loss from the entry price. They exist so the bot loop can run end to
# end and so you can compare ideas on a live pool; replace them with your own. Fees are 0.3% each
# way (about 0.6% round trip) plus price impact, so thresholds must stay well above that or a
# strategy loses money when "right". None of these has a proven edge: judge them on data.
WINDOW = 20  # price samples in the default moving average
THRESHOLD_PCT = 2.0  # default dip below the average before buying
TAKE_PROFIT_PCT = 3.0  # sell when the price is this far above the entry price
STOP_LOSS_PCT = 4.0  # sell when the price is this far below the entry price
# limits for the settings people can edit at runtime (the dashboard, logs/control.json)
TAKE_PROFIT_RANGE = (1.0, 100.0)  # below ~1% the round-trip fees eat the profit
STOP_LOSS_RANGE = (0.5, 50.0)
MIN_DIP_PCT = 1.0  # a dip smaller than this is noise next to fees: never a buy signal
HISTORY = 600  # price samples the runner keeps: more than the largest window below
# The pattern guard. Every entry rule assumes the market behaves as it did; this pauses NEW buys
# when it visibly stops doing so (exits are never paused). It reads the price series only.
# the recent big price steps must alternate up/down at least this often (6 of 7 pairs = 86%)
GUARD_FLIP_PCT = 80.0
# ...and the recent average may not sit this far below the one before it
GUARD_DRIFT_PCT = 5.0
GUARD_FLIP_RANGE = (0.0, 100.0)  # 0 switches that check off
GUARD_DRIFT_RANGE = (0.0, 20.0)  # 0 switches that check off
# a jump this big between two samples is "a big swap"; smaller ones are noise
GUARD_STEP_PCT = 2.0
GUARD_STEPS = 8  # how many of the latest big steps are judged (so 7 up/down pairs)
# only steps within this many samples count (3 hours at one sample per 30 s)
GUARD_WINDOW = 360
DRIFT_SAMPLES = 60  # compared: the average of the last 60 samples against the 60 before


class ConfigError(ValueError):
    """A setting a person typed is not acceptable; the message says why."""


@dataclass(frozen=True)
class Position:
    # vault balances in human units. "base" is the pool's token0 (priced in token1 = "quote")
    base: float
    quote: float
    # price at which the bot bought the base it still holds; None when it holds none of its own.
    # Base that was deposited rather than bought has no entry price and is never sold.
    entry_price: float | None = None


@dataclass(frozen=True)
class Param:
    default: float
    low: float
    high: float
    label: str
    integer: bool = False


@dataclass(frozen=True)
class Strategy:
    label: str
    about: str
    params: dict[str, Param]
    entry: Callable  # (prices, values) -> bool: is this a moment to buy?
    check: Callable | None = (
        None  # (values) -> raises ConfigError for a combination that makes no sense
    )


# ------------------------------------
# Entry rules. Pure: price history in, True/False out. Each returns False until it has enough
# history, so a fresh start never trades on a half-empty window.
# ------------------------------------


def mean_reversion(prices, p):
    # buy when the price has dropped this far below its recent average
    n = p["window"]
    if len(prices) < n:
        return False
    average = sum(prices[-n:]) / n
    return (prices[-1] - average) / average * 100 <= -p["threshold_pct"]


def sharp_drop(prices, p):
    # buy right after one big sell: the price is this far below the highest of the last few
    # samples. Unlike mean reversion it ignores slow drift, so it fits a pool where one scripted
    # bot swaps back and forth in big steps and the price returns after its next swap.
    n = p["lookback"]
    if len(prices) < n + 1:
        return False
    return prices[-1] <= max(prices[-(n + 1) :]) * (1 - p["drop_pct"] / 100)


def bollinger(prices, p):
    # like mean reversion, but "far below" is measured in standard deviations, so the bar rises
    # in a jumpy market and falls in a calm one
    n = p["window"]
    if len(prices) < n:
        return False
    recent = prices[-n:]
    mean = sum(recent) / n
    dip_pct = (mean - prices[-1]) / mean * 100
    return (
        prices[-1] < mean - p["deviations"] * statistics.pstdev(recent)
        and dip_pct >= MIN_DIP_PCT
    )


def rsi(prices, p):
    # buy when recent moves have been overwhelmingly down (relative strength index "oversold")
    n = p["period"]
    if len(prices) < n + 1:
        return False
    recent = prices[-(n + 1) :]
    deltas = [b - a for a, b in itertools.pairwise(recent)]
    gains = sum(d for d in deltas if d > 0)
    losses = -sum(d for d in deltas if d < 0)
    if gains + losses == 0:
        return False
    value = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    fell_enough = recent[-1] <= recent[0] * (1 - MIN_DIP_PCT / 100)
    return value <= p["oversold"] and fell_enough


def breakout(prices, p):
    # trend following: buy when the price pushes above everything seen in the window
    n = p["window"]
    if len(prices) < n + 1:
        return False
    return prices[-1] >= max(prices[-(n + 1) : -1]) * (1 + p["breakout_pct"] / 100)


def ma_cross(prices, p):
    # trend following: buy at the moment the fast average rises above the slow one
    fast, slow = p["fast"], p["slow"]
    if len(prices) < slow + 1:
        return False

    def sma(end, n):
        return sum(prices[end - n : end]) / n

    last = len(prices)
    was_below = sma(last - 1, fast) <= sma(last - 1, slow)
    return was_below and sma(last, fast) > sma(last, slow)


def check_bollinger(p):
    # the newest price is inside the window, so it can sit at most sqrt(window - 1) standard
    # deviations below the average: asking for more means the strategy can never buy
    reach = math.sqrt(p["window"] - 1)
    if p["deviations"] >= reach:
        raise ConfigError(
            f"Bollinger bands: a window of {p['window']} can reach at most {reach:.2f} "
            f"deviations, so {p['deviations']:g} would never buy. Use fewer deviations "
            "or a longer window"
        )


def check_ma_cross(p):
    if p["fast"] >= p["slow"]:
        raise ConfigError(
            "moving average cross: the fast window must be shorter than the slow"
        )


STRATEGIES = {
    "mean_reversion": Strategy(
        "Mean reversion",
        "Buys a dip: the price fell below its recent average and may bounce back. "
        "Loses in a steady downtrend.",
        {
            "window": Param(WINDOW, 5, 500, "Average over (samples)", True),
            "threshold_pct": Param(
                THRESHOLD_PCT, MIN_DIP_PCT, 20, "Dip below average (%)"
            ),
        },
        mean_reversion,
    ),
    "bollinger": Strategy(
        "Bollinger bands",
        "Buys a dip too, but 'big enough' adapts to how jumpy the price has been. "
        "Fewer trades in a calm pool.",
        {
            "window": Param(WINDOW, 5, 500, "Average over (samples)", True),
            "deviations": Param(2.0, 0.5, 5, "Std. deviations below"),
        },
        bollinger,
        check_bollinger,
    ),
    "rsi": Strategy(
        "RSI oversold",
        "Buys after a run of mostly falling prices (RSI at or below the limit). "
        "Another way of buying a dip.",
        {
            "period": Param(14, 2, 200, "Look back (samples)", True),
            "oversold": Param(30, 5, 50, "RSI at or below"),
        },
        rsi,
    ),
    "sharp_drop": Strategy(
        "Sharp drop",
        "Buys right after one big sell pushes the price down in a single step, and ignores slow "
        "drift. Fits a pool where a bot swaps back and forth in big steps.",
        {
            "lookback": Param(4, 2, 100, "Look back (samples)", True),
            "drop_pct": Param(3.0, MIN_DIP_PCT, 20, "Drop from the recent high (%)"),
        },
        sharp_drop,
    ),
    "breakout": Strategy(
        "Breakout",
        "Follows the trend: buys when the price rises above its recent high. "
        "Loses when breakouts fade straight away.",
        {
            "window": Param(WINDOW, 5, 500, "Recent high over (samples)", True),
            "breakout_pct": Param(1.0, 0.1, 10, "Above the high by (%)"),
        },
        breakout,
    ),
    "ma_cross": Strategy(
        "Moving average cross",
        "Follows the trend: buys when the fast average climbs above the slow one. "
        "Slow to react, so it enters late.",
        {
            "fast": Param(5, 2, 100, "Fast average (samples)", True),
            "slow": Param(WINDOW, 5, 500, "Slow average (samples)", True),
        },
        ma_cross,
        check_ma_cross,
    ),
}


@dataclass(frozen=True)
class Config:
    strategy: str = "mean_reversion"
    take_profit_pct: float = TAKE_PROFIT_PCT
    stop_loss_pct: float = STOP_LOSS_PCT
    # {strategy name: {parameter: value}}; whatever is not given is filled in with the default,
    # so two configs that behave the same also compare equal
    params: dict = field(default_factory=dict)
    guard_flip_pct: float = GUARD_FLIP_PCT
    guard_drift_pct: float = GUARD_DRIFT_PCT

    def __post_init__(self):
        full = {
            name: {
                key: self.params.get(name, {}).get(key, p.default)
                for key, p in s.params.items()
            }
            for name, s in STRATEGIES.items()
        }
        object.__setattr__(self, "params", full)

    def values(self, name=None):
        return dict(self.params[name or self.strategy])


DEFAULT = Config()


def number(label, value, low, high, integer=False):
    # JSON can hold true, "5", NaN or 1e999: only a finite real number inside the limits passes
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:  # an integer too large for a float
        finite = False
    if not finite or not low <= value <= high:
        raise ConfigError(f"{label} must be between {low:g} and {high:g}")
    if integer:
        if value != int(value):
            raise ConfigError(f"{label} must be a whole number")
        return int(value)
    return float(value)


def parse_config(raw):
    # ------------------------------------
    # Validate settings typed by a person (the dashboard, logs/control.json). Raises ValueError
    # with a message they can act on. Missing keys keep their defaults; unknown keys are errors,
    # so a typo is not silently ignored.
    # ------------------------------------

    if not isinstance(raw, dict):
        raise ConfigError("settings must be an object")
    unknown = set(raw) - {
        "strategy",
        "take_profit_pct",
        "stop_loss_pct",
        "params",
        "guard_flip_pct",
        "guard_drift_pct",
    }
    if unknown:
        raise ConfigError(f"unknown setting(s): {sorted(unknown)}")

    name = raw.get("strategy", DEFAULT.strategy)
    if not isinstance(name, str) or name not in STRATEGIES:
        raise ConfigError(
            f"unknown strategy {name!r}, choose from {sorted(STRATEGIES)}"
        )
    take_profit = number(
        "take profit %", raw.get("take_profit_pct", TAKE_PROFIT_PCT), *TAKE_PROFIT_RANGE
    )
    stop_loss = number(
        "stop loss %", raw.get("stop_loss_pct", STOP_LOSS_PCT), *STOP_LOSS_RANGE
    )

    guard_flip = number(
        "pattern guard: big steps that alternate up/down (%)",
        raw.get("guard_flip_pct", GUARD_FLIP_PCT),
        *GUARD_FLIP_RANGE,
    )
    guard_drift = number(
        "pattern guard: price slide (%)",
        raw.get("guard_drift_pct", GUARD_DRIFT_PCT),
        *GUARD_DRIFT_RANGE,
    )

    given = raw.get("params", {})
    if not isinstance(given, dict):
        raise ConfigError("params must be an object")
    params = {}
    for strategy_name, values in given.items():
        if strategy_name not in STRATEGIES:
            raise ConfigError(f"params for unknown strategy {strategy_name!r}")
        spec = STRATEGIES[strategy_name].params
        if not isinstance(values, dict):
            raise ConfigError(f"params for {strategy_name} must be an object")
        extra = set(values) - set(spec)
        if extra:
            raise ConfigError(
                f"unknown parameter(s) for {strategy_name}: {sorted(extra)}"
            )
        params[strategy_name] = {
            key: number(
                f"{STRATEGIES[strategy_name].label}: {spec[key].label}",
                value,
                spec[key].low,
                spec[key].high,
                spec[key].integer,
            )
            for key, value in values.items()
        }

    config = Config(name, take_profit, stop_loss, params, guard_flip, guard_drift)
    for (
        strategy_name,
        strat,
    ) in STRATEGIES.items():  # also the strategies not selected now
        if strat.check:
            strat.check(config.values(strategy_name))
    return config


def to_dict(config):
    # every strategy's values filled in, so a form can show all of them
    return {
        "strategy": config.strategy,
        "take_profit_pct": config.take_profit_pct,
        "stop_loss_pct": config.stop_loss_pct,
        "params": {name: config.values(name) for name in STRATEGIES},
        "guard_flip_pct": config.guard_flip_pct,
        "guard_drift_pct": config.guard_drift_pct,
    }


def schema():
    # what a form needs to draw the controls and their limits
    return {
        "strategies": {
            name: {
                "label": s.label,
                "about": s.about,
                "params": {k: asdict(p) for k, p in s.params.items()},
            }
            for name, s in STRATEGIES.items()
        },
        "take_profit_pct": {
            "default": TAKE_PROFIT_PCT,
            "low": TAKE_PROFIT_RANGE[0],
            "high": TAKE_PROFIT_RANGE[1],
        },
        "stop_loss_pct": {
            "default": STOP_LOSS_PCT,
            "low": STOP_LOSS_RANGE[0],
            "high": STOP_LOSS_RANGE[1],
        },
        "guard_flip_pct": {
            "default": GUARD_FLIP_PCT,
            "low": GUARD_FLIP_RANGE[0],
            "high": GUARD_FLIP_RANGE[1],
        },
        "guard_drift_pct": {
            "default": GUARD_DRIFT_PCT,
            "low": GUARD_DRIFT_RANGE[0],
            "high": GUARD_DRIFT_RANGE[1],
        },
    }


def big_steps(prices):
    # direction (+1 up, -1 down) of every jump of GUARD_STEP_PCT or more between neighbouring
    # samples, oldest first. A scripted bot's big swaps show up as exactly such jumps.
    steps = []
    for a, b in itertools.pairwise(prices[-GUARD_WINDOW:]):
        # (the small epsilon keeps a step of exactly 2.0% from depending on float rounding)
        if a > 0 and abs(b / a - 1) * 100 >= GUARD_STEP_PCT - 1e-9:
            steps.append(1 if b > a else -1)
    return steps[-GUARD_STEPS:]


def guard_status(prices, config=DEFAULT):
    # ------------------------------------
    # Is the market still behaving like the one the entry rules were built for? Pure: prices in,
    # numbers and a reason out (reason is None when buying is allowed). It fails closed: with too
    # few big steps to judge, it pauses.
    # ------------------------------------

    steps = big_steps(prices)
    flip_pct = None
    if len(steps) >= 2:
        pairs = list(itertools.pairwise(steps))
        flip_pct = 100 * sum(a != b for a, b in pairs) / len(pairs)
    drift_pct = None
    if len(prices) >= 2 * DRIFT_SAMPLES:
        recent = sum(prices[-DRIFT_SAMPLES:]) / DRIFT_SAMPLES
        before = sum(prices[-2 * DRIFT_SAMPLES : -DRIFT_SAMPLES]) / DRIFT_SAMPLES
        if before > 0:
            drift_pct = (recent / before - 1) * 100  # negative: sliding down

    reason, kind = None, None
    if config.guard_flip_pct > 0:
        if len(steps) < GUARD_STEPS:
            kind = "few_steps"
            reason = (
                f"seen only {len(steps)} of the {GUARD_STEPS} big price steps needed to judge "
                "the pattern"
            )
        elif flip_pct < config.guard_flip_pct:
            kind = "flip"
            reason = (
                f"the big price steps alternate up/down only {flip_pct:.0f}% of the time "
                f"(needs {config.guard_flip_pct:g}%)"
            )
    slide = config.guard_drift_pct > 0 and drift_pct is not None
    # "not >" rather than "<=": a NaN average pauses instead of slipping through
    if reason is None and slide and not drift_pct > -config.guard_drift_pct:
        kind = "slide"
        reason = (
            f"the price is sliding: its recent average is {-drift_pct:.1f}% below the "
            f"one before (limit {config.guard_drift_pct:g}%)"
        )
    return {
        "steps": len(steps),
        "flip_pct": flip_pct,
        "drift_pct": drift_pct,
        "reason": reason,
        "kind": kind,  # which check paused buying: few_steps, flip or slide (None if allowed)
    }


def exit_reason(price, entry_price, config=DEFAULT):
    change_pct = (price - entry_price) / entry_price * 100
    if change_pct >= config.take_profit_pct:
        return "take_profit"
    if change_pct <= -config.stop_loss_pct:
        return "stop_loss"
    return None


def decide(prices, position, config=DEFAULT):
    # ------------------------------------
    # Pure function: price history + position -> "buy" | "sell" | "hold"
    # "buy" acquires the base token (spends quote), "sell" does the reverse.
    # No web3 calls here, so it can be unit-tested with fake price lists.
    # ------------------------------------

    if position.entry_price is not None:
        # holding: only the take profit / stop loss end it, whatever the strategy. Checked before
        # any warm-up so a restart never leaves a stop loss waiting for fresh samples.
        return (
            "sell" if exit_reason(prices[-1], position.entry_price, config) else "hold"
        )

    if position.quote <= 0:
        return "hold"
    entry = STRATEGIES[config.strategy].entry
    if not entry(prices, config.values()):
        return "hold"
    # the guard is only worked out when a buy is on the table; it never blocks an exit
    return "hold" if guard_status(prices, config)["reason"] else "buy"
