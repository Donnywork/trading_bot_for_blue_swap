from types import SimpleNamespace

import pytest
import solcx
from eth_account import Account
from eth_tester.exceptions import TransactionFailed

import bot_contract

A = 10**18
ZERO = "0x0000000000000000000000000000000000000000"
FEES = [3000, 500]


# ---------------------------------------------------------------- basics


def test_owner_is_exposed(env):
    assert env.bot.functions.owner().call() == env.owner
    assert env.bot.functions.operator().call() == env.operator
    assert env.bot.functions.pendingOwner().call() == ZERO


def test_constructor_sets_limits(env):
    assert env.bot.functions.tokenCap(env.a.address).call() == 2 * A
    assert env.bot.functions.tokenCap(env.junk.address).call() == 0
    assert env.bot.functions.feeAllowed(3000).call()
    assert env.bot.functions.feeAllowed(500).call()
    assert not env.bot.functions.feeAllowed(10000).call()


def test_constructor_checks(env):
    with pytest.raises(TransactionFailed, match="bad owner"):
        env.deploy("TradingBot", ZERO, env.router.address, env.operator, [], [], [])
    with pytest.raises(TransactionFailed, match="router not a contract"):
        env.deploy("TradingBot", env.owner, env.stranger, env.operator, [], [], [])
    with pytest.raises(TransactionFailed, match="length mismatch"):
        env.deploy(
            "TradingBot",
            env.owner,
            env.router.address,
            env.operator,
            [env.a.address],
            [],
            [],
        )


# ---------------------------------------------------------------- swap


def test_operator_swap_keeps_output_in_contract(env):
    env.swap(env.a, env.b)
    assert env.a.functions.balanceOf(env.bot.address).call() == 4 * A
    assert env.b.functions.balanceOf(env.bot.address).call() == A
    assert env.b.functions.balanceOf(env.operator).call() == 0


def test_swap_params_reach_router(env):
    env.swap(env.a, env.b, amount=A, fee=500)
    assert env.router.functions.lastRecipient().call() == env.bot.address
    assert env.router.functions.lastFee().call() == 500
    assert env.router.functions.lastAmountIn().call() == A
    assert env.router.functions.lastSqrtPriceLimit().call() == 0
    assert env.router.functions.lastAllowance().call() == A  # exact, not unlimited


def test_allowance_is_cleared_after_swap(env):
    env.swap(env.a, env.b)
    assert env.a.functions.allowance(env.bot.address, env.router.address).call() == 0


def test_owner_can_swap(env):
    env.swap(env.a, env.b, frm=env.owner)
    assert env.b.functions.balanceOf(env.bot.address).call() == A
    assert env.b.functions.balanceOf(env.owner).call() == 0


def test_stranger_cannot_swap(env):
    with pytest.raises(TransactionFailed, match="not operator"):
        env.swap(env.a, env.b, frm=env.stranger)


def test_zero_min_out_rejected(env):
    with pytest.raises(TransactionFailed, match="minOut is zero"):
        env.swap(env.a, env.b, min_out=0)


def test_min_out_enforced_by_router(env):
    with pytest.raises(TransactionFailed, match="Too little received"):
        env.swap(env.a, env.b, min_out=2 * A)


def test_swap_events(env):
    r = env.receipt(env.swap(env.a, env.b))
    ev = env.bot.events.Swap().process_receipt(r)[0]["args"]
    assert ev["tokenIn"] == env.a.address
    assert ev["tokenOut"] == env.b.address
    assert (ev["amountIn"], ev["amountOut"]) == (A, A)


# ---------------------------------------------------------------- limits (leaked operator key)


def test_token_out_must_be_allowed(env):
    with pytest.raises(TransactionFailed, match="tokenOut not allowed"):
        env.swap(env.a, env.junk)


def test_token_in_must_be_allowed(env):
    env.junk.functions.mint(env.bot.address, A).transact()
    with pytest.raises(TransactionFailed, match="tokenIn not allowed"):
        env.swap(env.junk, env.a)


