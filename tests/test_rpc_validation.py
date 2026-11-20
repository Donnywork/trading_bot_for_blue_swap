"""Real Web3 onion, encoding and HTTP transport; no live network or signing."""

import json

import pytest
import requests
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ExtraDataLengthError, Web3ValidationError

import config
import rpc_metrics as rpc
from rpc_validation import EventValidationMiddleware

CHAIN = 11155111
ADDRESS = "0x" + "11" * 20


@pytest.fixture
def http_transport(monkeypatch):
    class Transport:
        def __init__(self):
            self.seen = []
            self.chain = {"first": CHAIN, "second": 1}
            self.fail_first = False
            self.extra_data = "0x1234"

        def send(self, request):
            payload = json.loads(request.body)
            host = "first" if "first.invalid" in request.url else "second"
            self.seen.append((host, payload["method"], payload["params"]))
            if host == "first" and self.fail_first:
                raise requests.Timeout("offline failover")
            method = payload["method"]
            if method == "eth_chainId":
                result = hex(self.chain[host])
            elif method == "eth_call":
                result = "0x1234"
            elif method == "eth_estimateGas":
                result = "0x186a0"
            elif method in ("eth_getBlockByNumber", "eth_getBlockByHash"):
                result = {
                    "number": "0x7",
                    "timestamp": "0x63",
                    "hash": "0x" + "ab" * 32,
                    "extraData": self.extra_data,
                }
            elif method == "eth_sendRawTransaction":
                result = "0x" + "cd" * 32
            else:
                pytest.fail(f"Unexpected RPC method: {method}")
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {"id": payload["id"], "jsonrpc": "2.0", "result": result}
            ).encode()
            return response

    transport = Transport()
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter,
        "send",
        lambda _, request, **kwargs: transport.send(request),
    )
    return transport


def connection(optimized, *, failover=False):
    metrics = rpc.RpcMetrics()
    providers = [
        config.ThrottledProvider(
            f"http://{host}.invalid",
            metrics=metrics,
            exception_retry_configuration=None,
        )
        for host in ("first", "second")
    ]
    w3 = Web3(config.FailoverProvider(providers) if failover else providers[0])
    if optimized:
        w3.middleware_onion.replace("validation", EventValidationMiddleware)
    return w3, metrics


@pytest.mark.parametrize("method", ["call", "estimate_gas"])
@pytest.mark.parametrize("declared", [False, True])
def test_call_and_estimate_match_default_with_fewer_http_reads(
    http_transport, method, declared
):
    tx = {"to": ADDRESS, "data": "0x12345678", "value": 0}
    if declared:
        tx["chainId"] = hex(CHAIN)
    outputs, sequences = [], []
    for optimized in (False, True):
        http_transport.seen.clear()
        w3, metrics = connection(optimized)
        with rpc.purpose(rpc.SNAPSHOT):
            outputs.append(getattr(w3.eth, method)(tx, block_identifier=7))
        sequence = http_transport.seen.copy()
        sequences.append(sequence)
        counts = metrics.snapshot()
        assert counts["total"] == counts["http_attempts"] == len(sequence)
        assert counts["by_purpose"][rpc.SNAPSHOT] == len(sequence)
    endpoint = "eth_call" if method == "call" else "eth_estimateGas"
    assert [row[1] for row in sequences[0]] == [
        "eth_chainId",
        endpoint,
        "eth_chainId",
    ]
    assert [row[1] for row in sequences[1]] == (
        ["eth_chainId", endpoint] if declared else [endpoint]
    )
    assert sequences[0][1] == sequences[1][-1]
    assert "chainId" not in sequences[1][-1][2][0]
    assert sequences[1][-1][2][1] == "0x7"
    assert (
        outputs[0]
        == outputs[1]
        == (HexBytes("0x1234") if method == "call" else 100_000)
    )
    assert ("chainId" in tx) == declared  # Normalization never mutates caller data.


@pytest.mark.parametrize("method", ["call", "estimate_gas"])
@pytest.mark.parametrize("chain_id", [1, "0x1", None, 0, "invalid", {}, []])
@pytest.mark.parametrize("raw_params", [False, True])
def test_declared_chain_validation_matches_default(
    http_transport, method, chain_id, raw_params
):
    errors = []
    for optimized in (False, True):
        http_transport.seen.clear()
        w3, _ = connection(optimized)
        tx = {"to": ADDRESS, "chainId": chain_id}
        with pytest.raises((ValueError, TypeError, Web3ValidationError)) as failure:
            if raw_params:
                # Exercise the middleware even for values Eth's input formatter
                # rejects first (notably null), matching the default's exact error.
                endpoint = "eth_call" if method == "call" else "eth_estimateGas"
                w3.manager.request_blocking(endpoint, [tx, "latest"])
            else:
                getattr(w3.eth, method)(tx)
        errors.append((type(failure.value), str(failure.value)))
        assert all(row[1] == "eth_chainId" for row in http_transport.seen)
    assert errors[0] == errors[1]


def test_declared_chain_queries_active_failover_provider(http_transport):
    w3, metrics = connection(True, failover=True)
    tx = {"to": ADDRESS, "chainId": CHAIN}
    assert w3.eth.call(tx) == HexBytes("0x1234")
    http_transport.fail_first = True
    # The next validation read itself fails over, exposing the new node's chain.
    with pytest.raises(Web3ValidationError, match="connected node is on 1"):
        w3.eth.call(tx)
    assert [(host, method) for host, method, _ in http_transport.seen] == [
        ("first", "eth_chainId"),
        ("first", "eth_call"),
        ("first", "eth_chainId"),
        ("second", "eth_chainId"),
    ]
    # A later declaration must also use the now-active provider, without a cache.
    assert w3.eth.estimate_gas({"to": ADDRESS, "chainId": 1}) == 100_000
    assert [row[:2] for row in http_transport.seen[-2:]] == [
        ("second", "eth_chainId"),
        ("second", "eth_estimateGas"),
    ]
    counts = metrics.snapshot()
    assert counts["http_attempts"] == counts["total"] == len(http_transport.seen)


@pytest.mark.parametrize("block", [7, "0x" + "ab" * 32])
def test_block_formatting_and_extradata_validation_preserved(http_transport, block):
    outputs = []
    for optimized in (False, True):
        w3, _ = connection(optimized)
        http_transport.extra_data = "0x1234"
        value = w3.eth.get_block(block)
        assert value.number == 7 and value.timestamp == 99
        assert value.extraData == HexBytes("0x1234")
        outputs.append(value)
        http_transport.extra_data = "0x" + "01" * 33
        with pytest.raises(ExtraDataLengthError):
            w3.eth.get_block(block)
    assert outputs[0] == outputs[1]
    assert all(row[1] != "eth_chainId" for row in http_transport.seen)


def test_raw_send_identical_bytes_and_failover_are_unchanged(http_transport):
    sequences = []
    for optimized in (False, True):
        http_transport.seen.clear()
        http_transport.fail_first = True
        w3, _ = connection(optimized, failover=True)
        # Deliberately not a signed transaction; only the fake HTTP adapter sees it.
        assert w3.eth.send_raw_transaction(b"\x01\x02\x03") == HexBytes("cd" * 32)
        sequences.append(http_transport.seen.copy())
    assert (
        sequences[0]
        == sequences[1]
        == [
            ("first", "eth_sendRawTransaction", ["0x010203"]),
            ("second", "eth_sendRawTransaction", ["0x010203"]),
        ]
    )
