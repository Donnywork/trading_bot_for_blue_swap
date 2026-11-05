import http.client
import json
import threading
import urllib.parse
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
    assert set(guard) == {
        "steps",
        "sequence",
        "alternating",
        "pairs",
        "flip_pct",
        "drift_pct",
        "reason",
        "kind",
    }
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


# ---------------------------------------------------------------- chart, trades and comparison views


def get_json(server, path):
    response, data = request(server, "GET", path)
    return response.status, json.loads(data)


def test_downsample_keeps_the_highs_and_lows_and_the_ends():
    points = [
        (i, 100.0 + (50 if i == 500 else 0) - (30 if i == 777 else 0))
        for i in range(2000)
    ]
    thin = dashboard.downsample(points, 100)
    assert len(thin) <= 110
    assert thin[0] == points[0] and thin[-1] == points[-1]
    assert (500, 150.0) in thin and (777, 70.0) in thin  # the jumps survive
    assert [p[0] for p in thin] == sorted(p[0] for p in thin)
    assert dashboard.downsample(points[:50], 100) == points[:50]


def test_prices_view_cuts_a_window_back_from_now(app):
    write_prices(app, [1000.0 + i for i in range(100)], spacing=30, age=0)
    view = app.prices_view({"range": ["600"]})
    assert view["samples"] == 21 and view["points"][-1] == (NOW, 1099.0)
    assert app.prices_view({"range": ["all"]})["samples"] == 100


def test_prices_view_can_zoom_to_an_exact_window(app):
    write_prices(app, [1000.0 + i for i in range(100)], spacing=30, age=0)
    lo, hi = NOW - 900, NOW - 300
    view = app.prices_view({"from": [str(lo)], "to": [str(hi)]})
    assert view["points"][0][0] >= lo and view["points"][-1][0] <= hi
    assert view["samples"] == 21


def test_prices_view_thins_a_long_window_and_lists_the_trades_inside_it(app):
    write_prices(app, [1000.0 + (i % 7) for i in range(2000)], spacing=30, age=0)
    data = ledger.open_position(
        ledger.empty(), 1000.0, Decimal("0.01"), Decimal(10), NOW - 600
    )
    data = ledger.close_position(
        data, 1010.0, Decimal("10.1"), "take_profit", NOW - 300
    )
    data = ledger.open_position(data, 1000.0, Decimal("0.01"), Decimal(10), NOW - 10**6)
    data = ledger.close_position(
        data, 1010.0, Decimal("10.1"), "take_profit", NOW - 10**6 + 60
    )
    ledger.save(app.ledger_file, data)
    view = app.prices_view({"range": ["3600"], "max": ["100"]})
    assert len(view["points"]) <= 110 and view["samples"] == 121
    assert [t["closed"] for t in view["trades"]] == [
        NOW - 300
    ]  # the old one is outside


@pytest.mark.parametrize(
    "query",
    [
        {"max": ["10"]},
        {"max": ["x"]},
        {"range": ["5"]},
        {"from": ["100"], "to": ["50"]},
        {"from": ["nan"]},
    ],
)
def test_prices_view_refuses_a_bad_query(app, query):
    with pytest.raises(dashboard.QueryError):
        app.prices_view(query)


def closed_trades(app, reasons, base=100):
    data = ledger.empty()
    for i, reason in enumerate(reasons):
        data = ledger.open_position(
            data, 1000.0, Decimal("0.01"), Decimal(10), base + i
        )
        received = "10.5" if reason == "take_profit" else "9.5"
        data = ledger.close_position(
            data, 1000.0, Decimal(received), reason, base + 100 + i
        )
    ledger.save(app.ledger_file, data)


def test_trades_view_pages_newest_first_and_adds_up_the_filtered_rows(app):
    closed_trades(app, ["take_profit", "stop_loss", "take_profit", "time_stop"])
    everything = app.trades_view({})
    assert everything["total"] == 4 and Decimal(everything["profit"]) == 0
    assert everything["items"][0]["reason"] == "time_stop"
    page = app.trades_view({"limit": ["2"], "offset": ["2"]})
    assert [t["reason"] for t in page["items"]] == ["stop_loss", "take_profit"]
    wins = app.trades_view({"reason": ["take_profit"]})
    assert wins["total"] == 2 and Decimal(wins["profit"]) == Decimal("1.0")
    assert wins["profit_pct"] == pytest.approx(5.0)


