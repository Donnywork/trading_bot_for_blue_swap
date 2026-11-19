import argparse
import importlib
import sys

import pytest
from web3.exceptions import ContractLogicError, Web3RPCError

import event_replay
import event_service
import runtime
from event_settings import EventSettings


def test_environment_uses_project_path_and_preserves_exports(tmp_path, monkeypatch):
    settings = tmp_path / "settings.txt"
    settings.write_text("TEST_CONFIG_EXPORT=from-file\nTEST_CONFIG_LOCAL=local\n")
    monkeypatch.setattr(runtime, "ENV_FILE", settings)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    monkeypatch.setenv("TEST_CONFIG_EXPORT", "exported")
    monkeypatch.setenv("TEST_CONFIG_LOCAL", "placeholder")
    monkeypatch.delenv("TEST_CONFIG_LOCAL")
    monkeypatch.chdir(tmp_path.parent)
    runtime.load_environment()
    import os

    assert os.environ["TEST_CONFIG_EXPORT"] == "exported"
    assert os.environ["TEST_CONFIG_LOCAL"] == "local"


@pytest.mark.parametrize(
    "module",
    [
        "legacy_main",
        "config",
        "bot_contract",
        "status",
        "dashboard",
        "event_service",
        "phase2_contract",
    ],
)
def test_runtime_entrypoints_share_environment_loader(module):
    assert importlib.import_module(module).load_dotenv is runtime.load_environment


@pytest.mark.parametrize(
    "flags,loads", [([], True), (["--no-env"], False), (["--load-env"], True)]
)
def test_event_startup_loads_configuration_before_settings(monkeypatch, flags, loads):
    calls = []
    monkeypatch.setattr(sys, "argv", ["event_service.py", *flags])
    monkeypatch.setattr(event_service, "load_dotenv", lambda: calls.append("env"))

    def settings():
        calls.append("settings")
        raise runtime.ConfigurationError("test stops before RPC")

    monkeypatch.setattr(event_service.EventSettings, "from_env", settings)
    with pytest.raises(runtime.ConfigurationError):
        event_service.main()
    assert calls == (["env"] if loads else []) + ["settings"]


def test_event_help_does_not_load_configuration(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["event_service.py", "--help"])
    monkeypatch.setattr(
        event_service, "load_dotenv", lambda: pytest.fail("help loaded environment")
    )
    with pytest.raises(SystemExit) as exc:
        event_service.main()
    assert exc.value.code == 0
    assert "--no-env" in capsys.readouterr().out


@pytest.mark.parametrize(
    "field",
    [
        "EVENT_QUOTE_CAP",
        "EVENT_BASE_CAP",
        "EVENT_MIN_PROFIT",
        "EVENT_MAX_FEE_WEI",
        "EVENT_COOLDOWN_SECONDS",
        "OWNER_ADDRESS",
        "EVENT_MARKET_BOTS",
    ],
)
def test_bad_settings_name_the_field_without_echoing_value(field):
    with pytest.raises(runtime.ConfigurationError) as exc:
        EventSettings.from_env({field: "sensitive-invalid-input"})
    assert field in str(exc.value)
    assert "sensitive-invalid-input" not in str(exc.value)


def test_diagnostics_keep_location_without_provider_body():
    try:
        raise ConnectionError("https://provider.invalid/private-credential")
    except ConnectionError as exc:
        message = runtime.error_summary(exc)
    assert "ConnectionError" in message and "test_runtime.py:" in message
    assert (
        "https://provider.invalid" not in message
        and "private-credential" not in message
    )


def test_contract_revert_reason_is_published_bounded_and_normalized():
    try:
        raise ContractLogicError(
            "execution reverted: VM Exception while processing transaction:\n\n"
            "revert   insufficient profit"
        )
    except ContractLogicError as exc:
        message = runtime.error_summary(exc)
    assert "ContractLogicError" in message and "test_runtime.py:" in message
    assert "revert insufficient profit" in message
    assert "\n" not in message and len(message) < 300
    try:
        raise ContractLogicError("x" * 500)
    except ContractLogicError as exc:
        assert len(runtime.error_summary(exc).rsplit(": ", 1)[1]) <= 160


@pytest.mark.parametrize(
    "message,rejected",
    [
        ("Under the Free tier plan up to a 10 block range", True),
        ("query returned more than 10000 results", True),
        ("Log response size exceeded", True),
        ("Please limit the query to 10 blocks", True),
        ("Too many results", True),
        ("Rate limit exceeded for block range", False),
        ("Daily quota exhausted for block range", False),
        ("Invalid API key for block range", False),
        ("Request limit exceeded", False),
        ("Invalid params", False),
    ],
)
def test_only_specific_rpc_range_errors_allow_narrowing(message, rejected):
    error = Web3RPCError(
        "provider-body", rpc_response={"error": {"code": -32005, "message": message}}
    )
    assert runtime.rejected_log_range(error) == rejected
    assert "provider-body" not in runtime.error_summary(error)


def test_replay_amount_is_exact_and_bounded():
    assert (
        event_replay.raw("123456789.123456789123456789") == 123456789123456789123456789
    )
    with pytest.raises(argparse.ArgumentTypeError):
        event_replay.raw(str(2**256))


@pytest.mark.parametrize(
    "name,value",
    [
        ("POLL_SECONDS", "0"),
        ("COOLDOWN_SECONDS", "-1"),
        ("MAX_TRADE_FAILURES", "0"),
        ("MAX_TRADE_AMOUNT", "NaN"),
        ("MAX_TRADE_AMOUNT", "0"),
        ("MIN_OPERATOR_ETH", "Infinity"),
    ],
)
def test_legacy_runtime_rejects_invalid_operating_limits(monkeypatch, name, value):
    import legacy_main as main

    monkeypatch.setenv("MAX_TRADE_AMOUNT", "1")
    monkeypatch.setenv(name, value)
    with pytest.raises(runtime.ConfigurationError, match=name):
        main.Settings.from_env()
