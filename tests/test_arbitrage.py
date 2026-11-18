from types import SimpleNamespace

import pytest
from eth_tester.exceptions import TransactionFailed
from web3 import EthereumTesterProvider, Web3

import arbitrage
from event_execution import Executor
from event_fees import FeeCache
from event_settings import EventSettings
from event_store import EventStore

A = 10**18
ZERO = "0x" + "00" * 20
LIMIT = 2**96


@pytest.fixture
def arb(combined_artifacts, request):
    w3 = Web3(EthereumTesterProvider())
    owner, operator, stranger = w3.eth.accounts[:3]
    w3.eth.default_account = owner

    def deploy(name, *args):
        item = combined_artifacts[name]
        factory = w3.eth.contract(abi=item["abi"], bytecode=item["bin"])
        receipt = w3.eth.wait_for_transaction_receipt(
            factory.constructor(*args).transact()
        )
        return w3.eth.contract(address=receipt["contractAddress"], abi=item["abi"])

    kind = getattr(request, "param", "MockToken")
    quote, base = deploy(kind), deploy(kind)
    factory = deploy("MockArbFactory")
    router = deploy("MockArbRouter", factory.address)
    pools = [
        deploy("MockArbPool", quote.address, base.address, fee, factory.address)
        for fee in (500, 10000)
    ]
    for fee, pool in zip((500, 10000), pools, strict=True):
        factory.functions.setPool(
            quote.address, base.address, fee, pool.address
        ).transact()
    args = [
        owner,
        operator,
        router.address,
        quote.address,
        base.address,
        [p.address for p in pools],
        [1000 * A, 10 * A, A // 10],
    ]
    vault = deploy("Phase2TradingBot", *args)
    quote.functions.mint(vault.address, 5000 * A).transact()
    base.functions.mint(
        vault.address, 7 * A
    ).transact()  # deposited base must never fund a cycle
    vault.functions.setPaused(False).transact()

    def route(token, fee, numerator, denominator, capacity=10**30, lie=0, ignore=False):
        router.functions.setRoute(
            token.address, fee, (numerator, denominator, capacity, lie, ignore)
        ).transact()

    for fee in (500, 10000):
        route(quote, fee, 1, 1000)
        route(base, fee, 1010, 1)
    return SimpleNamespace(**locals())


def cycle(arb, **changes):
    values = {
        "buyPool0": True,
        "amountIn": 1000 * A,
        "minBaseOut": A,
        "minQuoteOut": 1001 * A,
        "minProfit": A,
        "deadline": arb.w3.eth.get_block("latest")["timestamp"] + 120,
        "buySqrtPriceLimitX96": LIMIT,
        "sellSqrtPriceLimitX96": LIMIT,
    }
    return tuple((values | changes).values())


def execute(arb, caller=None, **changes):
    return arb.vault.functions.execute(cycle(arb, **changes)).transact(
        {"from": caller or arb.operator}
    )


def balances(arb):
    return tuple(
        t.functions.balanceOf(arb.vault.address).call() for t in (arb.quote, arb.base)
    )


@pytest.mark.parametrize("fee_policy", ["fixed", "competitive"])
def test_event_executor_sends_atomic_cycle_with_durable_accounting(
    arb, tmp_path, monkeypatch, fee_policy
):
    from dataclasses import replace

    import event_market

    monkeypatch.setattr(event_market, "QUOTE", arb.quote.address.lower())
    eth = arb.w3.eth

    class TestEth:
        chain_id = 11155111

        def __getattr__(self, name):
            return getattr(eth, name)

    ex = Executor.__new__(Executor)
    ex.w3 = SimpleNamespace(eth=TestEth())
    ex.settings = replace(EventSettings(), mode="live", fee_policy=fee_policy)
    ex.operator = arb.operator
    ex.account = eth.account.from_key(
        arb.w3.provider.ethereum_tester.backend.account_keys[1]
    )
    ex.clock = lambda: eth.get_block("latest")["timestamp"]
    ex.fees = FeeCache(ex.w3, ex.settings, ex.clock)
    ex.store = EventStore(tmp_path, {}, "live", clock=ex.clock)
    ex.bot = arb.vault
    ex.entry_guard = lambda action: None
    header = eth.get_block("latest")
    result = ex.perform(
        "backrun:fixture",
        arb.vault.functions.execute(cycle(arb)),
        "backrun",
        1000 * A,
        1001 * A,
        {
            "hash": "fixture",
            "snapshot_block": header["number"],
            "snapshot_hash": Web3.to_hex(header["hash"]),
        },
    )
    assert result["profit_raw"] == 10 * A
    assert balances(arb) == (5010 * A, 7 * A)
    assert ex.store.data["intent"] is None and ex.store.data["position"] is None
    assert ex.store.data["trades"][-1]["gas_wei"] > 0


@pytest.mark.parametrize("direction", [True, False])
def test_profitable_cycle_preserves_deposited_base_and_keeps_profit(arb, direction):
    assert (
        arb.vault.functions.execute(cycle(arb, buyPool0=direction)).call(
            {"from": arb.operator}
        )
        == 10 * A
    )
    before = balances(arb)
    receipt = arb.w3.eth.wait_for_transaction_receipt(execute(arb, buyPool0=direction))
    assert balances(arb) == (before[0] + 10 * A, before[1])
    assert arb.router.functions.calls().call() == 2
    assert arb.router.functions.lastRecipient().call() == arb.vault.address
    assert arb.router.functions.lastLimit().call() == LIMIT
    event = arb.vault.events.Arbitrage().process_receipt(receipt)[0]["args"]
    assert event["profit"] == 10 * A and event["baseTraded"] == A
    for token in (arb.quote, arb.base):
        assert (
            token.functions.allowance(arb.vault.address, arb.router.address).call() == 0
        )
        assert token.functions.balanceOf(arb.operator).call() == 0


def test_stranger_cannot_execute(arb):
    with pytest.raises(TransactionFailed, match="not operator"):
        execute(arb, caller=arb.stranger)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"amountIn": 0}, "quote cap"),
        ({"amountIn": 1000 * A + 1}, "quote cap"),
        ({"minBaseOut": 0}, "zero minimum"),
        ({"minQuoteOut": 0}, "zero minimum"),
        ({"minProfit": 0}, "profit floor"),
        ({"deadline": 1}, "expired"),
        ({"buySqrtPriceLimitX96": 0}, "bad price limit"),
        ({"sellSqrtPriceLimitX96": 0}, "bad price limit"),
    ],
)
def test_cycle_guards(arb, change, reason):
    with pytest.raises(TransactionFailed, match=reason):
        execute(arb, **change)
    assert arb.router.functions.calls().call() == 0