def test_trades_view_refuses_an_unknown_reason_and_a_huge_page(app):
    with pytest.raises(dashboard.QueryError):
        app.trades_view({"reason": ["luck"]})
    with pytest.raises(dashboard.QueryError):
        app.trades_view({"limit": ["100000"]})


def test_compare_view_replays_every_strategy_and_shows_what_really_happened(app):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    closed_trades(app, ["take_profit"], base=NOW - 100)
    view = app.compare_view({"hours": ["all"]})
    assert [r["strategy"] for r in view["rows"]] == list(strategy.STRATEGIES)
    assert view["active"] == "mean_reversion"
    assert view["actual"]["trades"] == 1
    json.dumps(view)


def test_compare_view_is_cached_until_the_prices_or_settings_change(app, monkeypatch):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    calls = []
    real = dashboard.compare.compare
    monkeypatch.setattr(
        dashboard.compare, "compare", lambda *a: calls.append(1) or real(*a)
    )
    first = app.compare_view({})
    assert app.compare_view({}) is first and len(calls) == 1
    control.save(app.control_file, strategy.parse_config({"take_profit_pct": 6}))
    app.compare_view({})
    assert len(calls) == 2
    history.append(app.history_file, NOW + 1, 1000.0)
    app.compare_view({})
    assert len(calls) == 3


def test_the_replay_charges_a_big_trade_its_real_cost_when_the_dashboard_has_the_depth(
    app,
):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    app.size = Decimal(20000)
    fixed = app.compare_view({"hours": ["all"]})
    # the bot has scanned nothing and not sized a buy yet: the layout that ships with the code
    app.depth_file = app.tmp / "pool_profile.json"
    exact = app.compare_view({"hours": ["all"]})

    def total(view):
        return sum(r["profit"] + r["open"] for r in view["rows"])

    assert sum(r["trades"] + r["holding"] for r in fixed["rows"]) > 0
    assert total(exact) < total(fixed)


def test_a_thinner_pool_reported_by_the_bot_makes_the_replay_cost_more(
    app, monkeypatch
):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    app.size = Decimal(20000)
    app.depth_file = app.tmp / "pool_profile.json"
    calls = []
    real = dashboard.compare.compare
    monkeypatch.setattr(
        dashboard.compare, "compare", lambda *a: calls.append(1) or real(*a)
    )

    def total(view):
        return sum(r["profit"] + r["open"] for r in view["rows"])

    as_saved = app.compare_view({"hours": ["all"]})
    app.compare_view({"hours": ["all"]})
    assert len(calls) == 1  # cached
    # what the bot writes to the state file when it finds the pool half as deep
    state.save(app.state_file, NOW - 10, {**CHAIN, "depth_scale": 0.5})
    thin = app.compare_view({"hours": ["all"]})
    assert len(calls) == 2 and total(thin) < total(as_saved)


# ---------------------------------------------------------------- the buttons


def post_command(server, command, **headers):
    base = {"Content-Type": "application/json"}
    body = json.dumps({"command": command})
    return request(server, "POST", "/api/command", body, base | headers)


def test_pause_and_resume_change_only_the_settings_file(app, server):
    state_before = app.state_file.read_text()
    res, data = post_command(server, "pause")
    assert res.status == 200 and json.loads(data)["control"]["paused"] is True
    assert control.load(app.control_file).paused is True
    res, data = post_command(server, "resume")
    assert res.status == 200 and json.loads(data)["control"]["paused"] is False
    assert control.load(app.control_file).paused is False
    # nothing but logs/control.json was written
    assert sorted(p.name for p in app.tmp.iterdir()) == ["control.json", "state.json"]
    assert app.state_file.read_text() == state_before


def test_a_pause_leaves_the_strategy_settings_alone(app, server):
    control.save(app.control_file, strategy.parse_config({"take_profit_pct": 7}))
    post_command(server, "pause")
    saved = control.load(app.control_file)
    assert saved.paused and saved.take_profit_pct == 7


