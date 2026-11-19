import json
import logging
import os
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from itertools import batched
from pathlib import Path

import requests
from web3 import Web3
from web3.exceptions import Web3Exception
from web3.providers import BaseProvider

from runtime import error_kind, rejected_log_range
from runtime import load_environment as load_dotenv

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
SUPPORTED_FEES = (100, 500, 3000, 10000)


def configured_pools():
    # The primary is the loop's execution pool; the secondary is for explicit research reads.
    pools = []
    for prefix, default_fee in (("POOL", 500), ("SECONDARY_POOL", 10000)):
        raw = os.environ.get(f"{prefix}_ADDRESS", "").strip()
        if not raw and prefix == "SECONDARY_POOL":
            continue
        if not Web3.is_address(raw) or int(raw, 16) == 0:
            raise ValueError(f"{prefix}_ADDRESS must be a nonzero Ethereum address")
        try:
            fee = int(os.environ.get(f"{prefix}_FEE", str(default_fee)))
        except ValueError:
            raise ValueError(
                f"{prefix}_FEE must be a supported integer fee tier"
            ) from None
        if fee not in SUPPORTED_FEES:
            raise ValueError(f"{prefix}_FEE must be one of {SUPPORTED_FEES}")
        pools.append((Web3.to_checksum_address(raw), fee))
    if len({p[0] for p in pools}) != len(pools) or len({p[1] for p in pools}) != len(
        pools
    ):
        raise ValueError(
            "Primary and secondary pools must have different addresses and fees"
        )
    return pools


def market_tokens():
    tokens = {}
    for symbol in ("feth", "f564"):
        name = f"{symbol.upper()}_ADDRESS"
        raw = os.environ.get(name, "")
        if not Web3.is_address(raw) or int(raw, 16) == 0:
            raise ValueError(f"{name} must be a nonzero Ethereum address")
        tokens[symbol] = Web3.to_checksum_address(raw)
    if tokens["feth"] == tokens["f564"]:
        raise ValueError("FETH_ADDRESS and F564_ADDRESS must differ")
    return tokens


def quote_per_base(sqrt_price_x96, decimals0, decimals1, base_is_token0):
    price = (sqrt_price_x96 / 2**96) ** 2 * 10 ** (decimals0 - decimals1)
    return price if base_is_token0 else 1 / price


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


# Alchemy meters throughput in compute units, not requests: one eth_getLogs costs as
# much as three eth_calls. A request-count cap alone lets a getLogs-heavy burst blow
# past the plan's CU/s ceiling (observed 560/300 CU/s on Sepolia = the 429 storms).
# Published EVM costs: https://www.alchemy.com/docs/reference/compute-unit-costs
CU_COSTS = {
    "eth_getLogs": 60,
    "eth_call": 26,
    "eth_sendRawTransaction": 40,
    "eth_callMany": 20,
    "eth_estimateGas": 20,
    "eth_gasPrice": 20,
    "eth_getBalance": 20,
    "eth_getBlockByHash": 20,
    "eth_getBlockByNumber": 20,
    "eth_getCode": 20,
    "eth_getStorageAt": 20,
    "eth_getTransactionByHash": 20,
    "eth_getTransactionCount": 20,
    "eth_getTransactionReceipt": 20,
    "alchemy_getTokenBalances": 20,
    "eth_feeHistory": 10,
    "eth_blockNumber": 10,
    "eth_maxPriorityFeePerGas": 10,
    "eth_chainId": 0,
}
DEFAULT_CU = 20  # unknown methods: a mid-weight read, never a free pass


def cu_rate():
    # ------------------------------------
    # MAX_COMPUTE_UNITS_PER_SECOND: pace the bot under the provider's compute-unit
    # throughput ceiling (Alchemy free tier is 300 CU/s; ~250 leaves headroom for
    # the dashboard and manual commands). 0 or unset means no CU pacing, and
    # MAX_RPC_PER_SECOND keeps applying on its own. Each request reserves its
    # methods' published CU cost: a getLogs at 250 CU/s paces the next call 0.24 s
    # out, a blockNumber only 0.04 s.
    # ------------------------------------

    try:
        rate = Decimal(os.environ.get("MAX_COMPUTE_UNITS_PER_SECOND", "0"))
    except InvalidOperation:
        rate = Decimal("nan")
    if not rate.is_finite() or rate < 0:
        raise SystemExit(
            "MAX_COMPUTE_UNITS_PER_SECOND must be a positive number of compute units"
            " per second, or 0 (no cap)"
        )
    return rate or None


