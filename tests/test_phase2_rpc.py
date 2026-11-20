"""Exercise real Web3 encoding/middleware against a deterministic offline transport."""

import json
import runpy
import sys
import time
from collections import Counter
from dataclasses import replace

import pytest
import requests
from eth_abi import decode, encode
from eth_utils import function_abi_to_4byte_selector
from web3 import Web3

import config
import control
import event_execution
import event_market as market
import event_modes
import event_service
import event_snapshot as layout
import phase2_contract
import rpc_metrics as rpc
import runtime
import strategy
from event_store import EventStore
from rpc_validation import EventValidationMiddleware


class PoolTransport:
    def __init__(self):
        self.seed = json.loads(layout.RESEARCH_SNAPSHOT.read_text())
        self.profiles = {p["pool_address"].lower(): p for p in self.seed["profiles"]}
        self.head = self.seed["block"] + 1
        self.http = self.items = self.bitmap = 0
        self.methods = []
        self.abi = {
            function_abi_to_4byte_selector(fn): fn
            for abi in (
                config.pool_abi,
                config.router_abi,
                config.quoter_abi,
                config.erc20_abi,
            )
            for fn in abi
            if fn["type"] == "function"
        }

    def post(self, uri, data, **kwargs):
        self.http += 1
        payload = json.loads(data)

        def answer(req):
            self.items += 1
            self.methods.append(req["method"])
            return {
                "id": req["id"],
                "jsonrpc": "2.0",
                "result": self.result(req["method"], req["params"]),
            }

        return json.dumps(
            [answer(r) for r in reversed(payload)]
            if isinstance(payload, list)
            else answer(payload)
        ).encode()

    def result(self, method, params):
        if method == "eth_chainId":
            return hex(11155111)
        if method == "eth_getLogs":
            return []
        if method == "eth_getTransactionCount":
            return "0x2"
        if method == "eth_getBlockByNumber":
            n = self.head if params[0] == "latest" else int(params[0], 16)
            return {
                "number": hex(n),
                "timestamp": hex(
                    self.seed["timestamp"] + 12 * (n - self.seed["block"])
                ),
                "hash": self.seed["block_hash"]
                if n == self.seed["block"]
                else "0x" + f"{n:064x}",
            }
        assert method == "eth_call"
        tx = params[0]
        data = bytes.fromhex(tx["data"][2:])
        fn = self.abi[data[:4]]
        name = fn["name"]
        p = self.profiles.get(tx["to"].lower(), {})
        if name == "tickBitmap":
            self.bitmap += 1
            (word,) = decode(["int16"], data[4:])
            values = [
                sum(
                    1 << (t["tick"] // p["spacing"] % 256)
                    for t in p["ticks"]
                    if t["tick"] // p["spacing"] >> 8 == word
                )
            ]
        elif name == "ticks":
            (tick,) = decode(["int24"], data[4:])
            t = next(t for t in p["ticks"] if t["tick"] == tick)
            values = [t["gross"], t["net"], 0, 0, 0, 0, 0, True]
        elif name == "slot0":
            values = [p["sqrt" + "PriceX96"], p["tick"], 0, 0, 0, 0, True]
        else:
            values = [
                {
                    "token0": market.QUOTE,
                    "token1": market.BASE,
                    "decimals": 18,
                    "factory": layout.FACTORY,
                    "fee": p.get("fee"),
                    "tickSpacing": p.get("spacing"),
                    "liquidity": p.get("liquidity"),
                }[name]
            ]
        return "0x" + encode([v["type"] for v in fn["outputs"]], values).hex()


@pytest.fixture
def offline_http(monkeypatch):
    transport = PoolTransport()

    def send(_, req, **kw):
        response = requests.Response()
        response.status_code = 200
        response._content = transport.post(req.url, req.body)
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return transport


@pytest.mark.parametrize("optimized", [False, True])
def test_pool_request_cost(tmp_path, monkeypatch, offline_http, optimized):
    transport = offline_http
    sleeps = []
    monkeypatch.setattr(config.time, "sleep", sleeps.append)
    metrics = rpc.RpcMetrics()
    w3 = Web3(config.ThrottledProvider("http://offline.invalid", metrics=metrics))
    if optimized:
        w3.middleware_onion.replace("validation", EventValidationMiddleware)
    pools = layout.markets(w3)
    snapshots = layout.Snapshots(w3, pools, tmp_path / "snapshot.json")
    with rpc.purpose(rpc.SNAPSHOT):
        snapshots.refresh()
    startup = transport.items
    transport.head += 1
    snapshots.refresh()
    ordinary = transport.items - startup
    snapshots.refresh()
    repeated = transport.items - startup - ordinary
    assert transport.bitmap == 0
    before, http = transport.items, transport.http
    start = time.perf_counter()
    with rpc.purpose(rpc.SCAN):
        result = snapshots.refresh(scan=True)
    assert {p["fee"]: p["ticks"] for p in result["profiles"]} == {
        p["fee"]: p["ticks"] for p in transport.seed["profiles"]
    }
    measured = metrics.snapshot()
    assert measured["total"] == transport.items == sum(measured["by_purpose"].values())
    assert measured["http_attempts"] == transport.http
    assert measured["by_purpose"][rpc.SCAN] == transport.items - before
    assert (
        startup,
        ordinary,
        repeated,
        transport.items - before,
        transport.http - http,
    ) == ((23, 8, 1, 762, 49) if optimized else (55, 16, 1, 778, 65))
    assert not sleeps
    print("Full scan, fake HTTP transport (seconds):", time.perf_counter() - start)


def cycle_http(tmp_path, monkeypatch, offline_http, optimized):
    """Set up a real Web3 executor with synthetic HTTP chain responses."""
    transport = offline_http
    unit, sqrt = 10**18, 2**96
    settings = replace(
        event_service.EventSettings(),
        mode="simulate",
        bot_address="0x" + "33" * 20,
        owner="0x" + "22" * 20,
        quote_cap=640 * unit,
        min_profit=unit // 100,
    )
    transport.abi.update(
        {
            function_abi_to_4byte_selector(fn): fn
            for fn in phase2_contract.load_abi()
            if fn["type"] == "function"
        }
    )
    original = transport.result

    def result(method, params):
        if method == "eth_getBlockByNumber":
            return original(method, params) | {"baseFeePerGas": "0x1"}
        if method == "eth_getCode":
            return "0x01"
        if method == "eth_getBalance":
            return hex(10**20)
        if method == "eth_estimateGas":
            return hex(100_000)
        if method != "eth_call":
            return original(method, params)
        data = bytes.fromhex(params[0]["data"][2:])
        fn = transport.abi[data[:4]]
        name = fn["name"]
        if name == "quoteExactInputSingle":
            token_in, _, amount, _, limit = decode(
                ["address", "address", "uint256", "uint24", "uint160"], data[4:]
            )
            output = amount // 1000 if token_in == market.QUOTE else amount * 1100
            values = [output, (sqrt + limit) // 2, 0, 90_000]
        elif name == "slot0":
            values = [sqrt, 0, 0, 0, 0, 0, True]
        else:
            metadata = {
                "contractVersion": 2,
                "owner": settings.owner,
                "operator": "0x" + "11" * 20,
                "router": market.ROUTER,
                "quoteToken": market.QUOTE,
                "baseToken": market.BASE,
                "pool0": market.POOLS[500],
                "pool1": market.POOLS[10000],
                "maxQuoteIn": settings.quote_cap,
                "maxBaseIn": settings.base_cap,
                "minProfitFloor": settings.min_profit,
                "execute": unit,
            }
            if name not in metadata:
                return original(method, params)
            values = [metadata[name]]
        return "0x" + encode([v["type"] for v in fn["outputs"]], values).hex()

    monkeypatch.setattr(transport, "result", result)
    metrics = rpc.RpcMetrics()
    w3 = Web3(config.ThrottledProvider("http://offline.invalid", metrics=metrics))
    if optimized:
        w3.middleware_onion.replace("validation", EventValidationMiddleware)
    pools = layout.markets(w3)
    store = EventStore(tmp_path, {}, "simulate")
    executor = event_execution.Executor(
        w3, pools, settings, store, clock=lambda: transport.seed["timestamp"] + 13
    )
    candidate = {
        "hash": "0x" + "ab" * 32,
        "snapshot_block": transport.head,
        "snapshot_hash": "0x" + f"{transport.head:064x}",
        "backrun": {"buy_fee": 500, "sell_fee": 10000, "input_f564": 100},
    }
    return executor, candidate, metrics


@pytest.mark.parametrize("optimized", [False, True])
def test_simulated_cycle_http_cost(tmp_path, monkeypatch, offline_http, optimized):
    """Count one three-rung cycle after market/executor startup, before any send.

    Real Web3 contracts perform encoding, middleware, decoding and HTTP requests;
    only chain responses are synthetic. No receipt, worker or startup reads count.
    """
    executor, candidate, metrics = cycle_http(
        tmp_path, monkeypatch, offline_http, optimized
    )
    transport = offline_http
    before = transport.items
    outcome = executor.backrun(candidate)
    counts = Counter(transport.methods[before:])
    assert outcome["status"] == "simulated" and outcome["quote_rungs"] == 3
    assert outcome["gas_limit"] == 2 * outcome["estimated_gas"] == 200_000
    assert counts == {
        "eth_chainId": 2 if optimized else 28,
        "eth_call": 12,
        "eth_getBlockByNumber": 6,
        "eth_estimateGas": 1,
        "eth_getBalance": 1,
    }
    assert transport.items - before == (22 if optimized else 48)
    assert transport.http == metrics.snapshot()["http_attempts"] == transport.items


def test_optimized_market_startup_refuses_wrong_chain(monkeypatch, offline_http):
    original = offline_http.result
    monkeypatch.setattr(
        offline_http,
        "result",
        lambda method, params: (
            "0x1" if method == "eth_chainId" else original(method, params)
        ),
    )
    w3 = Web3(config.ThrottledProvider("http://offline.invalid"))
    w3.middleware_onion.replace("validation", EventValidationMiddleware)
    with pytest.raises(ValueError, match="event service requires Sepolia"):
        layout.markets(w3)
    # No untagged metadata calls, quotes, estimates or sends reach HTTP.
    assert offline_http.methods == ["eth_chainId"]


@pytest.mark.parametrize("guard", ["executor_startup", "cycle"])
def test_optimized_executor_refuses_chain_change_after_setup(
    tmp_path, monkeypatch, offline_http, guard
):
    executor, candidate, metrics = cycle_http(
        tmp_path, monkeypatch, offline_http, optimized=True
    )
    original = offline_http.result
    monkeypatch.setattr(
        offline_http,
        "result",
        lambda method, params: (
            "0x1" if method == "eth_chainId" else original(method, params)
        ),
    )
    before = offline_http.items
    if guard == "executor_startup":
        with pytest.raises(
            runtime.ConfigurationError, match="execution requires Sepolia"
        ):
            event_execution.Executor(
                executor.w3, executor.pools, executor.settings, executor.store
            )
        assert offline_http.methods[before:] == ["eth_chainId"]
    else:
        with pytest.raises(runtime.ConfigurationError, match="no exact profitable"):
            executor.backrun(candidate)
        methods = offline_http.methods[before:]
        # Each rung's explicit guard refuses the changed network before any
        # untagged slot0/quote call. The ladder reports no executable cycle.
        assert methods[0] == "eth_getBlockByNumber"
        assert methods[1:] and set(methods[1:]) == {"eth_chainId"}
    assert executor.store.data["intent"] is None
    assert executor.store.data["attempts"] == {}
    assert (
        offline_http.http == metrics.snapshot()["http_attempts"] == offline_http.items
    )


def test_metrics_count_retry_failover_and_batch(monkeypatch):
    from web3.providers.rpc.utils import ExceptionRetryConfiguration

    metrics = rpc.RpcMetrics()
    providers = [
        config.ThrottledProvider(
            "http://" + host + ".invalid",
            metrics=metrics,
            exception_retry_configuration=ExceptionRetryConfiguration(
                errors=(requests.Timeout,), retries=2, backoff_factor=0
            ),
        )
        for host in ("first", "second")
    ]
    requests_seen = []

    def send(_, req, **kw):
        requests_seen.append(req.url)
        if req.url.startswith("http://first."):
            raise requests.Timeout("private-error")
        items = json.loads(req.body)
        reply = requests.Response()
        reply.status_code = 200

        def ok(item):
            return {"id": item["id"], "jsonrpc": "2.0", "result": "0x01"}

        reply._content = json.dumps(
            [ok(i) for i in items] if isinstance(items, list) else ok(items)
        ).encode()
        return reply

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    provider = config.FailoverProvider(providers)
    provider.make_request("eth_chainId", [])
    with rpc.purpose(rpc.SCAN):
        assert provider.depth_calls([[{}, "0x1"]] * 20) == [b"\x01"] * 20
    result = metrics.snapshot()
    assert len(requests_seen) == result["http_attempts"] == 4
    assert result["total"] == sum(result["by_purpose"].values()) == 23
    assert result["by_purpose"][rpc.SCAN] == 20
    assert "invalid" not in json.dumps(result)


@pytest.mark.parametrize("fallback_status", [400, 413, 422, 401, 403, 429, 503])
def test_catchup_http_rejections(tmp_path, monkeypatch, caplog, fallback_status):
    transport = PoolTransport()
    transport.head += 24
    ranges = []
    accepted = []

    def send(_, req, **kwargs):
        payload = json.loads(req.body)
        response = requests.Response()
        response.status_code = 200
        response.url = "https://private.invalid/credential"
        if payload["method"] == "eth_getLogs":
            params = payload["params"][0]
            start, end = (int(params[k], 16) for k in ("fromBlock", "toBlock"))
            ranges.append((start, end))
            if "primary.invalid" in req.url:
                response.status_code = 429
            elif end - start + 1 > 10 or fallback_status not in (400, 413, 422):
                response.status_code = fallback_status
            else:
                accepted.extend(
                    (address.lower(), start, end) for address in params["address"]
                )
        response._content = (
            b"private-body"
            if response.status_code != 200
            else transport.post(req.url, req.body)
        )
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    providers = [
        config.ThrottledProvider(
            f"http://{name}.invalid", exception_retry_configuration=None
        )
        for name in ("primary", "fallback")
    ]
    w3 = Web3(config.FailoverProvider(providers))
    snapshots = layout.Snapshots(w3, layout.markets(w3), tmp_path / "cache.json")
    if fallback_status in (400, 413, 422):
        value = snapshots.refresh()
        assert value["block"] == transport.head
        for address in transport.profiles:
            assert [
                block
                for addr, start, end in accepted
                if addr == address
                for block in range(start, end + 1)
            ] == list(range(transport.seed["block"] + 1, transport.head + 1))
    else:
        with pytest.raises(requests.HTTPError) as failure:
            snapshots.refresh()
        message = runtime.error_summary(failure.value)
        assert f"HTTP {fallback_status}" in message and "private" not in message
        assert len(set(ranges)) == 1 and not snapshots.path.exists()
    assert "private" not in caplog.text and not transport.bitmap


def test_zero_argument_start_mode_changes_and_reconnect(
    tmp_path, monkeypatch, offline_http
):
    events = tmp_path / "events"
    controls = tmp_path / "control.json"
    # Old Live and Start requests must neither acquire a signer nor resume entries.
    cfg = replace(
        strategy.DEFAULT,
        event_mode="live",
        event_mode_request=10,
        event_start_request=9,
        event_paused=False,
    )
    control.save(controls, cfg)
    settings = replace(
        event_service.EventSettings(),
        mode="simulate",
        live_enabled=True,
        bot_address="0x" + "11" * 20,
    )
    monkeypatch.setattr(event_service.EventSettings, "from_env", lambda: settings)
    monkeypatch.setattr(event_service, "DIRECTORY", events)
    monkeypatch.setattr(event_service, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        event_service, "required", lambda name: "http://offline.invalid"
    )
    monkeypatch.setattr(event_service, "endpoint", lambda: "unused")
    monkeypatch.setattr(event_modes, "required", lambda name: bytes([1]) * 32)
    monkeypatch.setattr(config, "fallback_urls", list)
    monkeypatch.setattr(config, "rpc_rate", lambda: None)

    class Executor:
        def __init__(self, w3, pools, settings, store, account):
            self.w3, self.settings, self.store = w3, settings, store
            assert (account is not None) == (settings.mode == "live")
            assert w3.middleware_onion["validation"] is EventValidationMiddleware
            assert (
                Web3().middleware_onion["validation"] is not EventValidationMiddleware
            )

        def verify_recent(self):
            pass

        def check_live_limits(self):
            pass

    monkeypatch.setattr(event_modes, "Executor", Executor)

    def run(svc, stop, seconds):
        assert (
            seconds is None
            and svc.settings.mode == "simulate"
            and svc.controls.event_paused
        )
        svc.feed.ready.set()
        svc.maintain()
        assert svc.engine.snapshot and offline_http.bitmap == 0
        session = svc.metrics.started

        def click(**change):
            control.save(controls, replace(control.load(controls), **change))
            svc.maintain()

        click(event_start_request=10, event_paused=False)
        assert not svc.controls.event_paused
        click(
            event_mode_request=11,
            event_mode="live",
            event_paused=False,
            event_start_request=11,
        )
        assert svc.settings.mode == "live" and svc.controls.event_paused
        with (
            pytest.raises(event_modes.operator_lock.SendBlocked),
            event_modes.operator_lock.sender(tmp_path, "manual"),
        ):
            pytest.fail("lost sender exclusion")
        click(event_start_request=12)
        assert not svc.controls.event_paused
        click(event_paused=True)
        svc.feed.gap.set()
        svc.feed.generation += 1
        svc.maintain()
        assert svc.controls.event_paused and svc.generation == svc.feed.generation
        click(event_mode="simulate", event_mode_request=12)
        assert svc.settings.mode == "simulate" and svc.controls.event_paused
        with event_modes.operator_lock.sender(tmp_path, "manual"):
            pass
        svc.publish()
        status = json.loads((events / "state.json").read_text())
        assert status["rpc"]["started"] == session
        assert status["rpc"]["total"] == offline_http.items
        assert offline_http.bitmap == 0

    monkeypatch.setattr(event_service.Service, "run", run)
    monkeypatch.setattr(event_service.signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        event_service.time, "time", lambda: offline_http.seed["timestamp"] + 13
    )
    monkeypatch.setattr(sys, "argv", ["main.py"])
    runpy.run_path(str(config.ROOT / "main.py"), run_name="__main__")