def test_every_scan_request_is_a_higher_number_than_the_last(app, server):
    post_command(server, "scan")
    first = control.load(app.control_file).scan_request
    post_command(server, "scan")  # the clock has not moved: still higher
    second = control.load(app.control_file).scan_request
    assert first >= NOW and second == first + 1


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "withdraw"},
        {"command": "pause", "dry_run": False},
        {"command": ["pause"]},
        {"command": None},
        {},
        [],
        "pause",
    ],
)
def test_only_the_three_commands_are_accepted(app, server, payload):
    res, data = request(
        server,
        "POST",
        "/api/command",
        json.dumps(payload),
        {"Content-Type": "application/json"},
    )
    assert res.status == 400 and "error" in json.loads(data)
    assert not app.control_file.exists()


def test_a_command_does_not_overwrite_a_damaged_settings_file(app, server):
    app.control_file.write_text("{not json")
    res, data = post_command(server, "pause")
    assert res.status == 400 and "control.json" in json.loads(data)["error"]
    assert app.control_file.read_text() == "{not json"


def test_the_command_route_has_the_same_guards_as_the_settings_route(app, server):
    assert post_command(server, "pause", Origin="http://evil.example")[0].status == 403
    assert post_command(server, "pause", Host="evil.example")[0].status == 403
    res, _ = request(
        server,
        "POST",
        "/api/command",
        '{"command": "pause"}',
        {"Content-Type": "text/plain"},
    )
    assert res.status == 415
    res, _ = request(
        server,
        "POST",
        "/api/command",
        json.dumps({"command": "x" * 20_000}),
        {"Content-Type": "application/json"},
    )
    assert res.status == 413
    assert request(server, "GET", "/api/command")[0].status == 404
    assert not app.control_file.exists()


def test_saving_the_settings_form_cannot_undo_a_pause_or_a_scan_request(app, server):
    post_command(server, "pause")
    post_command(server, "scan")
    asked = control.load(app.control_file).scan_request
    # a form loaded before the clicks, and even one that tries to say otherwise
    form = {**strategy.to_dict(strategy.DEFAULT), "paused": False, "scan_request": 0}
    res, _ = post_config(server, form)
    assert res.status == 200
    saved = control.load(app.control_file)
    assert saved.paused is True and saved.scan_request == asked


def test_a_pause_alone_is_not_a_change_in_the_settings_preview(app):
    control.save(app.control_file, strategy.parse_config({"paused": True}))
    write_prices(app, pattern_prices(), spacing=30, age=0)
    assert app.preview_view(draft_query())["changed"] is False


def test_the_snapshot_tells_the_page_what_the_buttons_show(app):
    control.save(app.control_file, strategy.parse_config({"paused": True}))
    view = app.snapshot()["control"]
    assert view["paused"] is True and view["scan_request"] == 0
    assert view["scan"] is None and view["pool"] is None  # no depth file given
    answer = {"done": 5, "block": 123, "at": NOW - 30.0, "error": None}
    state.save(app.state_file, NOW - 10, {**CHAIN, "scan": answer})
    app.depth_file = app.tmp / "pool_profile.json"
    view = app.snapshot()["control"]
    assert view["scan"] == answer
    assert view["pool"]["scanned"] is False  # the layout that ships with the code
    scanned = {**dashboard.v3pool.load_profile(), "block": 123, "request": 5}
    dashboard.v3pool.save_profile(app.depth_file, scanned)
    view = app.snapshot()["control"]
    assert view["pool"]["scanned"] is True and view["pool"]["block"] == 123
    json.dumps(app.snapshot())


def test_the_page_has_the_buttons(app):
    page = dashboard.PAGE.read_text()
    for needle in ('id="controls-bar"', "/api/command", '"pause"', '"scan"'):
        assert needle in page


def test_the_views_work_over_http_and_a_bad_query_is_a_400(server):
    assert get_json(server, "/api/prices?range=3600")[0] == 200
    assert get_json(server, "/api/trades?reason=all&limit=5")[0] == 200
    assert get_json(server, "/api/compare?hours=6")[0] == 200
    status, body = get_json(server, "/api/prices?max=1")
    assert status == 400 and "max" in body["error"]


