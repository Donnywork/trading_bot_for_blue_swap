import bisect
import json
import logging
import math
from functools import cache
from pathlib import Path

import ledger

# ------------------------------------
# Exact Uniswap V3 swap maths over the pool's real liquidity layout. Pure and offline: no web3, no
# chain read, so the bot can ask "what does a buy of this size cost?" without a quote call. base =
# token0, quote = token1; price = quote per base; amounts are human units (fETH, f564).
#
# The layout (pool_profile.json) is a snapshot of the pool as of 2026-11-04 (its "block"). If
# liquidity providers add or pull liquidity the costs change, and reading the whole layout again
# is about 120 calls, too many to do often. What is cheap is the pool's liquidity() in range, ONE
# call: depth_scale() compares it with what the snapshot holds at the same price, and scaled()
# gives the layout with every position's liquidity multiplied by that. It follows the pool getting
# deeper or thinner as a whole; a change in WHERE the liquidity sits needs a new scan, which the
# dashboard's "scan" button asks the bot for (config.read_depth, about 120 calls, on demand).
# ------------------------------------

PROFILE_FILE = Path(__file__).parent / "pool_profile.json"
# where the bot saves a layout it scanned itself when the dashboard's "scan" button asks for one;
# it takes the place of the shipped file wherever there is one (see current_profile)
LIVE_PROFILE_FILE = Path(__file__).parent / "logs" / "pool_profile.json"
TICK_BASE = 1.0001
MAX_TICK = 887272
FEE = 0.003
log = logging.getLogger("bot")


def check_profile(profile, where):
    if not isinstance(profile, dict) or not {"d0", "d1", "fee", "ticks"} <= set(
        profile
    ):
        raise ValueError(f"{where}: not a pool profile")
    if profile["d0"] != profile["d1"] or profile["fee"] != round(FEE * 1e6):
        raise ValueError(
            f"{where}: the pool profile does not match the maths (equal decimals, 0.3% fee)"
        )
    # (a damaged file with the right keys must be refused here, not crash a trade later: a tick
    # out of range or a huge net overflows the maths with an OverflowError, which nothing catches)
    ticks = profile["ticks"]
    request = profile.get("request", 0)
    if (
        type(profile["d0"]) is not int
        or not 0 <= profile["d0"] <= 36
        or type(profile.get("block")) is not int
        or type(request) is not int
        or request < 0
        or not isinstance(ticks, list)
        or not ticks
        or not all(
            isinstance(t, dict)
            and type(t.get("tick")) is int
            and abs(t["tick"]) <= MAX_TICK
            and type(t.get("net")) is int
            and abs(t["net"]) < 2**127  # (liquidity is a uint128)
            for t in ticks
        )
    ):
        raise ValueError(
            f"{where}: the pool profile's block, decimals or ticks are malformed"
        )
    return profile


@cache
def load_profile(path=PROFILE_FILE):
    # raises OSError / ValueError when the file is missing, unreadable or not for this fee tier
    return check_profile(json.loads(Path(path).read_text()), path)


_live = {}  # path -> (file stamp, profile): read again only when the file changed
_warned = set()  # (path, file stamp) of damaged files already reported


def current_profile(live_file=LIVE_PROFILE_FILE):
    # The layout the bot scanned last, if there is a usable one; else the one that ships with the
    # code (the scanned file does not exist until the first scan; a damaged one is ignored, with
    # a warning once, since the dashboard would otherwise show a layout nobody scanned).
    try:
        stat = Path(live_file).stat()
    except OSError:
        return load_profile()
    # (a scan replaces the file, so the inode changes with every save)
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    try:
        cached = _live.get(live_file)
        if cached is None or cached[0] != stamp:
            profile = check_profile(json.loads(Path(live_file).read_text()), live_file)
            cached = _live[live_file] = (stamp, profile)
        return cached[1]
    except (OSError, ValueError) as e:
        if (live_file, stamp) not in _warned:
            _warned.add((live_file, stamp))
            log.warning(
                "ignoring the scanned pool layout, using the shipped one: %s", e
            )
        return load_profile()


