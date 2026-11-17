import json
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import Web3Exception
from web3.providers import BaseProvider

log = logging.getLogger("bot")

ROOT = Path(__file__).parent

with open(ROOT / "abis/erc20.json", "r") as f:
    erc20_abi = json.load(f)

with open(ROOT / "abis/pool.json", "r") as f:
    pool_abi = json.load(f)

with open(ROOT / "abis/router.json", "r") as f:
    router_abi = json.load(f)

with open(ROOT / "abis/quoter.json", "r") as f:
    quoter_abi = json.load(f)

SLIPPAGE_BPS = 100  # 1%
MAX_PRICE_IMPACT_PCT = Decimal(2)


def rpc_rate():
    # ------------------------------------
    # MAX_RPC_PER_SECOND from the environment: the most JSON-RPC requests per second the bot
    # may send, whatever the provider (Infura or Alchemy; Alchemy's free tier allows about
    # 15 requests/s). 0 or unset means no cap; a value the parser rejects stops the program
    # instead of silently running uncapped.
    # ------------------------------------

    try:
        rate = Decimal(os.environ.get("MAX_RPC_PER_SECOND", "0"))
    except InvalidOperation:
        rate = Decimal("nan")
    if not rate.is_finite() or rate < 0:
        raise SystemExit(
            "MAX_RPC_PER_SECOND must be a positive number of requests per second, or 0 (no cap)"
        )
    return rate or None


def fallback_urls():
    # ------------------------------------
    # RPC_FALLBACK_URLS from the environment: optional comma-separated URLs used when the
    # primary (RPC_URL) errors. Unset or empty means one provider, the old behavior.
    # ------------------------------------

    raw = os.environ.get("RPC_FALLBACK_URLS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


class ThrottledProvider(Web3.HTTPProvider):
    # ------------------------------------
    # HTTPProvider that spaces the bot's JSON-RPC calls so they never exceed
    # MAX_RPC_PER_SECOND: without it a burst is possible (the depth scan fires about 120
    # reads back to back, though already DEPTH_CALL_PAUSE apart). Every call comes from the
    # one bot thread, so a next-free time is enough; `rate` None means no throttling.
    # ------------------------------------

    def __init__(self, url, rate=None, **kwargs):
        super().__init__(url, **kwargs)
        self._interval = float(1 / rate) if rate else 0.0
        self._next_at = 0.0

    def make_request(self, method, params):
        wait = self._next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_at = time.monotonic() + self._interval
        return super().make_request(method, params)


class FailoverProvider(BaseProvider):
    # ------------------------------------
    # One JSON-RPC entry point over several providers (RPC_URL first, then
    # RPC_FALLBACK_URLS). Sticky on the provider that works; a transport or quota error
    # (OSError, or a JSON-RPC error the node itself raised: HTTP 429, Alchemy's plan
    # limits) cools that provider for `cooldown` seconds and retries the SAME request on
    # the next one. A re-broadcast retries the identical signed bytes, so it can never
    # produce a second transaction, and a transaction is never rebuilt for a retry. When
    # every provider fails, the last error is raised unchanged, so the runner's halts
    # and backoffs see exactly what they saw before. Contract reverts never reach this
    # layer (they are raised from responses above it), so they cannot trigger a switch.
    # The message logs no error text on purpose: an RPC error can carry the URL and its
    # key, and not every caller has the redacting log handler set up.
    # ------------------------------------

    def __init__(self, providers, cooldown=60.0):
        super().__init__()
        self._providers = providers
        self._cooldown = cooldown
        self._active = 0
        self._until = [0.0] * len(providers)

    def __str__(self):
        return f"failover over {len(self._providers)} RPC providers"

    def _pick_next(self):
        # the first provider after the failed one that is not cooling down, wrapping
        # around; when all cool, the one that recovers first
        now = time.monotonic()
        n = len(self._providers)
        order = [(self._active + 1 + step) % n for step in range(n)]
        for i in order:
            if self._until[i] <= now:
                return i
        return min(order, key=lambda i: self._until[i])

    def make_request(self, method, params):
        failures = 0
        while True:
            try:
                return self._providers[self._active].make_request(method, params)
            except (OSError, Web3Exception) as e:
                if failures >= len(self._providers) - 1:
                    raise  # every provider has refused: nothing left to try
                failures += 1
                log.warning(
                    "rpc provider %d/%d failed on %s (%s); trying the next one",
                    self._active + 1,
                    len(self._providers),
                    method,
                    type(e).__name__,
                )
                self._until[self._active] = time.monotonic() + self._cooldown
                self._active = self._pick_next()

    def is_connected(self, show_traceback=False):
        return any(p.is_connected() for p in self._providers)


def rpc_provider(url):
    # ------------------------------------
    # The provider every connection goes through (load_market below and
    # bot_contract.connect_read_only), so the cap and the failover cover the loop and
    # every read-only script alike. Whatever the URLs hold, Infura or Alchemy: Sepolia
    # either way, which the chain-id checks confirm. Each provider gets its own
    # MAX_RPC_PER_SECOND cap, so each key is paced separately.
    # ------------------------------------

    providers = [ThrottledProvider(u, rpc_rate()) for u in [url] + fallback_urls()]
    if len(providers) == 1:
        return providers[0]
    return FailoverProvider(providers)


def read_token(w3, token_address):
    # ------------------------------------
    # 4. Read token metadata
    # ------------------------------------

    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=erc20_abi,
    )

    return token.functions.symbol().call(), token.functions.decimals().call()


