import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from eth_tester.exceptions import TransactionFailed
from web3 import Web3

import event_execution
import event_market as market
import event_snapshot
import phase2_contract
from event_execution import Executor
from event_settings import EventSettings
from event_store import EventStore

A = 10**18
LIMIT = 2**96


def single(e, **changes):
    values = {
        "buyBase": True,
        "fee": 500,
        "amountIn": A,
        "amountOutMinimum": A,
        "deadline": e.w3.eth.get_block("latest")["timestamp"] + 120,
        "sqrtPriceLimitX96": LIMIT,
    }
    return tuple((values | changes).values())


def cycle(e, **changes):
    values = {
        "buyPool0": True,
        "amountIn": A,
        "minBaseOut": A,
        "minQuoteOut": 2 * A,
        "minProfit": A // 10,
        "deadline": e.w3.eth.get_block("latest")["timestamp"] + 120,
        "buySqrtPriceLimitX96": LIMIT,
        "sellSqrtPriceLimitX96": LIMIT,
    }
    return tuple((values | changes).values())


def balances(e):
    return tuple(t.functions.balanceOf(e.bot.address).call() for t in (e.a, e.b))


def test_one_contract_holds_inventory_and_executes_both_routes(combined):
    e = combined
    assert e.bot.functions.contractVersion().call() == 2
    assert e.bot.functions.tokenCap(e.a.address).call() == 1000 * A
    assert e.bot.functions.tokenCap(e.b.address).call() == 10 * A
    assert e.bot.functions.tokenCap(e.stranger).call() == 0
    e.bot.functions.swap(single(e)).transact({"from": e.operator})
    assert balances(e) == (4999 * A, 4 * A)
    e.router.functions.setRoute(e.b.address, 10000, (2, 1, 10**30, 0, False)).transact()
    e.bot.functions.execute(cycle(e)).transact({"from": e.operator})
    assert balances(e) == (
        5000 * A,
        4 * A,
    )  # The cycle preserves the earlier entry and deposits.
    e.bot.functions.swap(
        single(e, buyBase=False, fee=10000, amountOutMinimum=2 * A)
    ).transact({"from": e.operator})
    assert balances(e) == (5002 * A, 3 * A)
    for token in (e.a, e.b):
        assert token.functions.allowance(e.bot.address, e.router.address).call() == 0
        assert token.functions.balanceOf(e.operator).call() == 0


def test_stranger_cannot_single_swap(combined):
    with pytest.raises(TransactionFailed, match="not operator"):
        combined.bot.functions.swap(single(combined)).transact(
            {"from": combined.stranger}
        )


def test_stranger_cannot_execute_cycle(combined):
    e = combined
    e.router.functions.setRoute(e.b.address, 10000, (2, 1, 10**30, 0, False)).transact()
    with pytest.raises(TransactionFailed, match="not operator"):
        e.bot.functions.execute(cycle(e)).transact({"from": e.stranger})


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"fee": 3000}, "fee not allowed"),
        ({"amountIn": 0}, "input cap"),
        ({"amountIn": 1001 * A}, "input cap"),
        ({"buyBase": False, "amountIn": 11 * A}, "input cap"),
        ({"amountOutMinimum": 0}, "zero minimum"),
        ({"deadline": 1}, "expired"),
        ({"sqrtPriceLimitX96": 0}, "bad price limit"),
    ],
)
def test_single_swap_limits(combined, changes, reason):
    e = combined
    before = balances(e)
    with pytest.raises(TransactionFailed, match=reason):
        e.bot.functions.swap(single(e, **changes)).transact({"from": e.operator})
    assert balances(e) == before