def test_fee_must_be_allowed(env):
    with pytest.raises(TransactionFailed, match="fee not allowed"):
        env.swap(env.a, env.b, fee=10000)


def test_amount_over_cap_rejected(env):
    with pytest.raises(TransactionFailed, match="amountIn out of range"):
        env.swap(env.a, env.b, amount=2 * A + 1)
    env.swap(env.a, env.b, amount=2 * A)  # exactly the cap is fine


def test_zero_amount_and_same_token_rejected(env):
    with pytest.raises(TransactionFailed, match="amountIn out of range"):
        env.swap(env.a, env.b, amount=0)
    with pytest.raises(TransactionFailed, match="same token"):
        env.swap(env.a, env.a)


def test_owner_manages_limits(env):
    env.bot.functions.setTokenCap(env.junk.address, A).transact({"from": env.owner})
    env.swap(env.a, env.junk)
    env.bot.functions.setTokenCap(env.junk.address, 0).transact({"from": env.owner})
    with pytest.raises(TransactionFailed, match="tokenOut not allowed"):
        env.swap(env.a, env.junk)

    env.bot.functions.setFeeAllowed(10000, True).transact({"from": env.owner})
    env.swap(env.a, env.b, fee=10000)
    env.bot.functions.setFeeAllowed(10000, False).transact({"from": env.owner})
    with pytest.raises(TransactionFailed, match="fee not allowed"):
        env.swap(env.a, env.b, fee=10000)


# ---------------------------------------------------------------- access control


def test_non_owner_cannot_use_admin_functions(env):
    fns = [
        env.bot.functions.setOperator(env.stranger),
        env.bot.functions.transferOwnership(env.stranger),
        env.bot.functions.withdraw(env.a.address, 1),
        env.bot.functions.withdrawETH(0),
        env.bot.functions.setTokenCap(env.junk.address, A),
        env.bot.functions.setFeeAllowed(10000, True),
    ]
    for caller in (env.stranger, env.operator):
        for fn in fns:
            with pytest.raises(TransactionFailed, match="not owner"):
                fn.transact({"from": caller})
    assert env.a.functions.balanceOf(env.operator).call() == 0
    assert env.a.functions.balanceOf(env.stranger).call() == 0


def test_set_operator_and_disable(env):
    env.bot.functions.setOperator(env.stranger).transact({"from": env.owner})
    env.swap(env.a, env.b, frm=env.stranger)
    with pytest.raises(TransactionFailed, match="not operator"):
        env.swap(env.a, env.b, frm=env.operator)

    env.bot.functions.setOperator(ZERO).transact({"from": env.owner})
    with pytest.raises(TransactionFailed, match="not operator"):
        env.swap(env.a, env.b, frm=env.stranger)


# ---------------------------------------------------------------- ownership (two-step)


def test_ownership_transfer_needs_acceptance(env):
    r = env.receipt(
        env.bot.functions.transferOwnership(env.stranger).transact({"from": env.owner})
    )
    started = env.bot.events.OwnershipTransferStarted().process_receipt(r)[0]["args"]
    assert started["pendingOwner"] == env.stranger
    # nothing changes until the new owner accepts
    assert env.bot.functions.owner().call() == env.owner
    assert env.bot.functions.pendingOwner().call() == env.stranger

    with pytest.raises(TransactionFailed, match="not pending owner"):
        env.bot.functions.acceptOwnership().transact({"from": env.operator})

    r = env.receipt(
        env.bot.functions.acceptOwnership().transact({"from": env.stranger})
    )
    done = env.bot.events.OwnershipTransferred().process_receipt(r)[0]["args"]
    assert (done["previousOwner"], done["newOwner"]) == (env.owner, env.stranger)
    assert env.bot.functions.owner().call() == env.stranger
    assert env.bot.functions.pendingOwner().call() == ZERO

    # the old owner has lost its rights
    with pytest.raises(TransactionFailed, match="not owner"):
        env.bot.functions.setOperator(env.owner).transact({"from": env.owner})