def check_factories(w3, pool, router_address, quoter_address):
    # ------------------------------------
    # 5. Compare router/quoter factory with the pool's factory
    # ------------------------------------

    router = w3.eth.contract(
        address=Web3.to_checksum_address(router_address),
        abi=router_abi,
    )
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(quoter_address),
        abi=quoter_abi,
    )

    pool_factory = pool.functions.factory().call()
    router_factory = router.functions.factory().call()
    quoter_factory = quoter.functions.factory().call()

    print("\n=== FACTORY CHECK ===")
    print("Pool factory:  ", pool_factory)
    print("Router factory:", router_factory)
    print("Quoter factory:", quoter_factory)

    if not (pool_factory == router_factory == quoter_factory):
        raise ValueError(
            "Factory mismatch: pool is a custom deployment, "
            "ask for the matching router and quoter"
        )

    return quoter


def read_price(pool, decimals0, decimals1):
    # ------------------------------------
    # 6. Spot price from slot0 (display/strategy only, never for on-chain amounts)
    # ------------------------------------

    sqrt_price_x96, tick, *_ = pool.functions.slot0().call()
    price = (sqrt_price_x96 / 2**96) ** 2 * 10 ** (decimals0 - decimals1)

    print("\n=== PRICE ===")
    print("sqrtPriceX96:", sqrt_price_x96)
    print("Tick:", tick)
    print(f"Price (token1 per token0): {price:.6f}")
    print(f"Price (token0 per token1): {1 / price:.6f}")

    return price


MIN_TICK, MAX_TICK = -887272, 887272
# the liquidity worked out from the ticks may differ this much from the pool's own figure: a
# provider can add or pull liquidity between two of the reads below
DEPTH_TOLERANCE = 0.01


