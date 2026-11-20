from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_event_service import candidate, fake_executor
from web3.exceptions import ContractLogicError

import arbitrage
import bot_contract
import event_market as market
from runtime import ConfigurationError

A = 10**18
SQRT = 2**96


@pytest.fixture
def cycle_executor(tmp_path, monkeypatch):
    ex, _ = fake_executor(tmp_path, "simulate")
    ex.settings = replace(ex.settings, quote_cap=640 * A, min_profit=A // 100)
    calls = []
    header = {
        "number": 7,
        "hash": bytes.fromhex("ab" * 32),
        "timestamp": 99,
        "baseFeePerGas": 1,
    }
    original_header = header.copy()
    hooks = {}
    limits = {
        "maxQuoteIn": 640 * A,
        "maxBaseIn": 10 * A,
        "minProfitFloor": A // 100,
    }

    class Eth:
        @property
        def chain_id(self):
            calls.append(("chain",))
            return 11155111

        def get_block(self, block):
            calls.append(("header", block))
            if "header" in hooks:
                hooks["header"](block)
            return header.copy() if block == "latest" or block == 7 else original_header

        def get_balance(self, address):
            calls.append(("balance",))
            return 10**20

        def get_transaction_count(self, *args):
            calls.append(("nonce",))
            return 1

    ex.w3.eth = Eth()

    class Read:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def call(self, *args, block_identifier=None):
            calls.append((self.name, block_identifier))
            return self.value() if callable(self.value) else self.value

    class Execute:
        def __init__(self, args):
            self.arguments = (args,)

        def call(self, tx, block_identifier=None):
            calls.append(("execute", block_identifier))
            if "execute" in hooks:
                hooks["execute"]()
            return A

        def estimate_gas(self, tx):
            calls.append(("estimate", "latest"))
            if "estimate" in hooks:
                hooks["estimate"]()
            return 100_000

        def build_transaction(self, tx):
            return tx

    def quote(params):
        token_in, token_out, amount, _fee, limit = params
        calls.append(("quote_args", params))
        zero_for_one = int(token_in, 16) < int(token_out, 16)
        assert (limit < SQRT) == zero_for_one
        output = amount // 1000 if token_in == market.QUOTE else amount * 1100
        if "quote" in hooks:
            hooks["quote"]()
        return Read("quote", (output, (SQRT + limit) // 2, 0, 90_000))

    tokens = {"f564": (market.QUOTE, 18), "feth": (market.BASE, 18)}
    ex.pools = {
        fee: {
            "pool": SimpleNamespace(
                address=address,
                functions=SimpleNamespace(slot0=lambda: Read("slot0", (SQRT,))),
            ),
            "quoter": SimpleNamespace(
                functions=SimpleNamespace(quoteExactInputSingle=quote)
            ),
            "w3": ex.w3,
            "fee": fee,
            "tokens": tokens,
        }
        for fee, address in market.POOLS.items()
    }
    identity = {
        "quoteToken": market.QUOTE,
        "baseToken": market.BASE,
        "pool0": market.POOLS[500],
        "pool1": market.POOLS[10000],
    }
    functions = {
        name: (lambda name=name, value=value: Read(name, value))
        for name, value in identity.items()
    }
    functions.update(
        {name: (lambda name=name: Read(name, lambda: limits[name])) for name in limits}
    )
    ex.bot.functions = SimpleNamespace(**functions, execute=Execute)
    decision = candidate() | {
        "backrun": {"buy_fee": 500, "sell_fee": 10000, "input_f564": 100}
    }
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: pytest.fail("unexpected broadcast")
    )
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("action", ["backrun", "arbitrage"])
@pytest.mark.parametrize("buy_fee,sell_fee", [(500, 10000), (10000, 500)])
def test_cycle_attempt_reuses_reads_and_preserves_exact_bracket_ranking(
    cycle_executor, action, buy_fee, sell_fee
):
    f = cycle_executor
    f.decision["backrun"].update(buy_fee=buy_fee, sell_fee=sell_fee)
    priority, _ = f.ex.quote_ladder(f.decision)
    result = f.ex.backrun(f.decision, action)
    counts = Counter(row[0] for row in f.calls)
    assert result["status"] == "simulated"
    assert result["stage"] == "final_guards"
    assert result["quote_rungs"] == 3
    assert result["snapshot_block"] == 7
    assert result["amount_raw"] == max(priority)
    assert result["gas_limit"] == 2 * result["estimated_gas"] == 200_000
    assert counts["chain"] == 2  # Once for the ladder, once at submission.
    assert counts["slot0"] == 2
    assert counts["quote"] == 6
    assert counts["execute"] == counts["estimate"] == 1
    # Previously: 3*7 quote reads + 10 simulation reads + 1 freshness read
    # + 4 perform reads = 36. This identical three-rung attempt needs 22 (14 fewer).
    assert sum(counts.values()) - counts["quote_args"] == 22
    for name in f.limits:
        assert counts[name] == 1
    for name in f.identity:
        assert counts[name] == 0  # Immutable route was validated at executor startup.
    pinned = {"slot0", "quote", "execute", *f.limits}
    assert all(row[1] == 7 for row in f.calls if row[0] in pinned)
    # Four latest freshness reads and two numbered canonical checks cover the attempt.
    assert [row[1] for row in f.calls if row[0] == "header"] == [
        "latest",
        "latest",
        7,
        7,
        "latest",
        "latest",
    ]
    assert f.ex.store.data["position"] is None


def test_new_attempt_rereads_mutable_caps_and_pool_prices(cycle_executor):
    f = cycle_executor
    f.ex.backrun(f.decision)
    f.limits["maxQuoteIn"] = 1
    with pytest.raises(ValueError, match="vault cap exceeded"):
        f.ex.backrun(f.decision | {"hash": "second"})
    counts = Counter(row[0] for row in f.calls)
    assert counts["slot0"] == 4
    assert counts["maxQuoteIn"] == 2
    assert counts["execute"] == counts["estimate"] == 1


@pytest.mark.parametrize("drift", ["block", "hash", "age"])
@pytest.mark.parametrize("stage", ["start", "quote", "execute", "estimate"])
def test_drift_never_reaches_send_or_durable_intent(cycle_executor, drift, stage):
    f = cycle_executor
    f.ex.settings = replace(f.ex.settings, mode="live")

    def change():
        if drift == "block":
            f.header["number"] = 8
        elif drift == "hash":
            f.header["hash"] = bytes.fromhex("cd" * 32)
        else:
            f.ex.clock = lambda: 140

    if stage == "start":
        change()
    else:
        f.hooks[stage] = change
    with pytest.raises((ConfigurationError, ValueError), match="current|reorganized"):
        f.ex.backrun(f.decision)
    counts = Counter(row[0] for row in f.calls)
    if stage in ("start", "quote"):
        assert counts["execute"] == counts["estimate"] == 0
    if stage == "execute":
        assert counts["estimate"] == 0
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}


@pytest.mark.parametrize("drift", [False, True])
def test_estimate_revert_is_classified_as_drift_only_with_head_evidence(
    cycle_executor, drift
):
    f = cycle_executor

    def revert():
        if drift:
            f.header["number"] = 8
        raise ContractLogicError("execution reverted: SPL")

    f.hooks["estimate"] = revert
    error = ConfigurationError if drift else ContractLogicError
    with pytest.raises(error, match="no longer current" if drift else "SPL"):
        f.ex.backrun(f.decision)
    assert f.ex.store.data["intent"] is None


def test_empty_bracket_keeps_full_ladder_breadth_without_duplicate_pool_reads(
    cycle_executor,
):
    f = cycle_executor
    f.ex.settings = replace(f.ex.settings, min_profit=30 * A)
    priority, rest = f.ex.quote_ladder(f.decision)
    result = f.ex.backrun(f.decision)
    bought = [
        row[1][2]
        for row in f.calls
        if row[0] == "quote_args" and row[1][0] == market.QUOTE
    ]
    assert bought == priority + rest
    assert result["amount_raw"] == f.ex.settings.quote_cap
    assert Counter(row[0] for row in f.calls)["slot0"] == 2


@pytest.mark.parametrize(
    "phase,hook,rungs",
    [
        ("quote_context", "header", 0),
        ("quote_ladder", "quote", 1),
        ("cycle_simulation", "execute", 3),
        ("gas_estimate", "estimate", 3),
        ("final_guards", "guard", 3),
    ],
)
def test_cycle_rejection_exposes_phase_and_rung_count(
    cycle_executor, phase, hook, rungs
):
    f = cycle_executor

    def fail(*args):
        raise ContractLogicError("execution reverted: SPL")

    if hook == "guard":
        f.ex.entry_guard = fail
    else:
        f.hooks[hook] = fail
    with pytest.raises(ContractLogicError):
        f.ex.backrun(f.decision)
    assert f.ex.attempt["stage"] == phase
    assert f.ex.attempt["quote_rungs"] == rungs
    assert f.ex.attempt["snapshot_block"] == 7
    assert "SPL" not in str(f.ex.attempt)
    assert f.ex.store.data["intent"] is None


def test_final_control_guard_still_blocks_cycle(cycle_executor):
    f = cycle_executor
    f.ex.settings = replace(f.ex.settings, mode="live")

    def stopped(action):
        raise ConfigurationError("entries paused")

    f.ex.entry_guard = stopped
    with pytest.raises(ConfigurationError, match="entries paused"):
        f.ex.backrun(f.decision)
    assert f.ex.store.data["intent"] is None


def test_standalone_defaults_keep_identity_checks_and_latest_estimate(cycle_executor):
    f = cycle_executor
    plan = arbitrage.quote_cycle(
        f.ex.pools[500], f.ex.pools[10000], 100 * A, A // 100, 10 * A
    )
    result = arbitrage.simulate_cycle(f.ex.w3, f.ex.bot, f.ex.operator, plan)
    assert result["estimated_gas"] == 100_000
    for name in f.identity:
        assert (name, 7) in f.calls
    assert ("execute", 7) in f.calls
    assert ("estimate", "latest") in f.calls


@pytest.mark.parametrize("changed", ["number", "hash"])
def test_context_requires_candidate_block_and_hash(cycle_executor, changed):
    f = cycle_executor
    header = f.header | {changed: 8 if changed == "number" else bytes(32)}
    context = arbitrage.CycleContext(7, f.decision["snapshot_hash"], header=header)
    with pytest.raises(ValueError, match="block mismatch"):
        arbitrage.quote_cycle(
            f.ex.pools[500],
            f.ex.pools[10000],
            100 * A,
            A // 100,
            10 * A,
            context=context,
        )
    assert not any(row[0] == "quote" for row in f.calls)


def test_simulation_rejects_plan_from_another_context_block(cycle_executor):
    f = cycle_executor
    plan = arbitrage.quote_cycle(
        f.ex.pools[500], f.ex.pools[10000], 100 * A, A // 100, 10 * A
    )
    context = arbitrage.CycleContext(8, f.decision["snapshot_hash"])
    with pytest.raises(ValueError, match="block mismatch"):
        arbitrage.simulate_cycle(f.ex.w3, f.ex.bot, f.ex.operator, plan, context)
    assert not any(row[0] == "execute" for row in f.calls)


@pytest.mark.parametrize(
    "profit,accepted",
    [
        (10 * A + 1, True),
        (10 * A, True),
        (10 * A - 1, False),
        (13_223 * A // 1000, True),
    ],
)
def test_atomic_profit_floor_tightens_maximum_slippage(
    cycle_executor, profit, accepted
):
    f = cycle_executor
    amount = 1860 * A
    quote_out = amount + profit

    def quote(params):
        token_in, _, _, _, limit = params
        output = A if token_in == market.QUOTE else quote_out
        return SimpleNamespace(
            call=lambda **kwargs: (output, (SQRT + limit) // 2, 0, 90_000)
        )

    for pool in f.ex.pools.values():
        pool["quoter"].functions.quoteExactInputSingle = quote

    def plan():
        return arbitrage.quote_cycle(
            f.ex.pools[500],
            f.ex.pools[10000],
            amount,
            10 * A,
            10 * A,
            slippage_bps=20,
        )

    if not accepted:
        with pytest.raises(ValueError, match="insufficient f564 profit"):
            plan()
        return
    result = plan()
    assert result["profit"] == profit
    assert result["min_quote_out"] == 1870 * A
    assert result["min_quote_out"] >= quote_out * 9980 // 10000
    assert result["min_quote_out"] <= quote_out
    assert result["min_base_out"] == A * 9980 // 10000
    if profit == 13_223 * A // 1000:
        assert quote_out * 9980 // 10000 - amount == 9_476_554 * A // 1_000_000


def test_pinned_simulation_executes_complete_cycle_on_in_memory_chain(combined):
    env = combined
    for token, fee, numerator, denominator in (
        (env.a, 500, 1, 1000),
        (env.b, 10000, 1100, 1),
    ):
        env.router.functions.setRoute(
            token.address, fee, (numerator, denominator, 10**30, 0, False)
        ).transact()
    header = env.w3.eth.get_block("latest")
    chain = SimpleNamespace(
        eth=SimpleNamespace(chain_id=11155111, get_block=env.w3.eth.get_block)
    )
    plan = {
        "block": header["number"],
        "block_hash": header["hash"].hex(),
        "quote_token": env.a.address,
        "base_token": env.b.address,
        "buy_pool": env.pools[0].address,
        "sell_pool": env.pools[1].address,
        "amount_in": 100 * A,
        "base_out": A // 10,
        "min_base_out": A // 10,
        "min_quote_out": 101 * A,
        "min_profit": A,
        "deadline": header["timestamp"] + 30,
        "buy_limit": SQRT,
        "sell_limit": SQRT,
    }
    context = arbitrage.CycleContext(
        plan["block"], plan["block_hash"], estimate_gas=False
    )
    before = env.a.functions.balanceOf(env.bot.address).call()
    result = arbitrage.simulate_cycle(chain, env.bot, env.operator, plan, context)
    assert result["profit"] == 10 * A
    assert result["estimated_gas"] is None
    assert env.a.functions.balanceOf(env.bot.address).call() == before
    assert env.router.functions.calls().call() == 0


def test_chain_guard_precedes_shared_quotes(cycle_executor):
    f = cycle_executor
    f.ex.w3.eth = SimpleNamespace(chain_id=1)
    with pytest.raises(ValueError, match="expected Sepolia"):
        arbitrage.quote_cycle(
            f.ex.pools[500],
            f.ex.pools[10000],
            100 * A,
            A // 100,
            10 * A,
            context=arbitrage.CycleContext(7, f.decision["snapshot_hash"]),
        )
    assert not any(row[0] == "quote" for row in f.calls)