def test_the_views_keep_the_host_check(server):
    response, _ = request(
        server, "GET", "/api/prices", headers={"Host": "evil.example"}
    )
    assert response.status == 403


def test_the_time_stop_setting_is_saved_and_shown(app):
    app.update_config({"time_stop_on": True, "time_stop_min": 20})
    config = app.snapshot()["config"]
    assert (config["time_stop_on"], config["time_stop_min"]) == (True, 20.0)
    assert app.snapshot()["schema"]["time_stop_min"]["low"] == 1.0


# ---------------------------------------------------------------- what-if preview


def draft_query(**changes):
    # the saved defaults with some settings changed, as the form would send them
    raw = strategy.to_dict(strategy.DEFAULT) | changes
    return {"cfg": [json.dumps(raw)], "hours": ["all"]}


def test_preview_with_unchanged_settings_shows_no_change(app):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    view = app.preview_view(draft_query())
    assert view["changed"] is False
    assert [r["strategy"] for r in view["rows"]] == list(strategy.STRATEGIES)
    assert all(r["change"] == 0 for r in view["rows"])
    json.dumps(view)


def test_preview_estimates_what_an_edit_would_change_for_every_strategy(app):
    write_prices(app, pattern_prices() * 4, spacing=30, age=0)
    wide = app.preview_view(draft_query(take_profit_pct=100.0))  # never takes profit
    assert wide["changed"] is True
    row = next(r for r in wide["rows"] if r["strategy"] == "mean_reversion")
    was = row["saved"]["profit"] + row["saved"]["open"]
    now = row["draft"]["profit"] + row["draft"]["open"]
    assert row["change"] == pytest.approx(now - was)
    # a take profit of 100% is never reached, so every trade the saved settings made is gone
    assert row["saved"]["trades"] > 0 and row["draft"]["trades"] == 0
    assert row["change"] != 0
    assert any(
        "above 9 in 10" in w["text"] or "rarely be reached" in w["text"]
        for w in row["draft"]["warnings"]
    ) or row["draft"]["warnings"][0]["text"].startswith("After the pool fee")


def test_preview_compares_against_the_saved_settings_not_the_defaults(app):
    write_prices(app, pattern_prices() * 4, spacing=30, age=0)
    control.save(app.control_file, strategy.parse_config({"take_profit_pct": 100}))
    saved = json.dumps(strategy.to_dict(app.saved_config()))
    assert app.preview_view({"cfg": [saved], "hours": ["all"]})["changed"] is False


def test_preview_never_saves_anything(app):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    app.preview_view(draft_query(take_profit_pct=9.0))
    assert not app.control_file.exists()


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"cfg": ["not json"]},
        {"cfg": ["[]"]},
        {"cfg": ['{"take_profit_pct": 0.1}']},
        {"cfg": ['{"stop_loss_pct": "4"}']},
        {"cfg": ['{"nonsense": 1}']},
        {"cfg": ['{"time_stop_on": "yes"}']},
        {"cfg": ["x" * 20_000]},
    ],
)
def test_preview_refuses_settings_a_save_would_refuse(app, query):
    with pytest.raises(dashboard.QueryError):
        app.preview_view(query)


def test_preview_works_over_http_and_a_bad_draft_is_a_400(server):
    cfg = json.dumps(strategy.to_dict(strategy.DEFAULT))
    status, body = get_json(
        server, "/api/preview?hours=6&cfg=" + urllib.parse.quote(cfg)
    )
    assert status == 200 and body["rows"] == []  # no prices recorded yet
    status, body = get_json(server, "/api/preview?cfg=%7B%22take_profit_pct%22%3A0%7D")
    assert status == 400 and "take profit" in body["error"]


def test_preview_is_cached_for_the_same_draft(app, monkeypatch):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    calls = []
    real = dashboard.compare.compare
    monkeypatch.setattr(
        dashboard.compare, "compare", lambda *a: calls.append(1) or real(*a)
    )
    app.preview_view(draft_query(take_profit_pct=6.0))
    first = len(calls)
    app.preview_view(draft_query(take_profit_pct=6.0))
    assert len(calls) == first
    app.preview_view(draft_query(take_profit_pct=7.0))
    assert (
        len(calls) == first + 1
    )  # only the new draft; the saved side was already known