def test_unprofitable_cycle_reverts_both_legs(arb):
    arb.route(arb.base, 10000, 990, 1)
    before = balances(arb)
    with pytest.raises(TransactionFailed, match="Too little received"):
        execute(arb, minQuoteOut=1)
    assert balances(arb) == before
    assert arb.router.functions.calls().call() == 0
    assert arb.quote.functions.balanceOf(arb.router.address).call() == 0


def test_operator_cannot_lower_owner_profit_floor(arb):
    arb.vault.functions.setLimits(1000 * A, 10 * A, 11 * A).transact()
    with pytest.raises(TransactionFailed, match="profit floor"):
        execute(arb)
    with pytest.raises(TransactionFailed, match="Too little received"):
        execute(arb, minProfit=11 * A)


@pytest.mark.parametrize("leg", [1, 2])
def test_range_limited_partial_fill_rolls_back_everything(arb, leg):
    # Model the pool stopping at the supplied sqrt price limit with input unspent.
    if leg == 1:
        arb.route(arb.quote, 500, 1, 1000, capacity=500 * A)
    else:
        # Deliberately generous output so only the full-input guard can catch the partial fill.
        arb.route(arb.base, 10000, 3000, 1, capacity=A // 2)
    before = balances(arb)
    with pytest.raises(TransactionFailed, match="partial input"):
        execute(arb, minBaseOut=1)
    assert balances(arb) == before
    assert arb.router.functions.calls().call() == 0
    assert (
        arb.quote.functions.allowance(arb.vault.address, arb.router.address).call() == 0
    )


def test_new_base_above_owner_cap_rolls_back_first_leg(arb):
    arb.vault.functions.setLimits(1000 * A, A // 2, A // 10).transact()
    before = balances(arb)
    with pytest.raises(TransactionFailed, match="base cap"):
        execute(arb)
    assert balances(arb) == before and arb.router.functions.calls().call() == 0


def test_router_output_cannot_substitute_for_received_tokens(arb):
    arb.route(arb.base, 10000, 1010, 1, lie=100 * A)
    before = balances(arb)
    with pytest.raises(TransactionFailed, match="bad output"):
        execute(arb)
    assert balances(arb) == before


def test_vault_enforces_minimum_even_if_router_does_not(arb):
    arb.route(arb.base, 10000, 990, 1, ignore=True)
    with pytest.raises(TransactionFailed, match="bad output"):
        execute(arb, minQuoteOut=1)


def test_reentry_is_blocked_even_from_authorized_router(arb):
    arb.vault.functions.setOperator(arb.router.address).transact()
    data = arb.vault.encode_abi("execute", args=[cycle(arb)])
    arb.router.functions.setCallback(arb.vault.address, data).transact()
    execute(arb, caller=arb.owner)
    assert not arb.router.functions.reentrySucceeded().call()
    assert arb.router.functions.calls().call() == 2


def test_owner_admin_and_two_step_ownership(arb):
    for caller in (arb.operator, arb.stranger):
        for fn in (
            arb.vault.functions.setPaused(True),
            arb.vault.functions.setOperator(caller),
            arb.vault.functions.setLimits(A, A, A),
            arb.vault.functions.withdraw(arb.quote.address, 1),
            arb.vault.functions.withdrawETH(0),
            arb.vault.functions.transferOwnership(caller),
        ):
            with pytest.raises(TransactionFailed, match="not owner"):
                fn.transact({"from": caller})
    arb.vault.functions.transferOwnership(arb.stranger).transact()
    assert arb.vault.functions.owner().call() == arb.owner
    with pytest.raises(TransactionFailed, match="not pending owner"):
        arb.vault.functions.acceptOwnership().transact({"from": arb.operator})
    arb.vault.functions.acceptOwnership().transact({"from": arb.stranger})
    assert arb.vault.functions.owner().call() == arb.stranger
    arb.vault.functions.withdraw(arb.quote.address, A).transact({"from": arb.stranger})
    assert arb.quote.functions.balanceOf(arb.stranger).call() == A


def test_pause_and_disable_operator(arb):
    arb.vault.functions.setPaused(True).transact()
    with pytest.raises(TransactionFailed, match="paused"):
        execute(arb)
    arb.vault.functions.setPaused(False).transact()
    arb.vault.functions.setOperator(ZERO).transact()
    with pytest.raises(TransactionFailed, match="not operator"):
        execute(arb)


def test_deployment_checks_canonical_pools_and_starts_paused(arb):
    vault = arb.deploy("Phase2TradingBot", *arb.args)
    assert vault.functions.paused().call()
    assert vault.functions.owner().call() == arb.owner
    assert vault.functions.fee0().call() == 500
    assert vault.functions.fee1().call() == 10000
    fake = arb.deploy(
        "MockArbPool", arb.quote.address, arb.base.address, 500, arb.factory.address
    )
    args = [*arb.args[:5], [fake.address, arb.pools[1].address], arb.args[6]]
    with pytest.raises(TransactionFailed, match="unregistered pool"):
        arb.deploy("Phase2TradingBot", *args)
    args[5] = [arb.pools[0].address, arb.pools[0].address]
    with pytest.raises(TransactionFailed, match="same pool"):
        arb.deploy("Phase2TradingBot", *args)


@pytest.mark.parametrize("arb", ["NoReturnToken"], indirect=True)
def test_no_return_approval_tokens_support_repeated_cycles(arb):
    before = balances(arb)
    execute(arb)
    execute(arb)
    assert balances(arb) == (before[0] + 20 * A, before[1])
    for token in (arb.quote, arb.base):
        assert (
            token.functions.allowance(arb.vault.address, arb.router.address).call() == 0
        )


@pytest.mark.parametrize("leg", [1, 2])
def test_absent_liquidity_reverts_cycle(arb, leg):
    if leg == 1:
        arb.route(arb.quote, 500, 1, 1000, capacity=0)
    else:
        arb.route(arb.base, 10000, 1010, 1, capacity=0)
    before = balances(arb)
    with pytest.raises(TransactionFailed):
        execute(arb)
    assert balances(arb) == before and arb.router.functions.calls().call() == 0


def test_zero_owner_limits_and_wrong_pool_tokens_are_refused(arb):
    args = arb.args.copy()
    args[0] = ZERO
    with pytest.raises(TransactionFailed, match="bad owner"):
        arb.deploy("Phase2TradingBot", *args)
    for limits in ([0, A, 1], [A, 0, 1], [A, A, 0]):
        with pytest.raises(TransactionFailed, match="zero limit"):
            arb.vault.functions.setLimits(*limits).transact()
    junk = arb.deploy("MockToken")
    wrong = arb.deploy(
        "MockArbPool", junk.address, arb.base.address, 500, arb.factory.address
    )
    args = arb.args.copy()
    args[5] = [wrong.address, arb.pools[1].address]
    with pytest.raises(TransactionFailed, match="pool tokens"):
        arb.deploy("Phase2TradingBot", *args)


def test_owner_withdraws_native_eth_and_false_token_is_refused(arb):
    arb.w3.eth.send_transaction(
        {"from": arb.owner, "to": arb.vault.address, "value": A}
    )
    arb.vault.functions.withdrawETH(A).transact()
    assert arb.w3.eth.get_balance(arb.vault.address) == 0
    false_token = arb.deploy("FalseToken")
    with pytest.raises(TransactionFailed, match="token call failed"):
        arb.vault.functions.withdraw(false_token.address, 1).transact()


def test_simulation_checks_vault_identity_and_does_not_change_balances(arb):
    plan = {
        "quote_token": arb.quote.address,
        "base_token": arb.base.address,
        "buy_pool": arb.pools[0].address,
        "sell_pool": arb.pools[1].address,
        "amount_in": 1000 * A,
        "base_out": A,
        "min_base_out": A,
        "min_quote_out": 1001 * A,
        "min_profit": A,
        "deadline": arb.w3.eth.get_block("latest")["timestamp"] + 120,
        "buy_limit": LIMIT,
        "sell_limit": LIMIT,
    }
    chain = SimpleNamespace(eth=SimpleNamespace(chain_id=11155111))
    before = balances(arb)
    result = arbitrage.simulate_cycle(chain, arb.vault, arb.operator, plan)
    assert result["profit"] == 10 * A and result["estimated_gas"] > 0
    assert balances(arb) == before and arb.router.functions.calls().call() == 0
    with pytest.raises(ValueError, match="pool mismatch"):
        arbitrage.simulate_cycle(
            chain, arb.vault, arb.operator, plan | {"sell_pool": arb.stranger}
        )
    with pytest.raises(ValueError, match="token mismatch"):
        arbitrage.simulate_cycle(
            chain, arb.vault, arb.operator, plan | {"quote_token": arb.stranger}
        )
