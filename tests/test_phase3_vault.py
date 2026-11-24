import importlib.util
import json
import os
from types import SimpleNamespace

import pytest
from eth_tester.exceptions import TransactionFailed

import bot_contract
import operator_lock
import phase3_auto as auto
import phase3_vault as cli

A = 10**18


@pytest.fixture(scope="module")
def vault_lab(combined_artifacts):
    path = cli.config.ROOT / "research/2026-11-24-phase3/lab/phase3_credit_labkit.py"
    spec = importlib.util.spec_from_file_location("credit_lab", path)
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    lab = kit.Lab()
    w3 = lab.w3
    build = cli.build_input()
    if os.environ.get("PHASE3_TEST_MUTATION") == "operator":
        source = build["sources"][cli.SOURCE]
        source["content"] = source["content"].replace(
            'require(msg.sender == owner || msg.sender == operator, "not operator");',
            "",
        )
    artifact = cli.compile_vault(build)

    def deploy_mock(name, *args):
        item = combined_artifacts[name]
        return kit.deploy(w3, item["abi"], item["bin"], *args)

    fac = deploy_mock("MockArbFactory")
    router = deploy_mock("MockArbRouter", fac.address)
    q, b = lab.f564.address, lab.feth.address
    fac.functions.setPool(q, b, 3000, lab.pool.address).transact()
    owner, operator, outsider = w3.eth.accounts[5:8]
    vault = kit.deploy(
        w3,
        artifact["abi"],
        artifact["evm"]["bytecode"]["object"],
        owner,
        operator,
        lab.credit.address,
        router.address,
        q,
        b,
        lab.pool.address,
    )
    lab.pool.functions.setLiquidity(57341279504686431094117).transact()
    lab.pool.functions.setCardinality(180).transact()
    tester = w3.provider.ethereum_tester
    tester.time_travel(w3.eth.get_block("latest").timestamp + 1900)
    tester.mine_block()
    lab.feth.functions.mint(vault.address, 11 * A).transact()
    lab.f564.functions.mint(vault.address, 10000 * A).transact()
    return SimpleNamespace(**locals())


@pytest.fixture
def e(vault_lab):
    snap = vault_lab.tester.take_snapshot()
    yield vault_lab
    vault_lab.tester.revert_to_snapshot(snap)


def test_release_matches_compilation(e):
    assert e.artifact == json.loads(cli.RELEASE.read_text())