# ---------------------------------------------------------------- what the review found


def test_downsample_keeps_a_spike_in_the_first_and_the_last_bucket():
    points = [(i, 100.0) for i in range(2000)]
    points[3] = (3, 40.0)  # a low spike right at the start
    points[1995] = (1995, 500.0)  # and a high one right at the end
    thin = dashboard.downsample(points, 100)
    assert len(thin) <= 100
    assert thin[0] == points[0] and thin[-1] == points[-1]
    assert (3, 40.0) in thin and (1995, 500.0) in thin
    assert thin == sorted(thin)


def test_the_cache_keeps_to_its_limit_and_forgets_the_oldest(app):
    for n in range(dashboard.CACHE_ENTRIES + 5):
        app.remembered(("k", n), lambda n=n: n)
    assert len(app.cache) == dashboard.CACHE_ENTRIES
    assert ("k", 0) not in app.cache and ("k", dashboard.CACHE_ENTRIES + 4) in app.cache


def test_the_cache_survives_requests_from_many_threads(app):
    errors = []

    def hammer(offset):
        try:
            for n in range(200):
                assert (
                    app.remembered(("t", (n + offset) % 60), lambda n=n: n) is not None
                )
        except Exception as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(i * 7,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(app.cache) <= dashboard.CACHE_ENTRIES


def test_the_same_answer_is_only_worked_out_once_even_when_asked_together(app):
    made = []

    def slow():
        made.append(1)
        threading.Event().wait(0.05)
        return "ok"

    threads = [
        threading.Thread(target=lambda: app.remembered("same", slow)) for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1


def test_the_comparison_notices_a_trade_that_closed_without_a_new_price(app):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    assert app.compare_view({"hours": ["all"]})["actual"]["trades"] == 0
    closed_trades(app, ["take_profit"], base=NOW - 100)
    assert app.compare_view({"hours": ["all"]})["actual"]["trades"] == 1


def test_the_comparison_window_moves_with_the_clock_even_with_no_new_price(app):
    write_prices(app, pattern_prices(), spacing=30, age=0)
    first = app.compare_view({"hours": ["1"]})["from"]
    app.clock = lambda: NOW + 1800
    assert app.compare_view({"hours": ["1"]})["from"] == first + 1800


def test_prices_out_of_order_in_the_file_still_give_the_right_window(app):
    write_prices(app, [1000.0 + i for i in range(100)], spacing=30, age=0)
    history.append(app.history_file, NOW - 9000, 5.0)  # a clock that stepped back
    view = app.prices_view({"range": ["600"]})
    assert view["samples"] == 21 and all(p[0] >= NOW - 600 for p in view["points"])


@pytest.mark.parametrize("path", sorted(dashboard.Handler.VIEWS) + ["/api/status"])
def test_every_api_get_refuses_a_bad_host_and_a_cross_site_page(server, path):
    assert (
        request(server, "GET", path, headers={"Host": "evil.example"})[0].status == 403
    )
    cross = request(server, "GET", path, headers={"Sec-Fetch-Site": "cross-site"})[0]
    assert cross.status == 403
    foreign = request(server, "GET", path, headers={"Origin": "https://evil.example"})[
        0
    ]
    assert foreign.status == 403
    # a page of the dashboard itself, and a command-line client, are fine
    assert (
        request(server, "GET", path, headers={"Sec-Fetch-Site": "same-origin"})[
            0
        ].status
        != 403
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/trades?limit=0",
        "/api/trades?reason=luck",
        "/api/compare?hours=0",
        "/api/compare?hours=x",
        "/api/compare?hours=999999",
        "/api/preview",
        "/api/prices?range=inf",
    ],
)
def test_a_bad_query_is_a_400_with_the_reason(server, path):
    status, body = get_json(server, path)
    assert status == 400 and body["error"]


def test_an_unknown_api_path_is_a_404_and_a_post_to_a_view_is_refused(server):
    assert get_json(server, "/api/nothing")[0] == 404
    response, _ = request(
        server, "POST", "/api/compare", "{}", {"Content-Type": "application/json"}
    )
    assert response.status == 404
