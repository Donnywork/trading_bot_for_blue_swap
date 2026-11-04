# Exact Uniswap V3 swap maths over a pool's real tick layout (see scan_liquidity.py). Offline.
# base = token0, quote = token1; price = quote per base. Amounts are human units (fETH, f564).
import bisect
import json
import math

TICK_BASE = 1.0001


class V3Pool:
    def __init__(self, profile_path, price, fee=0.003, depth_scale=1.0):
        p = json.load(open(profile_path))
        ticks = sorted(p["ticks"], key=lambda t: t["tick"])
        # the maths below assumes equal decimals on both tokens and the fee tier of the profile
        assert p["d0"] == p["d1"] and p["fee"] == round(fee * 1e6), "profile does not match the assumptions"
        self.fee, self.scale = fee, 10 ** p["d0"]
        self.tick_sqrt = [TICK_BASE ** (t["tick"] / 2) for t in ticks]
        self.net = [t["net"] * depth_scale / self.scale for t in ticks]  # liquidity in human units
        self.s = math.sqrt(price)
        self.i = bisect.bisect_right(self.tick_sqrt, self.s)  # ticks at or below the price
        self.L = sum(self.net[: self.i])

    def price(self):
        return self.s * self.s

    def copy(self):
        c = object.__new__(V3Pool)
        c.__dict__.update(self.__dict__)
        return c

    def _up(self, quote_in):  # spend quote (gross), receive base; price rises
        left, out = quote_in * (1 - self.fee), 0.0
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

    def _down(self, base_in):  # sell base (gross), receive quote; price falls
        left, out = base_in * (1 - self.fee), 0.0
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

    # the interface the simulator uses
    def buy_with_quote(self, quote):
        return self._up(quote)

    def sell_base(self, base):
        return self._down(base)

    def buy_base(self, base):
        # exactly `base` out; returns the quote paid (gross, fee included)
        left, paid = base, 0.0
        while left > 1e-18:
            nxt = self.tick_sqrt[self.i] if self.i < len(self.tick_sqrt) else math.inf
            room = self.L * (1 / self.s - 1 / nxt) if nxt < math.inf else math.inf
            if self.L > 0 and left <= room:
                s_new = 1 / (1 / self.s - left / self.L)
                paid += self.L * (s_new - self.s)
                self.s, left = s_new, 0.0
            else:
                if nxt == math.inf:
                    raise ValueError("no liquidity left")
                paid += self.L * (nxt - self.s)
                left -= room
                self.s = nxt
                self.L += self.net[self.i]
                self.i += 1
        return paid / (1 - self.fee)