def test_worker_sizes_simulates_and_executes_atomic_close(e, monkeypatch, tmp_path):
    lab, w3 = e.lab, e.w3
    directory = tmp_path / "phase3"
    directory.mkdir()
    trader = auto.Trader.__new__(auto.Trader)
    contracts = {
        "credit": lab.credit,
        "pool": lab.pool,
        "vault": e.vault,
        "fETH": lab.feth,
        "f564": lab.f564,
    }

    def header(tag):
        h = w3.eth.get_block(tag)
        return h.number, w3.to_hex(h.hash), h.timestamp

    def views(block, specs):
        return [
            getattr(contracts[label].functions, name)(*args).call(
                block_identifier=block
            )
            for label, name, *args in specs
        ]

    trader.chain = SimpleNamespace(header=header, views=views)
    trader.validated, trader.last_hash = True, None
    trader.live, trader.directory, trader.artifact = True, directory, e.artifact
    trader.vault, trader.address, trader.sender = e.vault, e.vault.address, w3
    trader.signer = w3.eth.account.from_key(e.tester.backend.account_keys[6].to_bytes())
    assert trader.signer.address == e.operator
    monkeypatch.setattr(auto, "TOKENS", (e.q, e.b))
    monkeypatch.setattr(cli, "OPERATOR", e.operator)
    monkeypatch.setattr(cli, "CHAIN_ID", w3.eth.chain_id)
    monkeypatch.setattr(cli, "verify", lambda *args: e.vault)
    monkeypatch.setattr(auto, "END", 2**32)
    # Keep the strategy clock aligned with the time-travelled local price oracle.
    monkeypatch.setattr(auto, "time", SimpleNamespace(time=lambda: header("latest")[2]))
    monkeypatch.setattr(auto, "alert", lambda *args: None)
    # The local chain charges arbitrary native gas, unlike the Sepolia fee fixture.
    monkeypatch.setattr(auto, "MAX_GAS_COST", 10**18)
    monkeypatch.setattr(auto, "DAILY_GAS", 10**18)
    assert trader.step()["status"] == "awaiting_owner_limits"
    e.vault.functions.setLimits(e.q, (0, 6000 * A, 1000 * A)).transact(
        {"from": e.owner}
    )
    assert trader.step()["status"] == "watching"
    borrower = {"from": lab.bob}
    lab.c.deposit(e.b, 20 * A).transact(borrower)
    lab.c.borrow(e.q, 11900 * A).transact(borrower)
    lab.set_price(700)
    e.router.functions.setRoute(e.b, 3000, (700, 1, 100 * A, 0, False)).transact()
    base_before = lab.feth.functions.balanceOf(e.vault.address).call()
    result = trader.step()
    assert result["status"] == "confirmed" and result["borrower"] == lab.bob
    records = list((directory / "actions").glob("auto-*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text())["gas_cost_wei"] > 0
    assert lab.f564.functions.balanceOf(e.vault.address).call() > 10010 * A
    assert lab.feth.functions.balanceOf(e.vault.address).call() == base_before
    assert not (directory / "pending.json").exists()


def test_auto_reserves_and_native_budget(tmp_path):
    assert auto.capacity(6800 * A, (0, 0, 5000 * A), 0) == 800 * A
    assert auto.capacity(8 * A, (0, 7 * A, 0), 1) == 0
    assert auto.capacity(8 * A, (0, 8 * A, 4 * A), 1) == 0
    assert auto.capacity(8 * A, (0, 0, 4 * A), 1) == A
    actions = tmp_path / "actions"
    actions.mkdir()
    (actions / "auto-20261124-ab.json").write_text(
        json.dumps({"gas_cost_wei": auto.DAILY_GAS - 100})
    )
    auto.fee_budget(tmp_path, "20261124", 100)
    with pytest.raises(cli.ConfigurationError, match="daily operator gas"):
        auto.fee_budget(tmp_path, "20261124", 101)


def test_operator_access_and_owner_custody(e):
    v = e.vault.functions
    with pytest.raises(TransactionFailed, match="not operator"):
        v.deposit(e.b, A).transact({"from": e.outsider})
    for fn in (
        v.withdrawToOwner(e.b, A),
        v.setBorrowingEnabled(True),
        v.setLimits(e.b, (A, 0, A)),
        v.setOperator(e.outsider),
    ):
        with pytest.raises(TransactionFailed, match="not owner"):
            fn.transact({"from": e.operator})
    v.withdrawToOwner(e.b, A).transact({"from": e.owner})
    assert e.lab.feth.functions.balanceOf(e.owner).call() == A


def test_cumulative_mixed_debt_limits_and_recovery(e):
    v, c = e.vault.functions, e.lab.c
    op, owner = {"from": e.operator}, {"from": e.owner}
    with pytest.raises(TransactionFailed, match="liquid reserve"):
        v.deposit(e.b, 11 * A).transact(op)
    v.deposit(e.b, 10 * A).transact(op)
    with pytest.raises(TransactionFailed, match="borrowing disabled"):
        v.borrow(e.q, A).transact(op)
    v.setLimits(e.q, (3000 * A, 1000 * A, 0)).transact(owner)
    v.setLimits(e.b, (5 * A, A, 0)).transact(owner)
    v.setBorrowingEnabled(True).transact(owner)
    v.borrow(e.q, 2000 * A).transact(op)
    with pytest.raises(TransactionFailed, match="debt cap"):
        v.borrow(e.q, 1001 * A).transact(op)
    with pytest.raises(TransactionFailed, match="vault LTV"):
        v.borrow(e.b, 3 * A).transact(op)
    v.borrow(e.b, A).transact(op)
    with pytest.raises(TransactionFailed, match="vault LTV"):
        v.withdrawCollateral(e.b, 3 * A).transact(op)
    v.setBorrowingEnabled(False).transact(owner)
    v.deposit(e.q, 300 * A).transact(op)
    v.repay(e.q, 3000 * A).transact(op)
    assert c.debtOf(e.vault.address, e.q).call() < A // 10**6
    v.withdrawCollateral(e.q, 300 * A).transact(op)
    assert (
        e.lab.f564.functions.allowance(e.vault.address, e.lab.credit.address).call()
        == 0
    )
    assert c.debtOf(e.operator, e.b).call() == 0


def test_atomic_liquidation_restores_inventory_and_reverts_whole_failed_exit(e):
    lab, vault = e.lab, e.vault
    v, borrower = vault.functions, {"from": lab.bob}
    lab.c.deposit(e.b, 20 * A).transact(borrower)
    lab.c.borrow(e.q, 11900 * A).transact(borrower)
    lab.set_price(700)
    op = {"from": e.operator}
    lot = (
        lab.bob,
        e.q,
        e.b,
        5000 * A,
        7 * A,
        e.w3.eth.get_block("latest").timestamp + 100,
    )
    with pytest.raises(TransactionFailed, match="lot limits"):
        v.liquidate(lot).transact(op)
    v.setLimits(e.q, (0, 1000 * A, 5000 * A)).transact({"from": e.owner})

    def wallet():
        return tuple(
            t.functions.balanceOf(vault.address).call() for t in (lab.f564, lab.feth)
        )

    before = wallet()
    collateral = lab.c.collateralOf(lab.bob, e.b).call()
    for rate, available, ignore, reason in (
        (600, 100 * A, True, "insufficient gain"),
        (900, 6 * A, True, "partial swap"),
    ):
        e.router.functions.setRoute(
            e.b, 3000, (rate, 1, available, 0, ignore)
        ).transact()
        with pytest.raises(TransactionFailed, match=reason):
            v.liquidateAndClose(lot, A, 2**96).transact(op)
        assert (
            wallet() == before and lab.c.collateralOf(lab.bob, e.b).call() == collateral
        )
    e.router.functions.setRoute(e.b, 3000, (700, 1, 100 * A, 0, False)).transact()
    v.liquidateAndClose(lot, A, 2**96).transact(op)
    after = wallet()
    assert after[0] >= before[0] + A and after[1] == before[1]
    assert lab.feth.functions.allowance(vault.address, e.router.address).call() == 0
    assert lab.f564.functions.allowance(vault.address, lab.credit.address).call() == 0


def test_unknown_send_blocks_other_commands_and_reuse(tmp_path, monkeypatch):
    pending = tmp_path / "phase3/pending.json"
    dest = pending.parent / "deployment.json"
    signer = SimpleNamespace(address=cli.OPERATOR)
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            estimate_gas=lambda tx: 120,
            get_transaction_count=lambda *args: 3,
            max_priority_fee=2,
            get_block=lambda n: {"baseFeePerGas": 10},
            get_balance=lambda a: 10**18,
        )
    )

    def unknown(*args, **kwargs):
        assert json.loads(pending.read_text())["nonce"] == 3
        raise bot_contract.SentUnknown("unknown")

    monkeypatch.setattr(bot_contract, "send_tx", unknown)
    with pytest.raises(bot_contract.SentUnknown):
        cli.broadcast(w3, signer, {"data": "0xff"}, dest, pending, {})
    with (
        pytest.raises(operator_lock.SendBlocked),
        operator_lock.sender(tmp_path, "events"),
    ):
        pytest.fail("sender reopened")
    with pytest.raises(ValueError, match="already recorded"):
        cli.broadcast(w3, signer, {"data": "0xff"}, dest, pending, {})


