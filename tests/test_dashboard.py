import http.client
import json
import threading
from decimal import Decimal

import pytest

import control
import dashboard
import history
import ledger
import state
import strategy

NOW = 1_700_000_000
CHAIN = {
    "base": "fETH",
    "quote": "f564",
    "price": 1050.0,
    "vault_base": Decimal("0.5"),
    "vault_quote": Decimal("12.5"),
    "operator_eth": Decimal("0.05"),
}


@pytest.fixture
def app(tmp_path):
    app = dashboard.Dashboard(
        dry_run=True,
        state_file=tmp_path / "state.json",
        control_file=tmp_path / "control.json",
        history_file=tmp_path / "prices.csv",
        ledger_file=tmp_path / "ledger.json",
        clock=lambda: NOW,
    )
    app.tmp = tmp_path
    state.save(app.state_file, NOW - 10, CHAIN)  # what the bot wrote on its last tick
    return app


@pytest.fixture
def server(app):
    srv = dashboard.serve(app, 0)  # port 0: any free port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def post_config(server, payload, **headers):
    base = {"Content-Type": "application/json"}
    return request(server, "POST", "/api/config", json.dumps(payload), base | headers)


# ---------------------------------------------------------------- snapshot


def test_snapshot_with_nothing_recorded_yet(app):
    snap = app.snapshot()
    assert snap["mode"] == "dry_run"
    assert snap["position"] is None and snap["trades"] == [] and snap["prices"] == []
    assert snap["chain"]["price"] == 1050.0
    assert snap["chain"]["vault_base"] == "0.5"  # Decimals travel as text
    assert snap["config"]["strategy"] == "mean_reversion"
    assert set(snap["schema"]["strategies"]) == set(strategy.STRATEGIES)
    json.dumps(snap)  # everything is serialisable


def test_snapshot_shows_the_position_targets_and_unrealized_profit(app):
    data = ledger.open_position(
        ledger.empty(), 1000.0, Decimal("0.00001"), Decimal("0.01"), NOW - 600
    )
    ledger.save(app.ledger_file, data)
    control.save(app.control_file, strategy.parse_config({"take_profit_pct": 8}))
    pos = app.snapshot()["position"]
    assert pos["take_profit_price"] == pytest.approx(1080.0)
    assert pos["stop_loss_price"] == pytest.approx(960.0)
    assert pos["change_pct"] == pytest.approx(5.0)
    assert Decimal(pos["unrealized"]) == Decimal("0.0005")


def test_snapshot_lists_trades_newest_first_with_their_profit(app):
    data = ledger.empty()
    for received in ("0.0105", "0.0097"):
        data = ledger.open_position(
            data, 1000.0, Decimal("0.00001"), Decimal("0.01"), 1
        )
        data = ledger.close_position(
            data, 1000.0, Decimal(received), "take_profit", NOW
        )
    ledger.save(app.ledger_file, data)
    trades = app.snapshot()["trades"]
    assert [t["profit"] for t in trades] == ["-0.0003", "0.0005"]


def test_snapshot_includes_the_price_history(app):
    history.append(app.history_file, NOW - 30, 1049.5)
    history.append(app.history_file, NOW, 1050.0)
    assert app.snapshot()["prices"] == [(NOW - 30, 1049.5), (NOW, 1050.0)]


def test_the_chain_figures_come_from_the_bots_state_file(app):
    chain = app.snapshot()["chain"]
    assert chain["base"] == "fETH" and chain["quote"] == "f564"
    assert chain["updated"] == NOW - 10
    assert not app.snapshot()["problems"]


def test_the_dashboard_makes_no_rpc_calls(app, monkeypatch):
    # nothing in the dashboard may open a connection: a tab left open must cost no quota
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the dashboard opened a connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for _ in range(3):
        app.snapshot()
    assert not hasattr(dashboard, "chain_reader")


def test_no_state_file_yet_is_reported_not_fatal(app):
    app.state_file.unlink()
    snap = app.snapshot()
    assert snap["chain"] is None
    assert any("no price data yet" in p for p in snap["problems"])


def test_an_old_state_file_still_shows_but_says_how_old(app):
    app.clock = lambda: NOW + 3600
    snap = app.snapshot()
    assert snap["chain"]["price"] == 1050.0
    assert any("price and balances are old" in p for p in snap["problems"])


