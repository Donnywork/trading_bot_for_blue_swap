from decimal import Decimal

import pytest
from eth_tester.exceptions import TransactionFailed

import bot_contract
import live_check

A = 10**18
FEES = [3000, 500]
OWNER_UNSET = None


@pytest.fixture(autouse=True)
def tester_rejections(monkeypatch):
    # the in-memory chain raises its own error type for a reverting eth_call
    monkeypatch.setattr(
        live_check, "REJECTIONS", (*live_check.REJECTIONS, TransactionFailed)
    )


def tokens_for(env):
    return [(env.a.address, 18, "A"), (env.b.address, 18, "B")]


def run_check(env, **overrides):
    kwargs = {
        "operator": env.operator,
        "router": env.router.address,
        "owner": env.owner,
        "fee": 3000,
        "tokens": tokens_for(env),
        "max_trade": Decimal(2),  # matches the 2-token cap the fixture deploys
        "min_eth": Decimal("0.01"),
        "stranger": env.stranger,  # the test chain needs a funded `from`; real nodes do not
    }
    return live_check.check_deployment(
        env.w3, overrides.pop("bot", env.bot), **(kwargs | overrides)
    )


def test_a_correct_deployment_has_no_failures(env, capsys):
    report = run_check(env)
    out = capsys.readouterr().out
    assert report.counts["FAIL"] == 0, out
    assert "[PASS] code matches contracts/TradingBot.sol" in out
    assert "[PASS] a stranger cannot swap" in out
    assert "[PASS] operator cannot swap into an unlisted token" in out
    assert "[PASS] operator cannot withdraw" in out
    # the vault holds tokenA only: A -> B is simulated, B -> A needs funding
    assert "[PASS] simulated swap A -> B" in out
    assert "[SKIP] simulated swap B -> A: fund the contract" in out


def test_wrong_operator_or_owner_or_router_fails(env, capsys):
    assert run_check(env, operator=env.stranger).failed
    assert "operator() is the PRIVATE_KEY wallet" in capsys.readouterr().out
    assert run_check(env, owner=env.stranger).failed
    assert run_check(env, router=env.junk.address).failed


def test_owner_not_set_warns_but_does_not_fail(env):
    report = run_check(env, owner=OWNER_UNSET)
    assert report.counts["FAIL"] == 0 and report.counts["WARN"] >= 1


def test_a_token_missing_from_the_allowlist_fails(env):
    bot = env.deploy_bot(env.router, [env.a.address], [2 * A])  # B is not allowed
    report = run_check(env, bot=bot)
    assert report.failed


def test_changed_cap_warns(env):
    env.bot.functions.setTokenCap(env.a.address, 3 * A).transact({"from": env.owner})
    report = run_check(env)
    assert not report.failed and report.counts["WARN"] >= 1


def test_fee_tier_not_allowed_fails(env):
    assert run_check(env, fee=10000).failed


def test_pending_ownership_transfer_warns(env):
    env.bot.functions.transferOwnership(env.stranger).transact({"from": env.owner})
    report = run_check(env)
    assert not report.failed and report.counts["WARN"] >= 1


def test_low_operator_gas_warns(env):
    report = run_check(env, min_eth=Decimal(10**9))
    assert not report.failed and report.counts["WARN"] >= 1


def test_no_contract_at_the_address_fails(env):
    ghost = env.w3.eth.contract(address=env.stranger, abi=env.bot.abi)
    report = live_check.check_deployment(
        env.w3,
        ghost,
        operator=env.operator,
        router=env.router.address,
        owner=env.owner,
        fee=3000,
        tokens=tokens_for(env),
        max_trade=Decimal(2),
        min_eth=Decimal("0.01"),
    )
    assert report.failed and report.counts["PASS"] == 0


def test_a_call_that_should_be_rejected_but_succeeds_is_a_failure(env):
    report = live_check.Report()
    live_check.expect_revert(
        report, "owner() must fail", env.bot.functions.owner(), env.owner, "x"
    )
    assert report.failed


def test_a_revert_for_another_reason_is_only_a_warning(env):
    report = live_check.Report()
    fn = env.bot.functions.withdraw(env.a.address, 1)
    live_check.expect_revert(
        report, "wrong reason", fn, env.operator, "some other reason"
    )
    assert not report.failed and report.counts["WARN"] == 1


def test_quote_function_sets_the_simulated_minimum(env):
    seen = []

    def quote_fn(token_in, token_out, amount_in):
        seen.append(amount_in)
        return amount_in  # 1:1 router

    report = run_check(env, quote_fn=quote_fn)
    assert not report.failed and seen == [2 * A]


def test_deployed_code_matches_only_this_contract(env):
    assert bot_contract.deployed_code_matches(env.w3, env.bot.address)
    # a second deployment (different router, owner...) is still the same code
    other = env.deploy_bot(env.router, [env.b.address], [A])
    assert bot_contract.deployed_code_matches(env.w3, other.address)
    # but a different contract is not
    assert not bot_contract.deployed_code_matches(env.w3, env.router.address)
    assert not bot_contract.deployed_code_matches(env.w3, env.stranger)


@pytest.mark.parametrize("status", ["PASS", "FAIL", "WARN", "SKIP"])
def test_report_counts_and_prints(status, capsys):
    report = live_check.Report()
    report.add(status, "a check", "detail")
    assert report.counts[status] == 1
    assert f"[{status}] a check: detail" in capsys.readouterr().out
    assert report.failed == (status == "FAIL")
