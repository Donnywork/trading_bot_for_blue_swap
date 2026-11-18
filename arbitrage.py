"""Read-only cycle planning. No signing, sending, dotenv loading, or automatic execution."""

from math import isqrt

MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


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
    if w3.eth.chain_id != 11155111:
        raise ValueError("expected Sepolia")
    block = w3.eth.get_block("latest")
    number = block["number"]

    def leg(market, token_in, token_out, amount):
        sqrt_price = market["pool"].functions.slot0().call(block_identifier=number)[0]
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
    min_quote = quote_out * (10000 - slippage_bps) // 10000
    if min_base <= 0 or min_quote < amount_in + min_profit:
        raise ValueError("insufficient f564 profit after slippage allowance")
    if w3.eth.get_block(number)["hash"] != block["hash"]:
        raise ValueError("quote block reorganized")
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


def simulate_cycle(w3, vault, operator, plan):
    # Re-check immutable route and owner limits on the actual deployed executor before eth_call.
    call = lambda fn: fn.call()
    if w3.eth.chain_id != 11155111:
        raise ValueError("expected Sepolia")
    if (
        call(vault.functions.quoteToken()).lower() != plan["quote_token"].lower()
        or call(vault.functions.baseToken()).lower() != plan["base_token"].lower()
    ):
        raise ValueError("vault token mismatch")
    p0, p1 = call(vault.functions.pool0()), call(vault.functions.pool1())
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
    profit = fn.call({"from": operator})
    gas = fn.estimate_gas({"from": operator})
    return {"profit": profit, "estimated_gas": gas, "args": args}