def fallback_urls():
    # ------------------------------------
    # RPC_FALLBACK_URLS from the environment: optional comma-separated URLs used when the
    # primary (RPC_URL) errors. Unset or empty means one provider, the old behavior.
    # ------------------------------------

    raw = os.environ.get("RPC_FALLBACK_URLS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


class RpcAdapter(requests.adapters.HTTPAdapter):
    """Pace and count every HTTP attempt, underneath Web3's cache and retry loop."""

    def __init__(self, rate, metrics, cu_rate=None):
        super().__init__(max_retries=0)
        self.interval = float(1 / rate) if rate else 0.0
        self.cu_budget = float(cu_rate) if cu_rate else 0.0
        self.metrics = metrics
        self.next_at = 0.0
        self.last_at = 0.0
        self.cu_next_at = 0.0
        self.pacing_lock = threading.Lock()

    def send(self, request, **kwargs):
        if request.method != "POST":
            return super().send(request, **kwargs)
        payload = json.loads(request.body)
        batch = isinstance(payload, list)
        calls = payload if batch else [payload]
        count = len(calls)
        cost = sum(CU_COSTS.get(c.get("method", ""), DEFAULT_CU) for c in calls)
        # Diagnostic reads share the quota with execution. Lock only pacing;
        # a slow diagnostic HTTP response must never hold the execution thread.
        with self.pacing_lock:
            now = time.monotonic()
            wait = self.next_at - now
            if self.interval and count > 1 and self.next_at:
                # Start a fresh quota window for a batch, including after scalar reads.
                wait = max(wait, self.last_at + 1 - now)
            if self.cu_budget and self.cu_next_at:
                # Every request reserves its compute units before it is sent, so a
                # burst of heavy methods cannot pass the provider's CU/s ceiling.
                wait = max(wait, self.cu_next_at - now)
            if wait > 0:
                time.sleep(wait)
            self.last_at = time.monotonic()
            self.next_at = self.last_at + (
                1 if count > 1 and self.interval else self.interval
            )
            if self.cu_budget:
                self.cu_next_at = self.last_at + cost / self.cu_budget
        if self.metrics is not None:
            self.metrics.attempt(
                count, method=None if batch else calls[0].get("method")
            )
        return super().send(request, **kwargs)


class ThrottledProvider(Web3.HTTPProvider):
    # ------------------------------------
    # HTTPProvider that spaces the bot's JSON-RPC calls so they never exceed
    # MAX_RPC_PER_SECOND: without it a burst is possible (the depth scan fires about 120
    # reads back to back, though already DEPTH_CALL_PAUSE apart). The pacing adapter
    # is shared by execution and background diagnostics; None means no throttling.
    # ------------------------------------

    def __init__(self, url, rate=None, metrics=None, cu_rate=None, **kwargs):
        session = requests.Session()
        adapter = RpcAdapter(rate, metrics, cu_rate)
        for scheme in ("http://", "https://"):
            session.mount(scheme, adapter)
        super().__init__(url, session=session, **kwargs)
        self.batch_size = min(20, max(1, int(rate))) if rate else 20

    def depth_calls(self, params):
        # All items are read-only, numeric-block eth_calls. Match IDs explicitly.
        if not 1 <= len(params) <= self.batch_size:
            raise ValueError("liquidity batch exceeds provider limit")
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": p}
            for i, p in enumerate(params)
        ]
        reply = json.loads(
            self._request_session_manager.make_post_request(
                self.endpoint_uri,
                json.dumps(payload).encode(),
                **self.get_request_kwargs(),
            )
        )
        if not isinstance(reply, list) or len(reply) != len(params):
            raise ValueError("incomplete liquidity batch")
        if any(
            not isinstance(r, dict) or type(r.get("id")) is not int or "error" in r
            for r in reply
        ):
            raise ValueError("invalid liquidity batch response")
        results = {r["id"]: r.get("result") for r in reply}
        if set(results) != set(range(len(params))) or not all(
            isinstance(v, str) for v in results.values()
        ):
            raise ValueError("invalid liquidity batch response")
        return [
            bytes.fromhex(results[i].removeprefix("0x")) for i in range(len(params))
        ]


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
        self._selection_lock = threading.Lock()

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
        return self._request(
            method, lambda provider: provider.make_request(method, params)
        )

    @property
    def batch_size(self):
        return min(p.batch_size for p in self._providers)

    def depth_calls(self, params):
        return self._request(
            "eth_call batch", lambda provider: provider.depth_calls(params)
        )

    def _request(self, method, send):
        failures = 0
        range_error = None
        while True:
            with self._selection_lock:
                selected = self._active
            try:
                return send(self._providers[selected])
            except (OSError, Web3Exception) as e:
                if failures >= len(self._providers) - 1:
                    if range_error is not None and range_error is not e:
                        # Preserve both outcomes: the last provider may be rate-limited
                        # while an earlier provider could serve a narrower log range.
                        raise e from range_error
                    raise  # every provider has refused: nothing left to try
                if method == "eth_getLogs" and rejected_log_range(e):
                    range_error = e
                failures += 1
                log.warning(
                    "rpc provider %d/%d failed on %s (%s); trying the next one",
                    selected + 1,
                    len(self._providers),
                    method,
                    error_kind(e),
                )
                with self._selection_lock:
                    self._until[selected] = time.monotonic() + self._cooldown
                    if self._active == selected:
                        self._active = self._pick_next()

    def is_connected(self, show_traceback=False):
        return any(p.is_connected() for p in self._providers)