def read_depth(pool, decimals0, decimals1, block, pause=0.0, stop=None):
    # ------------------------------------
    # The pool's whole liquidity layout: every initialised tick and its net liquidity (the input of
    # v3pool). Read-only eth_calls, about 120 of them (one per 256 ticks, one per initialised
    # tick), no key and no transaction, `pause` seconds apart so a rate-limited RPC (HTTP 429) is
    # not hit with a burst. Raises ValueError if the result does not add up to the pool's own
    # current liquidity. Every call is made AT `block`, so the layout is one snapshot even though
    # reading it takes 30 to 40 s.
    # ------------------------------------

    def ask(call):
        if (
            stop is not None and stop()
        ):  # a stop signal: give up, the scan is only a read
            raise InterruptedError("stopped while reading the pool's layout")
        time.sleep(pause)
        return call.call(block_identifier=block)

    spacing = ask(pool.functions.tickSpacing())
    sqrt_price_x96, current_tick, *_ = ask(pool.functions.slot0())
    liquidity = ask(pool.functions.liquidity())
    ticks = []
    for word in range((MIN_TICK // spacing) >> 8, ((MAX_TICK // spacing) >> 8) + 1):
        bits = ask(pool.functions.tickBitmap(word))
        for bit in range(256):
            if bits >> bit & 1:
                tick = (word * 256 + bit) * spacing
                gross, net, *_ = ask(pool.functions.ticks(tick))
                ticks.append({"tick": tick, "gross": gross, "net": net})
    active = sum(t["net"] for t in ticks if t["tick"] <= current_tick)
    if abs(active - liquidity) > DEPTH_TOLERANCE * liquidity:
        raise ValueError(
            f"the ticks add up to {active} liquidity but the pool says {liquidity}: "
            "it changed while being read, try again"
        )
    return {
        "block": block,
        "tick": current_tick,
        "sqrtPriceX96": sqrt_price_x96,
        "liquidity": liquidity,
        "spacing": spacing,
        "fee": ask(pool.functions.fee()),
        "d0": decimals0,
        "d1": decimals1,
        "ticks": ticks,
    }


def quote_swap(quoter, token_in, token_out, fee, amount_in):
    # ------------------------------------
    # Quote (read-only): .call(), never a transaction
    # ------------------------------------

    amount_out, _sqrt_price_after, ticks_crossed, gas_estimate = (
        quoter.functions.quoteExactInputSingle(
            (
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                amount_in,
                fee,
                0,  # sqrtPriceLimitX96 = 0: no price limit
            )
        ).call()
    )

    return amount_out, ticks_crossed, gas_estimate


def load_market():
    # ------------------------------------
    # Connect and gather everything get_quote() needs
    # ------------------------------------

    load_dotenv()
    RPC_URL = os.environ["RPC_URL"]
    POOL_ADDRESS = os.environ["POOL_ADDRESS"]
    ROUTER_ADDRESS = os.environ["ROUTER_ADDRESS"]
    QUOTER_ADDRESS = os.environ["QUOTER_ADDRESS"]

    w3 = Web3(rpc_provider(RPC_URL))

    if not w3.is_connected():
        raise ConnectionError("Failed to connect to Ethereum RPC")

    print("Connected to Ethereum RPC!")

    chain_id = w3.eth.chain_id
    if chain_id != 11155111:
        raise ValueError(f"Wrong network! Expected Sepolia (11155111), got {chain_id}")

    print(f"Connected to Sepolia (Chain ID: {chain_id})")

    latest_block = w3.eth.block_number
    print(f"Latest block number: {latest_block}")

    pool = w3.eth.contract(
        address=Web3.to_checksum_address(POOL_ADDRESS),
        abi=pool_abi,
    )

    token0_address = pool.functions.token0().call()
    token1_address = pool.functions.token1().call()

    fee = pool.functions.fee().call()
    pool_factory = pool.functions.factory().call()
    tick_spacing = pool.functions.tickSpacing().call()
    liquidity = pool.functions.liquidity().call()

    print("\n=== POOL INFORMATION ===")
    print("Pool:", pool.address)
    print("Token0:", token0_address)
    print("Token1:", token1_address)
    print("Fee:", fee)
    print("Factory:", pool_factory)
    print("Tick spacing:", tick_spacing)
    print("Liquidity:", liquidity)

    if fee != 3000:
        raise ValueError(f"Unexpected fee tier: {fee}")

    if liquidity == 0:
        raise ValueError("Pool has zero in-range liquidity: not usable")

    symbol0, decimals0 = read_token(w3, token0_address)
    symbol1, decimals1 = read_token(w3, token1_address)

    print("\n=== TOKEN INFORMATION ===")
    print(f"Token0: {symbol0}, decimals={decimals0}")
    print(f"Token1: {symbol1}, decimals={decimals1}")

    quoter = check_factories(w3, pool, ROUTER_ADDRESS, QUOTER_ADDRESS)

    read_price(pool, decimals0, decimals1)

    return {
        "pool": pool,
        "quoter": quoter,
        "fee": fee,
        "tokens": {
            symbol0.lower(): (token0_address, decimals0, symbol0),
            symbol1.lower(): (token1_address, decimals1, symbol1),
        },
    }


def get_quote(market, token_in, amount):
    # ------------------------------------
    # Quote `amount` (human units) of `token_in` ("fETH" or "f564") for the other
    # token. Read-only. Returns a dict and prints a report.
    # ------------------------------------

    tokens = market["tokens"]
    key_in = token_in.lower()
    if key_in not in tokens:
        raise ValueError(f"Unknown token {token_in!r}, choose from: {sorted(tokens)}")
    key_out = next(k for k in tokens if k != key_in)

    addr_in, dec_in, sym_in = tokens[key_in]
    addr_out, dec_out, sym_out = tokens[key_out]
    fee = market["fee"]

    amount_in = int(Decimal(str(amount)) * 10**dec_in)
    if amount_in <= 0:
        raise ValueError("Amount must be positive")

    amount_out, ticks_crossed, gas_estimate = quote_swap(
        market["quoter"], addr_in, addr_out, fee, amount_in
    )

    # integer math for anything that goes on-chain
    amount_out_min = amount_out * (10_000 - SLIPPAGE_BPS) // 10_000

    out_human = Decimal(amount_out) / 10**dec_out
    min_human = Decimal(amount_out_min) / 10**dec_out

    # spot price (out per in), display only
    sqrt_price_x96 = market["pool"].functions.slot0().call()[0]
    price_1_per_0 = (sqrt_price_x96 / 2**96) ** 2
    t0_is_in = addr_in == market["pool"].functions.token0().call()
    spot = price_1_per_0 if t0_is_in else 1 / price_1_per_0
    spot *= 10 ** (dec_in - dec_out)

    fee_factor = Decimal(1_000_000 - fee) / 1_000_000
    expected = Decimal(str(amount)) * Decimal(spot) * fee_factor
    impact_pct = (expected - out_human) / expected * 100

    print(f"\n=== QUOTE: {sym_in} -> {sym_out} ===")
    print(f"Amount in:        {amount} {sym_in}")
    print(f"Amount out:       {out_human} {sym_out}")
    print(f"Min out ({SLIPPAGE_BPS} bps): {min_human} {sym_out}")
    print(f"Spot price:       {spot:.8f} {sym_out} per {sym_in}")
    print(f"Expected at spot: {expected:.8f} {sym_out} (after {fee / 10_000}% fee)")
    print(f"Price impact:     {impact_pct:.4f}%")
    print(f"Ticks crossed:    {ticks_crossed}")
    print(f"Gas estimate:     {gas_estimate}")

    too_large = impact_pct > MAX_PRICE_IMPACT_PCT
    if too_large:
        print(
            f"WARNING: impact exceeds {MAX_PRICE_IMPACT_PCT}%, too large for this pool"
        )

    return {
        "token_in": addr_in,
        "token_out": addr_out,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "amount_out_min": amount_out_min,
        "price_impact_pct": impact_pct,
        "too_large": too_large,
    }


def main():
    # usage: uv run python config.py [fETH|f564 AMOUNT]
    market = load_market()

    if len(sys.argv) == 3:
        get_quote(market, sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 1:
        max_trade_amount = os.environ["MAX_TRADE_AMOUNT"]
        for token_in in ("fETH", "f564"):
            get_quote(market, token_in, max_trade_amount)
    else:
        raise SystemExit("usage: config.py [fETH|f564 AMOUNT]")


if __name__ == "__main__":
    main()