def test_a_broken_state_file_is_reported_not_fatal(app):
    app.state_file.write_text('{"price": "nan"}')
    snap = app.snapshot()
    assert snap["chain"] is None
    assert any("state.json" in p for p in snap["problems"])
    app.state_file.write_text("{nope")
    assert app.snapshot()["chain"] is None


def test_a_broken_ledger_or_settings_file_is_reported_not_fatal(app):
    app.ledger_file.write_text("{nope")
    app.control_file.write_text('{"strategy": "yolo"}')
    snap = app.snapshot()
    assert snap["position"] is None
    assert snap["config"]["strategy"] == "mean_reversion"
    assert len(snap["problems"]) == 2


# ---------------------------------------------------------------- HTTP


def test_serves_the_page_and_the_status(server):
    r, body = request(server, "GET", "/")
    assert r.status == 200 and b"Trading bot" in body
    assert r.getheader("Content-Type").startswith("text/html")
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    r, body = request(server, "GET", "/api/status")
    assert r.status == 200 and json.loads(body)["mode"] == "dry_run"


def test_unknown_paths_are_404(server):
    assert request(server, "GET", "/etc/passwd")[0].status == 404
    assert request(server, "GET", "/../main.py")[0].status == 404
    assert (
        request(
            server, "POST", "/api/other", "{}", {"Content-Type": "application/json"}
        )[0].status
        == 404
    )


def test_it_only_listens_on_loopback(server):
    assert server.server_address[0] == "127.0.0.1"


def test_saving_settings_writes_the_file_the_bot_reads(server, app):
    r, body = post_config(
        server,
        {"strategy": "rsi", "take_profit_pct": 7, "params": {"rsi": {"period": 10}}},
    )
    assert r.status == 200
    saved = control.load(app.control_file)
    assert (saved.strategy, saved.take_profit_pct, saved.values()["period"]) == (
        "rsi",
        7.0,
        10,
    )
    assert json.loads(body)["config"]["strategy"] == "rsi"


@pytest.mark.parametrize(
    "payload",
    [
        {"strategy": "yolo"},
        {"take_profit_pct": 0.1},
        {"stop_loss_pct": -1},
        {"params": {"ma_cross": {"fast": 50, "slow": 10}}},
        [],
    ],
)
def test_bad_settings_are_refused_with_a_reason_and_nothing_is_saved(
    server, app, payload
):
    r, body = post_config(server, payload)
    assert r.status == 400 and json.loads(body)["error"]
    assert not app.control_file.exists()


def test_bad_json_is_a_400(server):
    r, _ = request(
        server, "POST", "/api/config", "{nope", {"Content-Type": "application/json"}
    )
    assert r.status == 400


def test_a_dns_rebinding_host_is_refused(server):
    # a hostile page points its own name at 127.0.0.1; the Host header gives it away
    for method, path in (("GET", "/"), ("GET", "/api/status")):
        r, _ = request(server, method, path, headers={"Host": "evil.example.com"})
        assert r.status == 403
    r, _ = post_config(server, {"strategy": "rsi"}, Host="evil.example.com")
    assert r.status == 403


def test_a_cross_site_post_is_refused(server, app):
    r, _ = post_config(server, {"strategy": "rsi"}, Origin="https://evil.example.com")
    assert r.status == 403
    assert not app.control_file.exists()


