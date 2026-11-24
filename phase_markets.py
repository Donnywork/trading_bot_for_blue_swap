"""Official Sepolia competition markets. Phase selection is explicit per reader."""

from dataclasses import dataclass

import event_market


@dataclass(frozen=True)
class Market:
    quote: str
    base: str
    pools: tuple[tuple[int, str], ...]
    market_bot: str
    credit: str | None = None


PHASE2 = Market(
    event_market.QUOTE,
    event_market.BASE,
    tuple(event_market.POOLS.items()),
    event_market.MARKET_BOT,
)
PHASE3 = Market(
    "0xc319f958df8A74b077578b437C402dC7D7687864",
    "0xE00296A373Cd19E6aBAf085e50a393938879737D",
    ((3000, "0x7FE977Ba8a5b7b25B8805B3f9456D21D3A4a9934"),),
    event_market.MARKET_BOT,
    "0x2a61eB24e46E8568C15becaEAa7E549B50700Dd2",
)
PHASE3_START = 12_200_847
