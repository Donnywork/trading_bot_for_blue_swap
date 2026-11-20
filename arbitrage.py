"""Read-only cycle planning. No signing, sending, dotenv loading, or automatic execution."""

from dataclasses import dataclass, field
from math import isqrt

MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


@dataclass
class CycleContext:
    """One ladder attempt only; never retain across candidates or retries."""

    block: int
    block_hash: str
    header: dict | None = None
    # The event executor validates these immutable fields at startup.
    vault_identity: tuple | None = None
    estimate_gas: bool = True
    prices: dict = field(default_factory=dict)
    w3: object = None


def same_hash(left, right):
    def text(value):
        return (value if isinstance(value, str) else value.hex()).removeprefix("0x")

    return text(left).lower() == text(right).lower()


def canonical(w3, block, block_hash):
    header = w3.eth.get_block(block)
    if header["number"] != block or not same_hash(header["hash"], block_hash):
        raise ValueError("quote block reorganized")
    return header


def price_limit(sqrt_price, zero_for_one, move_bps):
    # Bound movement in the pool's raw token1/token0 price with integer arithmetic.
    if type(move_bps) is not int or not 1 <= move_bps <= 500:
        raise ValueError("price move must be 1..500 basis points")
    if type(sqrt_price) is not int or not MIN_SQRT_RATIO < sqrt_price < MAX_SQRT_RATIO:
        raise ValueError("pool price is outside V3 bounds")
    numerator = sqrt_price**2 * (10000 - move_bps if zero_for_one else 10000 + move_bps)
    limit = isqrt(numerator // 10000)
    if zero_for_one and limit**2 * 10000 < numerator:
        limit += (
            1  # round inward, so the permitted movement is never larger than requested
        )
    limit = min(MAX_SQRT_RATIO - 1, max(MIN_SQRT_RATIO + 1, limit))
    if (zero_for_one and limit >= sqrt_price) or (
        not zero_for_one and limit <= sqrt_price
    ):
        raise ValueError("no room inside the price limit")
    return limit


def quote_cycle(
    buy_market,
    sell_market,
    amount_in,
    min_profit,
    max_base_in,
    slippage_bps=50,
    move_bps=100,
    ttl=60,
    context=None,
):
    # Markets come from config.load_market(), which validates tokens, fees and factories.
    # Two distinct pools make independent same-block quotes equivalent to the ordered cycle.
    for value in (amount_in, min_profit, max_base_in):
        if type(value) is not int or value <= 0:
            raise ValueError(
                "amount, profit and base cap must be positive raw integers"
            )
    if type(slippage_bps) is not int or not 0 <= slippage_bps <= 500:
        raise ValueError("slippage must be 0..500 basis points")
    if type(ttl) is not int or not 1 <= ttl <= 120:
        raise ValueError("quote lifetime must be 1..120 seconds")
    if (
        buy_market["pool"].address.lower() == sell_market["pool"].address.lower()
        or buy_market["fee"] == sell_market["fee"]
    ):
        raise ValueError("cycle needs two distinct pools and fee tiers")
    for name in ("f564", "feth"):
        if (
            buy_market["tokens"][name][0].lower()
            != sell_market["tokens"][name][0].lower()
        ):
            raise ValueError("pool token mismatch")
    quote = buy_market["tokens"]["f564"][0]
    base = buy_market["tokens"]["feth"][0]
    w3 = buy_market["w3"]
    if context is None or context.w3 is None:
        if w3.eth.chain_id != 11155111:
            raise ValueError("expected Sepolia")
        if context is not None:
            context.w3 = w3
    elif context.w3 is not w3:
        raise ValueError("cycle context belongs to another connection")
    if context is None:
        block = w3.eth.get_block("latest")
    else:
        if context.header is None:
            context.header = canonical(w3, context.block, context.block_hash)
        block = context.header
        if block["number"] != context.block or not same_hash(
            block["hash"], context.block_hash
        ):
            raise ValueError("cycle context block mismatch")
    number = block["number"]
    prices = {} if context is None else context.prices

    def leg(market, token_in, token_out, amount):
        address = market["pool"].address.lower()
        if address not in prices:
            prices[address] = (
                market["pool"].functions.slot0().call(block_identifier=number)[0]
            )
        sqrt_price = prices[address]
        limit = price_limit(
            sqrt_price, int(token_in, 16) < int(token_out, 16), move_bps
        )
        out, after, ticks, gas = (
            market["quoter"]
            .functions.quoteExactInputSingle(
                (token_in, token_out, amount, market["fee"], limit)
            )
            .call(block_identifier=number)
        )
        # Quoter does not return input consumed. Reaching the limit may be a partial fill;
        # reject even the ambiguous case of an exact fill ending exactly on the boundary.
        if after == limit or out <= 0:
            raise ValueError("quote reached price limit or returned no output")
        return out, limit, ticks, gas

    base_out, buy_limit, buy_ticks, buy_gas = leg(buy_market, quote, base, amount_in)
    if base_out > max_base_in:
        raise ValueError("quoted base exceeds vault cap")
    quote_out, sell_limit, sell_ticks, sell_gas = leg(
        sell_market, base, quote, base_out
    )
    min_base = base_out * (10000 - slippage_bps) // 10000
    # Slippage is a maximum tolerance: tighten it when needed to preserve the
    # atomic profit floor, rather than rejecting an otherwise executable cycle.
    min_quote = max(quote_out * (10000 - slippage_bps) // 10000, amount_in + min_profit)
    if min_base <= 0 or quote_out < amount_in + min_profit:
        raise ValueError("insufficient f564 profit")
    # A shared ladder is canonical-checked as a whole before/after simulation.
    if context is None:
        canonical(w3, number, block["hash"])
    # buyPool0 is supplied only after matching this plan's addresses against the deployed vault.
    return {
        "block": number,
        "block_hash": block["hash"].hex(),
        "buy_pool": buy_market["pool"].address,
        "sell_pool": sell_market["pool"].address,
        "quote_token": quote,
        "base_token": base,
        "buy_fee": buy_market["fee"],
        "sell_fee": sell_market["fee"],
        "amount_in": amount_in,
        "base_out": base_out,
        "quote_out": quote_out,
        "profit": quote_out - amount_in,
        "min_base_out": min_base,
        "min_quote_out": min_quote,
        "min_profit": min_profit,
        "deadline": block["timestamp"] + ttl,
        "buy_limit": buy_limit,
        "sell_limit": sell_limit,
        "ticks_crossed": [buy_ticks, sell_ticks],
        "quoter_gas_estimates": [buy_gas, sell_gas],
    }


def simulate_cycle(w3, vault, operator, plan, context=None):
    # Owner limits remain fresh per attempt; only startup-validated immutable identity
    # may be supplied by the event executor. Standalone callers retain all checks.
    if context is None or context.w3 is None:
        if w3.eth.chain_id != 11155111:
            raise ValueError("expected Sepolia")
    elif context.w3 is not w3:
        raise ValueError("cycle context belongs to another connection")
    pinned = "block" in plan
    if context is not None and (
        plan.get("block") != context.block
        or not same_hash(plan.get("block_hash", ""), context.block_hash)
    ):
        raise ValueError("cycle context block mismatch")
    if pinned:
        canonical(w3, plan["block"], plan["block_hash"])
    call = lambda fn: fn.call(block_identifier=plan["block"]) if pinned else fn.call()
    identity = context.vault_identity if context is not None else None
    if identity is None:
        quote, base = (
            call(vault.functions.quoteToken()),
            call(vault.functions.baseToken()),
        )
        p0, p1 = call(vault.functions.pool0()), call(vault.functions.pool1())
    else:
        address, quote, base, p0, p1 = identity
        if address.lower() != vault.address.lower():
            raise ValueError("cycle context vault mismatch")
    if (
        quote.lower() != plan["quote_token"].lower()
        or base.lower() != plan["base_token"].lower()
    ):
        raise ValueError("vault token mismatch")
    buy0 = plan["buy_pool"].lower() == p0.lower()
    if (plan["buy_pool"].lower(), plan["sell_pool"].lower()) != tuple(
        p.lower() for p in ((p0, p1) if buy0 else (p1, p0))
    ):
        raise ValueError("vault pool mismatch")
    if plan["amount_in"] > call(vault.functions.maxQuoteIn()) or plan[
        "base_out"
    ] > call(vault.functions.maxBaseIn()):
        raise ValueError("vault cap exceeded")
    if plan["min_profit"] < call(vault.functions.minProfitFloor()):
        raise ValueError("vault profit floor exceeds plan")
    args = (
        buy0,
        plan["amount_in"],
        plan["min_base_out"],
        plan["min_quote_out"],
        plan["min_profit"],
        plan["deadline"],
        plan["buy_limit"],
        plan["sell_limit"],
    )
    fn = vault.functions.execute(args)
    profit = (
        fn.call({"from": operator}, block_identifier=plan["block"])
        if pinned
        else fn.call({"from": operator})
    )
    if pinned:
        canonical(w3, plan["block"], plan["block_hash"])
    # Event submission estimates at latest once, with freshness checks on either side.
    gas = (
        fn.estimate_gas({"from": operator})
        if context is None or context.estimate_gas
        else None
    )
    return {"profit": profit, "estimated_gas": gas, "args": args}
