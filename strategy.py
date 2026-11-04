from dataclasses import dataclass

# PLACEHOLDER strategy: mean reversion against a moving average. It exists so the bot loop can run
# end to end; replace decide() with your own. Fees are 0.3% each way (about 0.6% round trip) plus
# price impact, so THRESHOLD_PCT must stay well above that or the strategy loses money when "right".
WINDOW = 20  # price samples in the moving average
THRESHOLD_PCT = 2.0  # how far from the average before acting


@dataclass(frozen=True)
class Position:
    # vault balances in human units. "base" is the pool's token0 (priced in token1 = "quote")
    base: float
    quote: float


def decide(prices, position):
    # ------------------------------------
    # Pure function: price history + position -> "buy" | "sell" | "hold"
    # "buy" acquires the base token (spends quote), "sell" does the reverse.
    # No web3 calls here, so it can be unit-tested with fake price lists.
    # ------------------------------------

    if len(prices) < WINDOW:
        return "hold"

    average = sum(prices[-WINDOW:]) / WINDOW
    deviation_pct = (prices[-1] - average) / average * 100

    if deviation_pct <= -THRESHOLD_PCT and position.quote > 0:
        return "buy"  # base is cheap versus its recent average
    if deviation_pct >= THRESHOLD_PCT and position.base > 0:
        return "sell"  # base is expensive versus its recent average
    return "hold"