def test_ownership_transfer_rejects_bad_addresses(env):
    with pytest.raises(TransactionFailed, match="bad owner"):
        env.bot.functions.transferOwnership(ZERO).transact({"from": env.owner})
    with pytest.raises(TransactionFailed, match="bad owner"):
        env.bot.functions.transferOwnership(env.bot.address).transact(
            {"from": env.owner}
        )


# ---------------------------------------------------------------- withdraw


def test_withdraw_pays_the_owner_not_the_caller(env):
    env.bot.functions.withdraw(env.a.address, 2 * A).transact({"from": env.owner})
    assert env.a.functions.balanceOf(env.owner).call() == 2 * A
    assert env.a.functions.balanceOf(env.bot.address).call() == 3 * A

    # after a handover, funds go to the NEW owner (it is not hardcoded or msg.sender-based)
    env.bot.functions.transferOwnership(env.stranger).transact({"from": env.owner})
    env.bot.functions.acceptOwnership().transact({"from": env.stranger})
    env.bot.functions.withdraw(env.a.address, A).transact({"from": env.stranger})
    assert env.a.functions.balanceOf(env.stranger).call() == A
    assert env.a.functions.balanceOf(env.owner).call() == 2 * A


def test_withdraw_more_than_balance_reverts(env):
    with pytest.raises(TransactionFailed):
        env.bot.functions.withdraw(env.a.address, 6 * A).transact({"from": env.owner})


def test_eth_in_and_out(env):
    env.w3.eth.send_transaction({"from": env.owner, "to": env.bot.address, "value": A})
    assert env.w3.eth.get_balance(env.bot.address) == A

    env.bot.functions.transferOwnership(env.stranger).transact({"from": env.owner})
    env.bot.functions.acceptOwnership().transact({"from": env.stranger})
    before = env.w3.eth.get_balance(env.stranger)
    r = env.receipt(env.bot.functions.withdrawETH(A).transact({"from": env.stranger}))
    assert env.w3.eth.get_balance(env.bot.address) == 0
    gas_cost = r["gasUsed"] * r["effectiveGasPrice"]
    assert env.w3.eth.get_balance(env.stranger) == before + A - gas_cost

    with pytest.raises(TransactionFailed, match="eth transfer failed"):
        env.bot.functions.withdrawETH(1).transact({"from": env.stranger})


def test_withdraw_event(env):
    r = env.receipt(
        env.bot.functions.withdraw(env.a.address, A).transact({"from": env.owner})
    )
    ev = env.bot.events.Withdraw().process_receipt(r)[0]["args"]
    assert (ev["token"], ev["amount"]) == (env.a.address, A)


# ---------------------------------------------------------------- odd tokens


def test_token_without_return_value_can_be_withdrawn(env):
    t = env.deploy("NoReturnToken")
    t.functions.mint(env.bot.address, 5 * A).transact()
    env.bot.functions.withdraw(t.address, A).transact({"from": env.owner})
    assert t.functions.balanceOf(env.owner).call() == A


def test_token_returning_false_reverts(env):
    f = env.deploy("FalseToken")
    with pytest.raises(TransactionFailed, match="token call failed"):
        env.bot.functions.withdraw(f.address, 1).transact({"from": env.owner})


def test_non_contract_token_reverts(env):
    with pytest.raises(TransactionFailed, match="not a contract"):
        env.bot.functions.withdraw(env.stranger, 123).transact({"from": env.owner})


def test_partial_fill_with_usdt_style_token_does_not_brick_next_swap(env):
    router = env.deploy("PartialRouter")
    t = env.deploy("NoReturnToken")
    u = env.deploy("NoReturnToken")
    bot = env.deploy_bot(router, [t.address, u.address])
    t.functions.mint(bot.address, 5 * A).transact()

    env.swap(t, u, bot=bot, min_out=1)
    # router only pulled half: the leftover allowance must have been cleared
    assert t.functions.allowance(bot.address, router.address).call() == 0
    env.swap(t, u, bot=bot, min_out=1)  # a stale nonzero allowance would revert here


