"""Exercise real Web3 encoding/middleware against a deterministic offline transport."""

import json
import runpy
import sys
import time
from dataclasses import replace

import pytest
import requests
from eth_abi import decode, encode
from eth_utils import function_abi_to_4byte_selector
from web3 import Web3

import config
import control
import event_market as market
import event_modes
import event_service
import event_snapshot as layout
import rpc_metrics as rpc
import runtime
import strategy


class PoolTransport:
    def __init__(self):
        self.seed = json.loads(layout.RESEARCH_SNAPSHOT.read_text())
        self.profiles = {p["pool_address"].lower(): p for p in self.seed["profiles"]}
        self.head = self.seed["block"] + 1
        self.http = self.items = self.bitmap = 0
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


def test_pool_request_cost(tmp_path, monkeypatch, offline_http):
    transport = offline_http
    sleeps = []
    monkeypatch.setattr(config.time, "sleep", sleeps.append)
    metrics = rpc.RpcMetrics()
    w3 = Web3(config.ThrottledProvider("http://offline.invalid", metrics=metrics))
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
    ) == (56, 17, 1, 778, 65)
    assert not sleeps
    print("Full scan, fake HTTP transport (seconds):", time.perf_counter() - start)


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
                (address,) = params["address"]
                accepted.append((address.lower(), start, end))
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
