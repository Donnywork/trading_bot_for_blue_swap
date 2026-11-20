from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_event_service import candidate, fake_executor
from web3 import Web3
from web3.exceptions import ContractLogicError

import bot_contract
from phase2_contract import load_abi
from runtime import ConfigurationError

A = 10**18


@pytest.fixture
def submission(tmp_path, monkeypatch):
    ex, _ = fake_executor(tmp_path)
    contract = Web3().eth.contract(address=ex.bot.address, abi=load_abi())
    fields = {
        "buyBase": True,
        "fee": 500,
        "amountIn": A,
        "amountOutMinimum": A // 2,
        "deadline": 129,
        "sqrtPriceLimitX96": 2**96,
    }
    hooks, calls = {}, []

    def prepare(fn):
        def estimate(tx):
            calls.append("estimate")
            if "estimate" in hooks:
                hooks["estimate"]()
            return 100_000

        def call(*args, **kwargs):
            calls.append("call")
            if "call" in hooks:
                hooks["call"]()
            return A

        def build(tx):
            calls.append("build")
            if "build" in hooks:
                hooks["build"]()
            return tx

        monkeypatch.setattr(fn, "estimate_gas", estimate)
        monkeypatch.setattr(fn, "call", call)
        monkeypatch.setattr(fn, "build_transaction", build)
        return fn

    def guard(action):
        calls.append("guard")
        if "guard" in hooks:
            hooks["guard"]()

    ex.entry_guard = guard
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: pytest.fail("unexpected broadcast")
    )
    return SimpleNamespace(**locals())


def open_position(ex):
    position = {
        "base_raw": A,
        "quote_raw": A,
        "opened": 90,
        "target": candidate()["hash"],
        "target_nonce": 1,
        "target_sender": candidate()["swap"]["sender"],
        "exit_fee": 10000,
        "entry_tx": "0x" + "cd" * 32,
        "entry_block": 7,
        "entry_block_hash": candidate()["snapshot_hash"],
        "entry_fee": 500,
        "entry_time": 90,
    }
    ex.store.data["position"] = position
    return position