def rpc_provider(url, metrics=None):
    # ------------------------------------
    # The provider every connection goes through (load_market below and
    # bot_contract.connect_read_only), so the cap and the failover cover the loop and
    # every read-only script alike. Whatever the URLs hold, Infura or Alchemy: Sepolia
    # either way, which the chain-id checks confirm. Each provider gets its own
    # MAX_RPC_PER_SECOND cap, so each key is paced separately.
    # ------------------------------------

    providers = [
        ThrottledProvider(u, rpc_rate(), metrics, cu_rate())
        for u in [url] + fallback_urls()
    ]
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


def read_depth(
    pool, decimals0, decimals1, block, pause=0.0, stop=None, *, batch_provider=None
):
    # ------------------------------------
    # The pool's whole liquidity layout: every initialised tick and its net liquidity (the input of
    # v3pool). Read-only eth_calls, about 120 of them (one per 256 ticks, one per initialised
    # tick), no key and no transaction, `pause` seconds apart so a rate-limited RPC (HTTP 429) is
    # not hit with a burst. Raises ValueError if the result does not add up to the pool's own
    # current liquidity. Every call is made AT `block`, so the layout is one snapshot even though
    # reading it takes 30 to 40 s.
    # ------------------------------------

    def check_stop():
        if (
            stop is not None and stop()
        ):  # a stop signal: give up, the scan is only a read
            raise InterruptedError("stopped while reading the pool's layout")

    def ask(call):
        check_stop()
        if pause:
            time.sleep(pause)
        return call.call(block_identifier=block)

    def read_many(name, keys):
        size = batch_provider.batch_size if batch_provider else 1
        for group in batched(keys, size):
            check_stop()
            fn = getattr(pool.functions, name)
            if size == 1:
                yield group[0], ask(fn(group[0]))
                continue
            params = [
                [
                    {"to": pool.address, "data": pool.encode_abi(name, args=[k])},
                    hex(block),
                ]
                for k in group
            ]
            outputs = [o["type"] for o in fn.abi["outputs"]]
            for key, data in zip(
                group, batch_provider.depth_calls(params), strict=True
            ):
                values = pool.w3.codec.decode(outputs, data)
                yield key, values[0] if len(values) == 1 else values

    spacing = ask(pool.functions.tickSpacing())
    sqrt_price_x96, current_tick, *_ = ask(pool.functions.slot0())
    liquidity = ask(pool.functions.liquidity())
    ticks = []
    words = range((MIN_TICK // spacing) >> 8, ((MAX_TICK // spacing) >> 8) + 1)
    initialized = [
        (word * 256 + bit) * spacing
        for word, bits in read_many("tickBitmap", words)
        for bit in range(256)
        if bits >> bit & 1
    ]
    for tick, (gross, net, *_) in read_many("ticks", initialized):
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


def quote_swap(quoter, token_in, token_out, fee, amount_in, block_identifier="latest"):
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
        ).call(block_identifier=block_identifier)
    )

    return amount_out, ticks_crossed, gas_estimate