def test_creation_gas_fits_sepolia_and_fee_budget(tmp_path, monkeypatch):
    signer = SimpleNamespace(address=cli.OPERATOR)
    eth = SimpleNamespace(
        estimate_gas=lambda tx: 15_287_046,
        get_transaction_count=lambda *a: 4,
        max_priority_fee=2,
        get_block=lambda n: {"baseFeePerGas": 17},
        get_balance=lambda a: 10**18,
    )

    def inspect(w3, account, tx, gas_buffer):
        assert tx["gas"] == 2**24 and gas_buffer == 1
        assert tx["gas"] * tx["maxFeePerGas"] <= 10**15
        raise bot_contract.NotSent("stop before signing")

    monkeypatch.setattr(bot_contract, "send_tx", inspect)
    pending = tmp_path / "pending.json"
    with pytest.raises(bot_contract.NotSent):
        cli.broadcast(
            SimpleNamespace(eth=eth),
            signer,
            {"data": "0xfa"},
            tmp_path / "deployment.json",
            pending,
            {},
        )
    assert not pending.exists()
    eth.get_block = lambda n: {"baseFeePerGas": 10**9}
    with pytest.raises(ValueError, match="gas budget"):
        cli.broadcast(
            SimpleNamespace(eth=eth),
            signer,
            {"data": "0xfa"},
            tmp_path / "deployment.json",
            pending,
            {},
        )