def test_single_partial_fill_and_excess_base_rollback(combined):
    e = combined
    for route, reason in (
        ((4, 1, A // 2, 0, False), "partial input"),
        ((11, 1, 10**30, 0, False), "base cap"),
    ):
        e.router.functions.setRoute(e.a.address, 500, route).transact()
        before = balances(e)
        with pytest.raises(TransactionFailed, match=reason):
            e.bot.functions.swap(single(e)).transact({"from": e.operator})
        assert balances(e) == before
        assert e.a.functions.allowance(e.bot.address, e.router.address).call() == 0


def test_cycle_failure_rolls_back_without_losing_existing_single_inventory(combined):
    e = combined
    e.bot.functions.swap(single(e)).transact({"from": e.operator})
    before = balances(e)
    with pytest.raises(TransactionFailed, match="Too little received"):
        e.bot.functions.execute(cycle(e, minQuoteOut=1)).transact({"from": e.operator})
    assert balances(e) == before
    e.bot.functions.swap(single(e, buyBase=False)).transact({"from": e.operator})
    assert balances(e) == (5000 * A, 3 * A)


def test_cycle_partial_fill_and_owner_profit_floor(combined):
    e = combined
    before = balances(e)
    with pytest.raises(TransactionFailed, match="profit floor"):
        e.bot.functions.execute(cycle(e, minProfit=1)).transact({"from": e.operator})
    e.router.functions.setRoute(e.a.address, 500, (4, 1, A // 2, 0, False)).transact()
    with pytest.raises(TransactionFailed, match="partial input"):
        e.bot.functions.execute(cycle(e)).transact({"from": e.operator})
    assert balances(e) == before


def test_single_reentrancy_and_owner_admin(combined):
    e = combined
    e.bot.functions.setOperator(e.router.address).transact()
    data = e.bot.encode_abi("swap", args=[single(e)])
    e.router.functions.setCallback(e.bot.address, data).transact()
    e.bot.functions.swap(single(e)).transact({"from": e.owner})
    assert not e.router.functions.reentrySucceeded().call()
    for fn in (
        e.bot.functions.withdraw(e.a.address, A),
        e.bot.functions.setLimits(A, A, 1),
        e.bot.functions.setPaused(True),
    ):
        with pytest.raises(TransactionFailed, match="not owner"):
            fn.transact({"from": e.stranger})
    e.bot.functions.transferOwnership(e.stranger).transact()
    assert e.bot.functions.owner().call() == e.owner
    e.bot.functions.acceptOwnership().transact({"from": e.stranger})
    e.bot.functions.withdraw(e.a.address, A).transact({"from": e.stranger})
    assert e.a.functions.balanceOf(e.stranger).call() == A


def test_contract_pause_blocks_both_functions(combined):
    e = combined
    e.bot.functions.setPaused(True).transact()
    for fn in (e.bot.functions.swap(single(e)), e.bot.functions.execute(cycle(e))):
        with pytest.raises(TransactionFailed, match="paused"):
            fn.transact({"from": e.operator})
    fresh = e.deploy("Phase2TradingBot", *e.args)
    assert fresh.functions.paused().call()


def test_settings_use_one_address_and_refuse_split_configuration():
    env = {
        "EVENT_EXECUTION_MODE": "simulate",
        "BOT_CONTRACT_ADDRESS": "0x" + "11" * 20,
        "OWNER_ADDRESS": "0x" + "22" * 20,
    }
    settings = EventSettings.from_env(env)
    assert settings.bot_address == env["BOT_CONTRACT_ADDRESS"]
    for key in ("ARBITRAGE_VAULT_ADDRESS", "EVENT_FRONT_VAULT_ADDRESS"):
        with pytest.raises(ValueError, match="obsolete"):
            EventSettings.from_env(env | {key: env["BOT_CONTRACT_ADDRESS"]})


def test_executor_initializes_one_contract_for_both_capabilities(
    combined, tmp_path, monkeypatch
):
    e = combined
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps({"abi": e.bot.abi}))
    monkeypatch.setattr(event_execution, "ARTIFACT", artifact)
    for key, value in (
        ("BASE", e.b.address),
        ("QUOTE", e.a.address),
        ("ROUTER", e.router.address),
    ):
        monkeypatch.setattr(market, key, value.lower())
    monkeypatch.setattr(
        market,
        "POOLS",
        {fee: p.address.lower() for fee, p in zip((500, 10000), e.pools, strict=True)},
    )

    class Eth:
        chain_id = 11155111

        def __getattr__(self, name):
            return getattr(e.w3.eth, name)

    w3 = SimpleNamespace(eth=Eth())
    settings = replace(
        EventSettings(),
        mode="simulate",
        bot_address=e.bot.address,
        owner=e.owner.lower(),
    )
    ex = Executor(w3, {}, settings, EventStore(tmp_path / "events", {}, "simulate"))
    assert ex.bot.address == e.bot.address
    assert ex.bot.functions.contractVersion().call() == 2
    with pytest.raises(ValueError, match="owner mismatch"):
        Executor(w3, {}, replace(settings, owner=e.stranger.lower()), ex.store)


def test_unsigned_plan_exact_limits_and_cli_offline(combined_artifacts, tmp_path):
    item = combined_artifacts["Phase2TradingBot"]
    artifact = {"abi": item["abi"], "evm": {"bytecode": {"object": item["bin"]}}}
    owner, operator = "0x" + "11" * 20, "0x" + "22" * 20
    plan = phase2_contract.deployment_plan(
        owner, operator, [100 * A, A, A // 10], artifact
    )
    assert plan["data"].startswith("0x" + item["bin"])
    assert plan["constructor"][-1] == [100 * A, A, A // 10]
    assert plan["constructor"][5] == [
        Web3.to_checksum_address(market.POOLS[f]) for f in (500, 10000)
    ]
    with pytest.raises(ValueError):
        phase2_contract.deployment_plan(owner, owner, [A, A, 1], artifact)
    output = tmp_path / "plan.json"
    command = [
        sys.executable,
        "phase2_contract.py",
        "prepare",
        "--owner",
        owner,
        "--operator",
        operator,
        "--quote-cap",
        "100",
        "--base-cap",
        "1",
        "--min-profit",
        "0.1",
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "no network" in result.stdout
    saved = json.loads(output.read_text())
    assert (
        saved["limits_raw"] == [100 * A, A, A // 10] and saved["chain_id"] == 11155111
    )
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    assert json.loads(output.read_text()) == saved


def test_owner_calldata_uses_isolated_artifact(
    combined_artifacts, tmp_path, monkeypatch, capsys
):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps({"abi": combined_artifacts["Phase2TradingBot"]["abi"]})
    )
    monkeypatch.setattr(phase2_contract, "ARTIFACT", artifact)
    monkeypatch.setattr(
        phase2_contract, "load_dotenv", lambda: pytest.fail("calldata loaded env")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "phase2_contract.py",
            "calldata",
            "--address",
            "0x" + "11" * 20,
            "setPaused",
            "false",
        ],
    )
    phase2_contract.main()
    assert "Send from the OWNER wallet" in capsys.readouterr().out


@pytest.mark.parametrize(
    "outcome", ["confirmed", "unknown", "reorg", "no_code", "reverted"]
)
def test_deployment_records_intent_before_send_and_never_retries(
    tmp_path, monkeypatch, outcome
):
    operator, owner, address = ("0x" + byte * 20 for byte in ("11", "22", "33"))
    plan = {
        "operator": operator,
        "owner": owner,
        "data": "0x01",
        "limits_raw": [1, 1, 1],
    }
    record = tmp_path / "deployment.json"
    account = SimpleNamespace(address=operator)
    eth = SimpleNamespace(
        chain_id=11155111,
        account=SimpleNamespace(from_key=lambda key: account),
        get_transaction_count=lambda *a: 8,
        estimate_gas=lambda tx: 1000000,
        get_block=lambda n: {"hash": b"wrong" if outcome == "reorg" else b"block"},
        get_code=lambda a: b"" if outcome == "no_code" else b"code",
    )
    monkeypatch.setenv("RPC_URL", "https://example.invalid")
    monkeypatch.setenv("PRIVATE_KEY", "test-placeholder-never-signed")
    monkeypatch.setattr(phase2_contract.config, "rpc_provider", lambda uri: None)
    monkeypatch.setattr(
        phase2_contract, "Web3", lambda provider: SimpleNamespace(eth=eth)
    )
    monkeypatch.setattr(phase2_contract.ledger, "LOG_DIR", tmp_path)
    monkeypatch.setattr(event_snapshot, "markets", lambda w3: {})
    monkeypatch.setattr("builtins.input", lambda prompt: "DEPLOY")
    calls = []

    def send(w3, signer, tx):
        saved = json.loads(record.read_text())
        assert saved["status"] == "intent" and saved["nonce"] == tx["nonce"] == 8
        calls.append(tx)
        if outcome == "unknown":
            raise OSError("provider details must not escape")
        return {
            "status": 0 if outcome == "reverted" else 1,
            "contractAddress": address,
            "blockNumber": 9,
            "blockHash": b"block",
            "transactionHash": b"tx",
        }

    # Keep Web3's pure hex conversion on the fake constructor used above.
    phase2_contract.Web3.to_hex = Web3.to_hex
    monkeypatch.setattr(phase2_contract.bot_contract, "send_tx", send)
    if outcome == "confirmed":
        phase2_contract.deploy(plan, record)
        assert json.loads(record.read_text())["address"] == address
    else:
        with pytest.raises(SystemExit):
            phase2_contract.deploy(plan, record)
        assert json.loads(record.read_text())["status"] == "intent"
    with pytest.raises(ValueError, match="record already exists"):
        phase2_contract.deploy(plan, record)
    assert len(calls) == 1


def test_deploy_refuses_changed_plan_before_loading_environment(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "owner": "owner",
                "operator": "operator",
                "limits_raw": [1, 1, 1],
                "data": "tampered",
            }
        )
    )
    monkeypatch.setattr(phase2_contract, "compile_contract", dict)
    monkeypatch.setattr(
        phase2_contract,
        "build_files",
        lambda: pytest.fail("deploy overwrote build artifacts"),
    )
    monkeypatch.setattr(
        phase2_contract, "deployment_plan", lambda *a: {"data": "reviewed"}
    )
    monkeypatch.setattr(
        phase2_contract,
        "load_dotenv",
        lambda *a: pytest.fail("loaded env before validating plan"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "phase2_contract.py",
            "deploy",
            "--plan",
            str(plan),
            "--record",
            str(tmp_path / "record.json"),
            "--load-env",
        ],
    )
    with pytest.raises(ValueError, match="differs"):
        phase2_contract.main()


def test_deploy_with_existing_record_does_no_build_or_env_work(tmp_path, monkeypatch):
    record = tmp_path / "record.json"
    record.write_text('{"status":"intent","nonce":8}')

    def forbidden(*args, **kwargs):
        pytest.fail("existing deployment triggered build, env or network work")

    monkeypatch.setattr(phase2_contract, "compile_contract", forbidden)
    monkeypatch.setattr(phase2_contract, "build_files", forbidden)
    monkeypatch.setattr(phase2_contract, "load_dotenv", forbidden)
    monkeypatch.setattr(phase2_contract, "deploy", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "phase2_contract.py",
            "deploy",
            "--plan",
            str(tmp_path / "missing.json"),
            "--record",
            str(record),
        ],
    )
    with pytest.raises(ValueError, match="record already exists"):
        phase2_contract.main()
    assert record.read_text() == '{"status":"intent","nonce":8}'
