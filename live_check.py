import os
from decimal import Decimal

from solcx.exceptions import SolcError, SolcInstallationError
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

import bot_contract
import config

ZERO_ADDRESS = "0x" + "00" * 20
# addresses that are neither operator nor owner, and a token the owner never allowed
STRANGER = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
UNLISTED_TOKEN = Web3.to_checksum_address("0x000000000000000000000000000000000000bEEF")
UNLISTED_FEE = 10000
# how a node reports an eth_call that reverts (tests add the in-memory chain's own error type)
REJECTIONS = (ContractLogicError, Web3RPCError)


class Report:
    # ------------------------------------
    # PASS / FAIL / WARN / SKIP lines. Only FAIL makes the check fail.
    # ------------------------------------

    def __init__(self):
        self.counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}

    def add(self, status, name, detail=""):
        self.counts[status] += 1
        print(f"[{status}] {name}" + (f": {detail}" if detail else ""))

    @property
    def failed(self):
        return self.counts["FAIL"] > 0

    def summary(self):
        c = self.counts
        print(
            f"\n{c['PASS']} passed, {c['FAIL']} failed, {c['WARN']} warnings, "
            f"{c['SKIP']} skipped"
        )


def expect_revert(report, name, fn, caller, reason):
    # eth_call from `caller`: it must be rejected, and for the right reason. If it would
    # SUCCEED, a guard is missing on the real deployment.
    try:
        fn.call({"from": caller})
    except REJECTIONS as e:
        if reason in str(e):
            report.add("PASS", name, f"rejected with {reason!r}")
        else:
            report.add("WARN", name, f"rejected, but not with {reason!r}: {e}")
    else:
        report.add("FAIL", name, "the call SUCCEEDED but must be rejected")


def check_state(
    report, w3, bot, operator, router, owner, fee, tokens, max_trade, min_eth
):
    # ------------------------------------
    # Is the deployed contract the one we think it is, with the right roles and limits?
    # ------------------------------------

    try:
        matches = bot_contract.deployed_code_matches(w3, bot.address)
    except (
        SolcError,
        SolcInstallationError,
        OSError,
    ) as e:  # e.g. solc is not available here
        report.add(
            "WARN", "code matches contracts/TradingBot.sol", f"could not compile: {e}"
        )
    else:
        if matches:
            report.add("PASS", "code matches contracts/TradingBot.sol")
        else:
            report.add(
                "FAIL",
                "code matches contracts/TradingBot.sol",
                "different code: an older version? redeploy (docs/DEPLOY.md part 2)",
            )

    on_chain_owner = bot.functions.owner().call()
    if owner is None:
        report.add(
            "WARN",
            "owner()",
            f"{on_chain_owner} (OWNER_ADDRESS not set, nothing to compare)",
        )
    elif on_chain_owner == owner:
        report.add("PASS", "owner() is OWNER_ADDRESS", on_chain_owner)
    else:
        report.add("FAIL", "owner() is OWNER_ADDRESS", f"{on_chain_owner} != {owner}")

    on_chain_operator = bot.functions.operator().call()
    if on_chain_operator == operator:
        report.add("PASS", "operator() is the PRIVATE_KEY wallet", operator)
    else:
        report.add(
            "FAIL",
            "operator() is the PRIVATE_KEY wallet",
            f"{on_chain_operator} != {operator}",
        )

    pending = bot.functions.pendingOwner().call()
    if pending == ZERO_ADDRESS:
        report.add("PASS", "no ownership transfer pending")
    else:
        report.add("WARN", "ownership transfer pending", pending)

    on_chain_router = bot.functions.router().call()
    if on_chain_router == Web3.to_checksum_address(router):
        report.add("PASS", "router() is ROUTER_ADDRESS", on_chain_router)
    else:
        report.add(
            "FAIL", "router() is ROUTER_ADDRESS", f"{on_chain_router} != {router}"
        )

    if bot.functions.feeAllowed(fee).call():
        report.add("PASS", f"fee tier {fee} is allowed")
    else:
        report.add(
            "FAIL", f"fee tier {fee} is allowed", "the bot cannot trade this pool"
        )

    for address, decimals, symbol in tokens:
        cap = bot.functions.tokenCap(address).call()
        expected = int(max_trade * 10**decimals)
        if cap == expected:
            report.add(
                "PASS",
                f"{symbol} cap is MAX_TRADE_AMOUNT",
                f"{Decimal(cap) / 10**decimals}",
            )
        elif cap == 0:
            report.add(
                "FAIL", f"{symbol} is allowed", "cap is 0: the bot cannot trade it"
            )
        else:
            report.add(
                "WARN",
                f"{symbol} cap differs from MAX_TRADE_AMOUNT",
                f"on-chain {Decimal(cap) / 10**decimals} vs {max_trade} (owner changed it?)",
            )

    eth = Decimal(w3.eth.get_balance(operator)) / 10**18
    if eth >= min_eth:
        report.add("PASS", "operator has gas", f"{eth} ETH")
    else:
        report.add(
            "WARN", "operator has gas", f"{eth} ETH is below {min_eth}: top it up"
        )