def test_a_form_style_post_without_json_is_refused(server, app):
    r, _ = request(
        server,
        "POST",
        "/api/config",
        "strategy=rsi",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status == 415
    assert not app.control_file.exists()


def test_a_local_origin_and_a_remapped_tunnel_port_are_fine(server):
    r, _ = post_config(
        server,
        {"strategy": "rsi"},
        Origin="http://localhost:9000",
        Host="localhost:9000",
    )
    assert r.status == 200


def test_oversized_and_missing_bodies_are_refused(server):
    big = json.dumps({"strategy": "rsi", "pad": "x" * dashboard.MAX_BODY})
    r, _ = request(
        server, "POST", "/api/config", big, {"Content-Type": "application/json"}
    )
    assert r.status == 413
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest("POST", "/api/config")
    conn.putheader("Content-Type", "application/json")
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()


def test_the_page_cannot_change_dry_run_or_trade_size(server, app):
    for key in ("dry_run", "DRY_RUN", "max_trade", "private_key"):
        r, _ = post_config(server, {key: "x"})
        assert r.status == 400
    assert not app.control_file.exists()


def test_cli_rejects_bad_arguments(monkeypatch):
    for argv in (
        ["--port"],
        ["--port", "x"],
        ["--port", "0"],
        ["--port", "99999"],
        ["--host", "0.0.0.0"],
    ):
        monkeypatch.setattr("sys.argv", ["dashboard.py", *argv])
        with pytest.raises(SystemExit, match="usage"):
            dashboard.cli()


# ---------------------------------------------------------------- hardening


def test_the_csp_allows_only_the_pages_own_script_and_style_by_hash(server):
    import base64
    import hashlib
    import re

    r, body = request(server, "GET", "/")
    csp = r.getheader("Content-Security-Policy")
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    html = body.decode()
    for tag in ("script", "style"):
        inner = re.search(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL).group(1)
        digest = base64.b64encode(hashlib.sha256(inner.encode()).digest()).decode()
        assert f"{tag}-src 'sha256-{digest}'" in csp


def test_the_page_has_no_inline_attributes_the_csp_would_block():
    import re

    html = dashboard.PAGE.read_text()
    assert not re.search(r"\sstyle=", html), (
        "inline style attributes need unsafe-inline"
    )
    assert not re.search(r"\son[a-z]+=", html), (
        "inline event handlers need unsafe-inline"
    )
    assert html.count("<script>") == 1 and html.count("<style>") == 1


def test_every_response_carries_the_security_headers(server):
    responses = [
        request(server, "GET", "/")[0],
        request(server, "GET", "/api/status")[0],
        request(server, "GET", "/nope")[0],
        request(server, "GET", "/", headers={"Host": "evil.example.com"})[0],
        request(server, "PUT", "/api/config", "{}")[0],  # the library's own error path
        request(server, "OPTIONS", "/api/config")[0],
    ]
    for r in responses:
        assert r.getheader("Content-Security-Policy"), r.status
        assert r.getheader("X-Content-Type-Options") == "nosniff", r.status
        assert r.getheader("X-Frame-Options") == "DENY", r.status
        assert r.getheader("Referrer-Policy") == "no-referrer", r.status
        assert r.getheader("Cache-Control") == "no-store", r.status


def test_library_error_pages_are_json_without_a_server_banner(server):
    r, body = request(server, "PUT", "/api/config", "{}")
    assert r.status == 501
    assert r.getheader("Content-Type") == "application/json"
    assert "error" in json.loads(body)
    assert "Python" not in (r.getheader("Server") or "")


def test_a_malformed_origin_is_refused_not_a_stack_dump(server, app):
    r, _ = post_config(server, {"strategy": "rsi"}, Origin="http://[evil")
    assert r.status == 403
    assert not app.control_file.exists()


@pytest.mark.parametrize(
    "content_type", ["application/jsonp", "application/json-evil", "text/json"]
)
def test_the_content_type_must_be_exactly_json(server, app, content_type):
    r, _ = request(server, "POST", "/api/config", "{}", {"Content-Type": content_type})
    assert r.status == 415


def test_json_with_a_charset_or_odd_capitals_is_accepted(server):
    for content_type in ("application/json; charset=utf-8", "APPLICATION/JSON"):
        r, _ = request(
            server,
            "POST",
            "/api/config",
            '{"strategy": "rsi"}',
            {"Content-Type": content_type},
        )
        assert r.status == 200


@pytest.mark.parametrize(
    "payload", [{"strategy": []}, {"strategy": {}}, {"take_profit_pct": 10**400}]
)
def test_odd_json_values_are_a_400_not_a_500(server, payload):
    r, body = post_config(server, payload)
    assert r.status == 400 and json.loads(body)["error"]


def test_a_stalled_client_cannot_hold_a_thread_forever():
    assert 0 < dashboard.Handler.timeout <= 30


def test_concurrent_saves_never_corrupt_the_settings_file(app):
    errors = []

    def save(n):
        try:
            for _ in range(30):
                app.update_config(
                    {"take_profit_pct": 5 + n, "params": {"rsi": {"period": 10 + n}}}
                )
                control.load(app.control_file)  # must always parse
        except Exception as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=save, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_the_dashboard_and_status_read_the_ledger_that_matches_dry_run(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ledger, "LOG_DIR", tmp_path)
    dry = dashboard.Dashboard(True, state_file=tmp_path / "state.json")
    live = dashboard.Dashboard(False, state_file=tmp_path / "state.json")
    assert dry.ledger_file.name == "ledger_dryrun.json"
    assert live.ledger_file.name == "ledger.json"
    assert dry.snapshot()["mode"] == "dry_run" and live.snapshot()["mode"] == "live"


def test_snapshot_reports_the_pattern_guard(app):
    guard = app.snapshot()["guard"]
    assert guard["reason"]  # no price history yet: it fails closed
    assert set(guard) == {"steps", "flip_pct", "drift_pct", "reason", "kind"}
    control.save(
        app.control_file,
        strategy.parse_config({"guard_flip_pct": 0, "guard_drift_pct": 0}),
    )
    assert app.snapshot()["guard"]["reason"] is None


def write_prices(app, prices, spacing=30, age=60):
    # a price history that ends `age` seconds before the dashboard's clock
    start = NOW - age - spacing * (len(prices) - 1)
    for i, price in enumerate(prices):
        history.append(app.history_file, start + spacing * i, price)


def pattern_prices(drops=0):
    prices, price = [], 1050.0
    for d in [1, -1] * 5:
        price *= 1.04 if d > 0 else 1 / 1.04
        prices += [price] * 4
    for _ in range(drops):
        price *= 0.9
        prices += [price] * 4
    return prices


def test_snapshot_guard_is_allowed_after_a_healthy_pattern(app):
    write_prices(app, pattern_prices())
    guard = app.snapshot()["guard"]
    assert guard["reason"] is None and guard["kind"] is None
    assert guard["flip_pct"] == 100 and guard["steps"] == 8
    assert guard["drift_pct"] is None  # 40 samples: too few for the 60-vs-60 comparison
    json.dumps(app.snapshot())  # null reason / numbers all serialise


def test_snapshot_guard_pauses_on_a_slide(app):
    start = pattern_prices()[-1]
    slide = [start * (1 - 0.001 * i) for i in range(120)]  # 0.1% a sample: no big steps
    write_prices(app, pattern_prices() + slide)
    guard = app.snapshot()["guard"]
    assert guard["kind"] == "slide" and guard["drift_pct"] < -5


def test_snapshot_guard_ignores_a_history_the_bot_would_not_restore(app):
    write_prices(
        app, pattern_prices(), age=100_000
    )  # stale: the bot restarts from nothing
    guard = app.snapshot()["guard"]
    assert guard["kind"] == "few_steps" and guard["steps"] == 0


def test_guard_settings_round_trip_over_http(server, app):
    r, _ = post_config(server, {"guard_flip_pct": 55, "guard_drift_pct": 0})
    assert r.status == 200
    saved = control.load(app.control_file)
    assert (saved.guard_flip_pct, saved.guard_drift_pct) == (55, 0)
    assert app.snapshot()["config"]["guard_flip_pct"] == 55
    assert app.snapshot()["schema"]["guard_drift_pct"]["high"] == 20


@pytest.mark.parametrize(
    "payload",
    [
        {"guard_flip_pct": 101},
        {"guard_flip_pct": -1},
        {"guard_drift_pct": 21},
        {"guard_drift_pct": None},
        {"guard_flip_pct": "80"},
        {"guard_flip_pct": float("nan")},
        {"guard": {"flip": 5}},
    ],
)
def test_bad_guard_settings_are_refused_and_nothing_is_saved(server, app, payload):
    r, body = post_config(server, payload)
    assert r.status == 400
    assert "error" in json.loads(body)
    assert not app.control_file.exists()


def test_a_settings_file_from_before_the_guard_loads_with_the_default_limits(app):
    app.control_file.write_text('{"strategy": "rsi", "take_profit_pct": 4}')
    config = app.snapshot()["config"]
    assert config["strategy"] == "rsi"
    assert (config["guard_flip_pct"], config["guard_drift_pct"]) == (80, 5)
