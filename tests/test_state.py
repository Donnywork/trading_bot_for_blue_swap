import json
from decimal import Decimal

import pytest

import state

CHAIN = {
    "base": "fETH",
    "quote": "f564",
    "price": 1050.0,
    "vault_base": Decimal("0.5"),
    "vault_quote": Decimal("12.5"),
    "operator_eth": Decimal("0.05"),
}


def test_a_saved_state_loads_back(tmp_path):
    state.save(tmp_path / "state.json", 1_700_000_000.9, CHAIN)
    # (the pool depth is 1.0, as saved, until the bot has measured it, and there is no answer to
    # a scan request until it has served one)
    assert state.load(tmp_path / "state.json") == {
        **CHAIN,
        "updated": 1_700_000_000.0,
        "depth_scale": 1.0,
        "scan": None,
    }


def test_the_answer_to_a_scan_request_loads_back(tmp_path):
    done = {"done": 1_700_000_100, "block": 12345, "at": 1_700_000_130.5, "error": None}
    state.save(tmp_path / "state.json", 1, {**CHAIN, "scan": done})
    assert state.load(tmp_path / "state.json")["scan"] == done
    failed = {"done": 7, "error": "node said no"}
    state.save(tmp_path / "state.json", 1, {**CHAIN, "scan": failed})
    assert state.load(tmp_path / "state.json")["scan"] == {
        "done": 7,
        "block": None,
        "at": None,
        "error": "node said no",
    }


def test_the_pool_depth_the_bot_measured_loads_back(tmp_path):
    state.save(tmp_path / "state.json", 1, {**CHAIN, "depth_scale": 0.62})
    assert state.load(tmp_path / "state.json")["depth_scale"] == 0.62


def test_no_file_yet_is_none_not_an_error(tmp_path):
    assert state.load(tmp_path / "state.json") is None


def test_the_file_holds_no_secret_only_the_chain_figures(tmp_path):
    state.save(tmp_path / "state.json", 1, CHAIN)
    keys = set(json.loads((tmp_path / "state.json").read_text()))
    assert keys == {"time", *CHAIN}


@pytest.mark.parametrize(
    "text",
    [
        "{nope",
        "[]",
        "null",
        '{"base": "a"}',  # missing keys
        json.dumps({**CHAIN, "time": 1, "price": "nan"}, default=str),
        json.dumps({**CHAIN, "time": 1, "price": 0}, default=str),
        json.dumps({**CHAIN, "time": 1, "vault_base": "inf"}, default=str),
        json.dumps({**CHAIN, "time": 1, "operator_eth": "x"}, default=str),
        json.dumps({**CHAIN, "time": 1, "operator_eth": None}, default=str),
        json.dumps({**CHAIN, "time": 1, "depth_scale": -1}, default=str),
        json.dumps({**CHAIN, "time": 1, "depth_scale": "inf"}, default=str),
        json.dumps({**CHAIN, "time": 1, "depth_scale": "x"}, default=str),
        json.dumps({**CHAIN, "time": 1, "scan": "yes"}, default=str),
        json.dumps({**CHAIN, "time": 1, "scan": {"block": 1}}, default=str),
        json.dumps({**CHAIN, "time": 1, "scan": {"done": "x"}}, default=str),
    ],
)
def test_an_invalid_file_is_an_error_naming_the_file(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="state.json"):
        state.load(path)