def load_market(pool_index=0, w3=None):
    # ------------------------------------
    # Connect and gather everything get_quote() needs
    # ------------------------------------

    load_dotenv()
    RPC_URL = os.environ["RPC_URL"]
    pools = configured_pools()
    if not 0 <= pool_index < len(pools):
        raise ValueError("SECONDARY_POOL_ADDRESS is not configured")
    POOL_ADDRESS, expected_fee = pools[pool_index]
    expected_tokens = market_tokens()
    ROUTER_ADDRESS = os.environ["ROUTER_ADDRESS"]
    QUOTER_ADDRESS = os.environ["QUOTER_ADDRESS"]

    w3 = w3 or Web3(rpc_provider(RPC_URL))

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

    if fee != expected_fee:
        raise ValueError(f"Pool fee {fee} does not match configured fee {expected_fee}")
    if {token0_address.lower(), token1_address.lower()} != {
        address.lower() for address in expected_tokens.values()
    }:
        raise ValueError("Pool tokens do not match FETH_ADDRESS / F564_ADDRESS")

    if liquidity == 0:
        raise ValueError("Pool has zero in-range liquidity: not usable")

    symbol0, decimals0 = read_token(w3, token0_address)
    symbol1, decimals1 = read_token(w3, token1_address)

    print("\n=== TOKEN INFORMATION ===")
    print(f"Token0: {symbol0}, decimals={decimals0}")
    print(f"Token1: {symbol1}, decimals={decimals1}")

    quoter = check_factories(w3, pool, ROUTER_ADDRESS, QUOTER_ADDRESS)

    read_price(pool, decimals0, decimals1)

    by_address = {
        token0_address.lower(): (token0_address, decimals0, symbol0),
        token1_address.lower(): (token1_address, decimals1, symbol1),
    }
    return {
        "w3": w3,
        "pool": pool,
        "quoter": quoter,
        "fee": fee,
        "tokens": {
            key: by_address[address.lower()] for key, address in expected_tokens.items()
        },
    }


def get_quote(market, token_in, amount, block_identifier="latest"):
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
        market["quoter"], addr_in, addr_out, fee, amount_in, block_identifier
    )

    # integer math for anything that goes on-chain
    amount_out_min = amount_out * (10_000 - SLIPPAGE_BPS) // 10_000

    out_human = Decimal(amount_out) / 10**dec_out
    min_human = Decimal(amount_out_min) / 10**dec_out

    # spot price (out per in), display only
    sqrt_price_x96 = (
        market["pool"].functions.slot0().call(block_identifier=block_identifier)[0]
    )
    price_1_per_0 = (sqrt_price_x96 / 2**96) ** 2
    t0_is_in = addr_in == market["pool"].functions.token0().call()
    spot = price_1_per_0 if t0_is_in else 1 / price_1_per_0
    spot *= 10 ** (dec_in - dec_out)

    fee_factor = Decimal(1_000_000 - fee) / 1_000_000
    expected = Decimal(str(amount)) * Decimal(spot) * fee_factor
    impact_pct = (expected - out_human) / expected * 100

    print(f"\n=== QUOTE: {sym_in} -> {sym_out} ===")
    print(f"Pool:             {market['pool'].address} (fee {fee})")
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
        "pool_address": market["pool"].address,
        "fee": fee,
        "token_in": addr_in,
        "token_out": addr_out,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "amount_out_min": amount_out_min,
        "price_impact_pct": impact_pct,
        "too_large": too_large,
    }


def main():
    # --all-pools compares executable quotes at one block; never sends a transaction.
    args = sys.argv[1:]
    all_pools = bool(args and args[0] == "--all-pools")
    if all_pools:
        args = args[1:]
    if len(args) not in (0, 2) or (all_pools and len(args) != 2):
        raise SystemExit("usage: config.py [--all-pools] [fETH|f564 AMOUNT]")
    market = load_market()
    if all_pools:
        markets = [market] + [
            load_market(i, market["w3"]) for i in range(1, len(configured_pools()))
        ]
        block = market["w3"].eth.block_number
        quotes = [get_quote(m, args[0], args[1], block) for m in markets]
        best = max(quotes, key=lambda q: q["amount_out"])
        print(
            f"\nBest output at block {block}: pool={best['pool_address']} fee={best['fee']}"
        )
    elif args:
        get_quote(market, args[0], args[1])
    else:
        max_trade_amount = os.environ["MAX_TRADE_AMOUNT"]
        for token_in in ("fETH", "f564"):
            get_quote(market, token_in, max_trade_amount)


if __name__ == "__main__":
    # Provider exceptions may contain credentials, including fallback URLs.
    import traceback

    from legacy_main import redact

    try:
        main()
    except Exception:  # noqa: BLE001 - redact provider errors before displaying them
        print(redact(traceback.format_exc()), file=sys.stderr)
        raise SystemExit(1) from None