@pytest.mark.parametrize(
    "mode,action,stage",
    [
        (mode, action, stage)
        for mode in ("live", "simulate")
        for action in ("entry", "exit")
        for stage in ("estimate", "guard" if action == "entry" else "call", "build")
        if stage != "build" or mode == "live"
    ],
)
@pytest.mark.parametrize("now", [115, 116, 125])
def test_own_swap_deadline_after_blocking_preparation(
    submission, mode, action, stage, now, monkeypatch
):
    f = submission
    f.ex.settings = replace(f.ex.settings, mode=mode)
    fields = f.fields | {"buyBase": action == "entry"}
    fn = f.prepare(f.contract.functions.swap(tuple(fields.values())))
    position = open_position(f.ex) if action == "exit" else None

    def advance():
        # The header (99) remains acceptable even at 125, and the target's
        # deadline (200) is still safe. Only our own deadline (129) is tight.
        f.ex.clock = lambda: now

    f.hooks[stage] = advance
    sends = []
    if now == 115:

        def send(*args, **kwargs):
            assert f.ex.store.data["intent"] is not None
            assert kwargs["gas_buffer"] == 2.0
            sends.append(True)
            raise bot_contract.NotSent("test pre-send refusal")

        monkeypatch.setattr(bot_contract, "send_tx", send)
        result = f.ex.perform("test", fn, action, A, A // 2, candidate())
        assert result["status"] == ("not_sent" if mode == "live" else "simulated")
        assert result["fee"] == 500
        assert sends == ([True] if mode == "live" else [])
    else:
        monkeypatch.setattr(
            f.ex.store, "begin", lambda *a: pytest.fail("unexpected durable intent")
        )
        with pytest.raises(
            ConfigurationError, match="single swap deadline cannot cover inclusion"
        ):
            f.ex.perform("test", fn, action, A, A // 2, candidate())
        assert f.ex.store.data["attempts"] == {}
        assert f.ex.store.data["trades"] == []
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["position"] == position
    assert f.calls.count("estimate") == 1
    assert f.calls.count("build") == (mode == "live")
    assert f.calls.count("guard") == (action == "entry")
    assert f.calls.count("call") == (action == "exit")


@pytest.mark.parametrize("named_struct", [False, True])
@pytest.mark.parametrize("keyword", [False, True])
def test_real_web3_swap_arguments_supply_encoded_deadline(
    submission, named_struct, keyword
):
    f = submission
    struct = f.fields if named_struct else tuple(f.fields.values())
    fn = (
        f.contract.functions.swap(s=struct)
        if keyword
        else f.contract.functions.swap(struct)
    )
    assert fn.arguments == (struct,)
    encoded = f.contract.encode_abi("swap", args=fn.arguments)
    if not named_struct:
        # single() binds a positional tuple in production.
        assert fn._encode_transaction_data() == encoded
    _, decoded = f.contract.decode_function_input(encoded)
    assert decoded["s"]["deadline"] == 129
    assert decoded["s"]["fee"] == 500
    f.prepare(fn)
    f.ex.settings = replace(f.ex.settings, mode="simulate")
    f.ex.clock = lambda: 116
    with pytest.raises(
        ConfigurationError, match="single swap deadline cannot cover inclusion"
    ):
        f.ex.perform("test", fn, "entry", A, A // 2, candidate())
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        (),
        {"s": {}},
        ((True, 500, A, A // 2),),
        {"s": {"fee": 500, "deadline": 129}},
        ((True, 500, A, A // 2, "129", 2**96),),
        ((True, 500, A, A // 2, True, 2**96),),
        ((True, 500, A, A // 2, 129.0, 2**96),),
        ((True, 500, A, A // 2, 0, 2**96),),
    ],
)
def test_real_web3_missing_or_invalid_metadata_fails_closed(submission, arguments):
    f = submission
    fn = f.prepare(f.contract.functions.swap(tuple(f.fields.values())))
    fn.arguments = arguments
    with pytest.raises(
        ConfigurationError, match="single swap deadline metadata invalid"
    ):
        f.ex.perform("test", fn, "entry", A, A // 2, candidate())
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}


@pytest.mark.parametrize("action", ["entry", "exit"])
def test_missing_single_arguments_fail_closed_for_any_adapter(
    tmp_path, monkeypatch, action
):
    ex, fn = fake_executor(tmp_path)
    del fn.arguments
    monkeypatch.setattr(
        ex.store, "begin", lambda *a: pytest.fail("unexpected durable intent")
    )
    monkeypatch.setattr(
        bot_contract, "send_tx", lambda *a, **k: pytest.fail("unexpected broadcast")
    )
    with pytest.raises(
        ConfigurationError, match="single swap deadline metadata invalid"
    ):
        ex.perform("test", fn, action, A, A // 2, candidate())
    assert ex.store.data["intent"] is None
    assert ex.store.data["attempts"] == {}


def test_outer_named_argument_metadata(submission):
    f = submission
    fn = f.prepare(f.contract.functions.swap(tuple(f.fields.values())))
    fn.arguments = {"s": f.fields}
    f.ex.clock = lambda: 116
    with pytest.raises(
        ConfigurationError, match="single swap deadline cannot cover inclusion"
    ):
        f.ex.perform("test", fn, "entry", A, A // 2, candidate())
    assert f.ex.store.data["intent"] is None


@pytest.mark.parametrize("action", ["entry", "exit"])
def test_single_estimate_revert_preserves_original_reason(submission, action):
    f = submission
    fn = f.prepare(f.contract.functions.swap(tuple(f.fields.values())))

    def revert():
        f.ex.clock = lambda: 125
        raise ContractLogicError("execution reverted: SPL")

    f.hooks["estimate"] = revert
    with pytest.raises(ContractLogicError, match="SPL"):
        f.ex.perform("test", fn, action, A, A // 2, candidate())
    assert f.ex.store.data["intent"] is None
    assert f.ex.store.data["attempts"] == {}