@pytest.mark.parametrize("attack", ["withdraw", "swap", "setOperator"])
def test_reentrancy_from_token_is_blocked(env, attack):
    rt = env.deploy("ReentrantToken")
    bot = env.deploy_bot(env.router, [rt.address, env.b.address])
    rt.functions.mint(bot.address, 5 * A).transact()
    calls = {
        "withdraw": bot.encode_abi("withdraw", [rt.address, 1]),
        "swap": bot.encode_abi("swap", [rt.address, env.b.address, 3000, 1, 1]),
        "setOperator": bot.encode_abi("setOperator", [rt.address]),
    }
    rt.functions.setup(bot.address, calls[attack]).transact()

    if attack == "withdraw":
        bot.functions.withdraw(rt.address, 1).transact({"from": env.owner})
    else:
        env.swap(rt, env.b, bot=bot)

    assert rt.functions.attempted().call() is True  # the attack really ran
    assert rt.functions.reentrySucceeded().call() is False


# ---------------------------------------------------------------- python layer


def test_validate_owner():
    bot_wallet = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    good = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    assert bot_contract.validate_owner(good, bot_wallet) == good

    typo = good[:-1] + "6"  # still hex, but the checksum no longer matches
    for bad in ("", typo, good.lower(), ZERO, "not-an-address"):
        with pytest.raises(ValueError):
            bot_contract.validate_owner(bad, bot_wallet)
    with pytest.raises(ValueError, match="bot wallet"):
        bot_contract.validate_owner(bot_wallet, bot_wallet)


def test_standard_json_compiles_to_the_deployed_bytecode():
    # what Etherscan would compile for verification == what we deploy
    out = solcx.compile_standard(
        bot_contract.standard_json_input(), solc_version=bot_contract.SOLC_VERSION
    )
    code = out["contracts"]["contracts/TradingBot.sol"]["TradingBot"]["evm"]["bytecode"]
    assert code["object"] == bot_contract.compile_bot()[1]


class Funded:
    """A real eth_account Account funded on the tester chain, so send_tx signs for real."""

    def __init__(self, env):
        self.account = Account.create()
        env.w3.eth.send_transaction(
            {"from": env.owner, "to": self.account.address, "value": 10 * A}
        )


def quote_for(env, amount_in=A):
    return {
        "token_in": env.a.address,
        "token_out": env.b.address,
        "amount_in": amount_in,
        "amount_out_min": amount_in // 2,
    }


def test_deploy_and_swap_through_send_tx(env):
    bot_wallet = Funded(env).account
    caps = {env.a.address: 2 * A, env.b.address: 2 * A}
    address, abi, ctor_args = bot_contract.deploy_bot(
        env.w3,
        bot_wallet,
        env.owner,
        env.router.address,
        bot_wallet.address,
        caps,
        FEES,
    )
    bot = env.w3.eth.contract(address=address, abi=abi)
    assert bot.functions.owner().call() == env.owner  # the deployer is NOT the owner
    assert bot.functions.operator().call() == bot_wallet.address
    assert bot.functions.tokenCap(env.a.address).call() == 2 * A
    assert len(ctor_args) % 64 == 0 and ctor_args

    env.a.functions.mint(address, 5 * A).transact()
    receipt = bot_contract.swap_via_contract(
        env.w3, bot_wallet, bot, quote_for(env), 3000, dry_run=False
    )
    assert receipt["status"] == 1
    assert env.b.functions.balanceOf(address).call() == A


def test_a_swap_is_sent_with_the_swap_gas_headroom_not_the_small_one(env, monkeypatch):
    sent = {}

    def spy(w3, account, tx, gas_buffer=bot_contract.GAS_BUFFER):
        sent.update(gas_buffer=gas_buffer, estimate=tx["gas"])
        return {"status": 1}

    monkeypatch.setattr(bot_contract, "send_tx", spy)
    account = SimpleNamespace(address=env.operator)
    bot_contract.swap_via_contract(
        env.w3, account, env.bot, quote_for(env), 3000, dry_run=False
    )
    assert sent["gas_buffer"] == bot_contract.SWAP_GAS_BUFFER > bot_contract.GAS_BUFFER