def check_guards(report, bot, operator, fee, tokens, stranger):
    # ------------------------------------
    # The limits that protect the vault, tested on the live contract with eth_call (nothing is
    # sent): who is refused, and what is refused.
    # ------------------------------------

    (in_addr, _, _), (out_addr, _, _) = tokens[0], tokens[1]
    in_cap = bot.functions.tokenCap(in_addr).call()
    amount = max(1, in_cap)

    def swap(
        token_in=in_addr, token_out=out_addr, swap_fee=fee, amount_in=amount, min_out=1
    ):
        return bot.functions.swap(token_in, token_out, swap_fee, amount_in, min_out)

    expect_revert(report, "a stranger cannot swap", swap(), stranger, "not operator")
    expect_revert(
        report,
        "operator cannot swap into an unlisted token",
        swap(token_out=UNLISTED_TOKEN),
        operator,
        "tokenOut not allowed",
    )
    if bot.functions.feeAllowed(UNLISTED_FEE).call():
        report.add(
            "SKIP", f"operator cannot use fee tier {UNLISTED_FEE}", "owner allowed it"
        )
    else:
        expect_revert(
            report,
            f"operator cannot use fee tier {UNLISTED_FEE}",
            swap(swap_fee=UNLISTED_FEE),
            operator,
            "fee not allowed",
        )
    if in_cap > 0:
        expect_revert(
            report,
            "operator cannot swap more than the cap",
            swap(amount_in=in_cap + 1),
            operator,
            "amountIn out of range",
        )
    expect_revert(
        report,
        "operator cannot set minOut to 0",
        swap(min_out=0),
        operator,
        "minOut is zero",
    )

    expect_revert(
        report,
        "operator cannot withdraw",
        bot.functions.withdraw(in_addr, 1),
        operator,
        "not owner",
    )
    expect_revert(
        report,
        "operator cannot change the operator",
        bot.functions.setOperator(operator),
        operator,
        "not owner",
    )
    expect_revert(
        report,
        "operator cannot raise a cap",
        bot.functions.setTokenCap(in_addr, 10**30),
        operator,
        "not owner",
    )


def check_swaps(report, w3, bot, operator, fee, tokens, max_trade, quote_fn=None):
    # ------------------------------------
    # Simulate a real swap in each direction through the contract (eth_call, free). Needs the
    # vault to hold the input token.
    # ------------------------------------

    for (in_addr, in_dec, in_sym), (out_addr, _, out_sym) in (
        (tokens[0], tokens[1]),
        (tokens[1], tokens[0]),
    ):
        name = f"simulated swap {in_sym} -> {out_sym}"
        cap = bot.functions.tokenCap(in_addr).call()
        amount = min(int(max_trade * 10**in_dec), cap)
        balance = (
            bot_contract.token_contract(w3, in_addr)
            .functions.balanceOf(bot.address)
            .call()
        )
        if amount == 0:
            report.add("SKIP", name, f"{in_sym} is not allowed")
        elif balance < amount:
            need = Decimal(amount) / 10**in_dec
            report.add(
                "SKIP",
                name,
                f"fund the contract with at least {need} {in_sym} to test this",
            )
        else:
            min_out = quote_fn(in_addr, out_addr, amount) if quote_fn else 1
            fn = bot.functions.swap(in_addr, out_addr, fee, amount, max(min_out, 1))
            try:
                out = fn.call({"from": operator})
            except REJECTIONS as e:
                report.add("FAIL", name, str(e))
            else:
                report.add(
                    "PASS",
                    name,
                    f"{Decimal(amount) / 10**in_dec} {in_sym} -> {out} (raw)",
                )


def check_deployment(
    w3,
    bot,
    *,
    operator,
    router,
    owner,
    fee,
    tokens,
    max_trade,
    min_eth,
    quote_fn=None,
    stranger=STRANGER,
):
    # tokens: [(address, decimals, symbol), (address, decimals, symbol)]
    report = Report()
    if not w3.eth.get_code(bot.address):
        report.add("FAIL", "a contract exists at BOT_CONTRACT_ADDRESS", bot.address)
        return report
    report.add("PASS", "a contract exists at BOT_CONTRACT_ADDRESS", bot.address)

    check_state(
        report, w3, bot, operator, router, owner, fee, tokens, max_trade, min_eth
    )
    check_guards(report, bot, operator, fee, tokens, stranger)
    check_swaps(report, w3, bot, operator, fee, tokens, max_trade, quote_fn)
    return report


def main():
    # ------------------------------------
    # Read-only check of the contract you deployed on Sepolia. Sends NO transaction.
    # usage: uv run python live_check.py
    # ------------------------------------

    w3, account = bot_contract.connect()  # also enforces the Sepolia chain id
    print(f"Contract: {os.environ['BOT_CONTRACT_ADDRESS']}")
    print(f"Operator wallet: {account.address}\n")

    market = (
        config.load_market()
    )  # pool, token and factory checks (raises if inconsistent)
    print()
    bot = bot_contract.load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])

    def quote_fn(token_in, token_out, amount_in):
        out, _, _ = config.quote_swap(
            market["quoter"], token_in, token_out, market["fee"], amount_in
        )
        return out * (10_000 - config.SLIPPAGE_BPS) // 10_000

    owner = os.environ.get("OWNER_ADDRESS") or None
    report = check_deployment(
        w3,
        bot,
        operator=account.address,
        router=os.environ["ROUTER_ADDRESS"],
        owner=Web3.to_checksum_address(owner) if owner else None,
        fee=market["fee"],
        tokens=list(market["tokens"].values()),
        max_trade=Decimal(os.environ["MAX_TRADE_AMOUNT"]),
        min_eth=Decimal(os.environ.get("MIN_OPERATOR_ETH", "0.01")),
        quote_fn=quote_fn,
    )
    report.summary()
    if report.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