def save_profile(path, profile):
    ledger.atomic_write(path, json.dumps(profile))


def scaled(profile, scale):
    # the same layout, every position holding `scale` times its liquidity
    return {
        **profile,
        "ticks": [{**t, "net": int(t["net"] * scale)} for t in profile["ticks"]],
        "scale": scale,
    }


def depth_scale(profile, price, live_liquidity):
    # How deep the pool is now against the saved layout: the liquidity the pool reports in range
    # (its liquidity() call, raw units) over what the saved layout holds in range at `price`.
    # 1.0 = as saved, 0.5 = half as deep.
    saved = V3Pool(profile, price).L * 10 ** profile["d0"]
    if saved <= 0:
        raise ValueError("the saved layout holds no liquidity at this price")
    return live_liquidity / saved


class V3Pool:
    def __init__(self, profile, price):
        ticks = sorted(profile["ticks"], key=lambda t: t["tick"])
        scale = 10 ** profile["d0"]
        self.tick_sqrt = [TICK_BASE ** (t["tick"] / 2) for t in ticks]
        self.net = [t["net"] / scale for t in ticks]  # liquidity in human units
        self.s = math.sqrt(price)
        self.i = bisect.bisect_right(
            self.tick_sqrt, self.s
        )  # ticks at or below the price
        self.L = sum(self.net[: self.i])

    def buy_with_quote(self, quote):
        # spend `quote` (fee included), receive base; the price rises
        left, out = quote * (1 - FEE), 0.0
        while left > 1e-18:
            nxt = self.tick_sqrt[self.i] if self.i < len(self.tick_sqrt) else math.inf
            room = self.L * (nxt - self.s)
            if self.L > 0 and left <= room:
                s_new = self.s + left / self.L
                out += self.L * (1 / self.s - 1 / s_new)
                self.s, left = s_new, 0.0
            else:
                if nxt == math.inf:
                    raise ValueError("no liquidity left")
                out += self.L * (1 / self.s - 1 / nxt)
                left -= room
                self.s = nxt
                self.L += self.net[self.i]
                self.i += 1
        return out

    def sell_base(self, base):
        # sell `base` (fee included), receive quote; the price falls
        left, out = base * (1 - FEE), 0.0
        while left > 1e-18:
            nxt = self.tick_sqrt[self.i - 1] if self.i > 0 else 0.0
            room = self.L * (1 / nxt - 1 / self.s) if nxt > 0 else math.inf
            if self.L > 0 and left <= room:
                s_new = 1 / (1 / self.s + left / self.L)
                out += self.L * (self.s - s_new)
                self.s, left = s_new, 0.0
            else:
                if self.i == 0:
                    raise ValueError("no liquidity left")
                out += self.L * (self.s - nxt)
                left -= room
                self.s = nxt
                self.i -= 1
                self.L -= self.net[self.i]
        return out


def round_trip(profile, entry_price, exit_price, quote):
    # What comes back, in quote: buy with `quote` at `entry_price`, sell all of it at `exit_price`.
    # Both pool fees and the price impact of both legs are taken off. Raises ValueError when the
    # pool cannot absorb that size.
    base = V3Pool(profile, entry_price).buy_with_quote(quote)
    return V3Pool(profile, exit_price).sell_base(base)


def loss_at_stop(profile, price, stop_loss_pct, quote):
    # ------------------------------------
    # What a stop loss costs, in quote: buy with `quote` at `price`, the price falls by the stop
    # loss, sell everything that was bought. Both pool fees and the price impact of both legs are
    # in it. Raises ValueError when the pool cannot absorb that size.
    # ------------------------------------

    return quote - round_trip(profile, price, price * (1 - stop_loss_pct / 100), quote)