def test_a_swap_is_sent_with_a_gas_limit_that_leaves_headroom_over_what_it_used(
    env,
):
    # the real chain: send the swap with exactly the estimate (no headroom) and with the swap
    # headroom; both fit here, and the limit actually sent is the estimate times the buffer
    bot_wallet = Funded(env).account
    caps = {env.a.address: 2 * A, env.b.address: 2 * A}
    address, abi, _ = bot_contract.deploy_bot(
        env.w3,
        bot_wallet,
        env.owner,
        env.router.address,
        bot_wallet.address,
        caps,
        FEES,
    )
    bot = env.w3.eth.contract(address=address, abi=abi)
    env.a.functions.mint(address, 5 * A).transact()
    receipt = bot_contract.swap_via_contract(
        env.w3, bot_wallet, bot, quote_for(env), 3000, dry_run=False
    )
    tx = env.w3.eth.get_transaction(receipt["transactionHash"])
    assert (
        tx["gas"] >= receipt["gasUsed"] * 1.5
    )  # room for the state moving before it is mined


def test_dry_run_sends_nothing(env):
    before = env.w3.eth.block_number
    account = SimpleNamespace(address=env.operator)
    assert (
        bot_contract.swap_via_contract(env.w3, account, env.bot, quote_for(env), 3000)
        is None
    )
    assert env.w3.eth.block_number == before
    assert env.b.functions.balanceOf(env.bot.address).call() == 0


def test_dust_amount_gives_clear_error(env):
    account = SimpleNamespace(address=env.operator)
    with pytest.raises(ValueError, match="too small"):
        bot_contract.swap_via_contract(
            env.w3, account, env.bot, quote_for(env, 1) | {"amount_out_min": 0}, 3000
        )


def test_print_calldata_encodes_owner_call(env, capsys):
    bot_contract.print_calldata(env.bot, "withdraw", [env.a.address, A])
    out = capsys.readouterr().out
    expected = env.bot.encode_abi("withdraw", [env.a.address, A])
    assert expected in out and env.bot.address in out


@pytest.fixture
def cli(env, monkeypatch, capsys):
    # point the CLI helpers at the in-memory chain instead of Sepolia
    monkeypatch.setattr(bot_contract, "connect", lambda: (env.w3, None))
    monkeypatch.setattr(bot_contract, "load_bot", lambda w3, address: env.bot)
    monkeypatch.setenv("BOT_CONTRACT_ADDRESS", env.bot.address)
    monkeypatch.setenv("F564_ADDRESS", env.a.address)
    monkeypatch.setenv("FETH_ADDRESS", env.b.address)
    return capsys


def test_withdraw_calldata_converts_human_amounts(env, cli):
    bot_contract.cmd_withdraw_calldata("f564", "1.5")
    out = cli.readouterr().out
    assert env.bot.encode_abi("withdraw", [env.a.address, 15 * A // 10]) in out

    bot_contract.cmd_withdraw_calldata("f564", "all")  # the vault holds 5
    assert (
        env.bot.encode_abi("withdraw", [env.a.address, 5 * A]) in cli.readouterr().out
    )


def test_withdraw_calldata_rejects_more_than_the_balance(env, cli):
    with pytest.raises(SystemExit, match="between 0 and the contract balance"):
        bot_contract.cmd_withdraw_calldata("f564", "5.01")
    with pytest.raises(SystemExit):
        bot_contract.cmd_withdraw_calldata("f564", "0")


def test_generic_calldata_parses_each_argument_type(env, cli):
    bot_contract.cmd_calldata("setFeeAllowed", ["500", "true"])
    assert env.bot.encode_abi("setFeeAllowed", [500, True]) in cli.readouterr().out

    bot_contract.cmd_calldata("setTokenCap", [env.junk.address, str(A)])
    assert (
        env.bot.encode_abi("setTokenCap", [env.junk.address, A]) in cli.readouterr().out
    )

    with pytest.raises(SystemExit, match="Unknown function"):
        bot_contract.cmd_calldata("nope", [])
    with pytest.raises(SystemExit, match="wrong number"):
        bot_contract.cmd_calldata("setFeeAllowed", ["500"])
