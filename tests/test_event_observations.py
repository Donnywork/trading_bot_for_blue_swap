import copy
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from eth_abi import encode
from web3 import Web3

import control
import event_activity as activity
import event_history
import event_market as market
import event_portfolio as portfolio
import strategy
from dashboard import Dashboard
from event_diagnostics import trading_controls
from event_observer import Observer, balances
from event_settings import EventSettings
from event_store import EventStore

A = 10**18
HASH = "0x" + "ab" * 32
TX = "0x" + "cd" * 32
VAULT = "0x" + "11" * 20
OPERATOR = "0x" + "22" * 20
NOW = 1000


def swap(fee=500, index=1, buy=True, tx=TX, block=7):
    return {
        "address": market.POOLS[fee],
        "transactionHash": tx,
        "blockHash": HASH,
        "transactionIndex": 2,
        "blockNumber": block,
        "logIndex": index,
        "args": {
            "amount0": 400 * A * (1 if buy else -1),
            "amount1": A * (-1 if buy else 1),
            "sqrt" + "PriceX96": 2**95,
            "sender": market.MARKET_BOT,
        },
    }


def runtime(**changes):
    return {
        "updated": NOW,
        "health": "ready",
        "mode": "simulate",
        "bot_address": VAULT,
        "mode_done": 3,
        "start_floor": 4,
        "position": None,
        "controls": {
            "event_paused": True,
            "event_mode_request": 3,
            "event_start_request": 4,
        },
        **changes,
    }


def test_control_acknowledgement_pending_failure_and_stale_pause(tmp_path):
    app = Dashboard(
        True,
        state_file=tmp_path / "state.json",
        control_file=tmp_path / "control.json",
        clock=lambda: NOW,
    )
    state = tmp_path / "events/state.json"
    state.parent.mkdir()
    state.write_text(json.dumps(runtime()))
    cfg = replace(strategy.DEFAULT, event_mode_request=3, event_start_request=4)
    control.save(app.control_file, cfg)
    saved = app.command({"command": "event_live"})
    assert saved["event_paused"]
    view = app.events_view()
    assert view["events"]["mode"] == "simulate"
    assert view["trading_controls"]["mode_pending"]
    with pytest.raises(ValueError, match="acknowledge"):
        app.command({"command": "event_resume"})
    state.write_text(
        json.dumps(runtime(mode_done=NOW, mode_block="Live requires DRY_RUN=false"))
    )
    assert not app.events_view()["trading_controls"]["mode_pending"]
    with pytest.raises(ValueError, match="DRY_RUN"):
        app.command({"command": "event_resume"})
    # Acknowledgement changes mode but leaves entry permission paused.
    state.write_text(json.dumps(runtime(mode="live", mode_done=NOW, start_floor=4)))
    assert app.events_view()["trading_controls"]["trading_label"] == "Start Trading"
    first = app.command({"command": "event_resume"})
    assert first["event_start_request"] > 4
    assert app.events_view()["trading_controls"]["start_pending"]
    state.write_text(json.dumps(runtime(mode="live", mode_done=NOW, controls=first)))
    assert app.events_view()["trading_controls"]["trading_label"] == "Stop Trading"
    app.command({"command": "event_pause"})
    assert app.events_view()["trading_controls"]["pause_pending"]
    state.write_text(json.dumps(runtime(updated=NOW - 31)))
    assert app.events_view()["trading_controls"]["trading_command"] == "event_pause"
    app.command({"command": "event_pause"})
    with pytest.raises(ValueError, match="stale"):
        app.command({"command": "event_resume"})


def test_paper_and_unavailable_modes_are_honest():
    value = runtime(
        mode="paper",
        bot_address="",
        mode_availability={"simulate": "No vault", "live": "DRY_RUN=true"},
    )
    result = trading_controls(value, {}, "", NOW)
    assert result["mode_disabled"] and result["mode_reason"] == "No vault"
    result = trading_controls(runtime(mode=None), {}, "", NOW)
    assert result["start_block"] and result["trading_command"] == "event_pause"


@pytest.mark.parametrize("fee", [500, 10000])
@pytest.mark.parametrize("buy", [True, False])
def test_swap_prices_directions_and_receipt_legs(fee, buy):
    event = swap(fee, buy=buy)
    row = activity.swap_row(event, fee, timestamp=NOW)
    assert row["side"] == ("buy" if buy else "sell")
    assert row["execution_price"] == "400" and row["spot_price"] == "4"
    assert row["category"] == "unknown" and row["actor"] is None
    # A shared router/event sender must not be attributed to the watched actor.
    log = {k: v for k, v in event.items() if k != "args"}
    log.update(
        topics=[Web3.to_bytes(hexstr=activity.SWAP_TOPIC)],
        data=encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [event["args"]["amount0"], event["args"]["amount1"], 2**95, 100, -1],
        ),
    )
    receipt = {
        k: event[k]
        for k in ("blockNumber", "blockHash", "transactionHash", "transactionIndex")
    }
    assert activity.receipt_legs(receipt | {"logs": [log]}, NOW) == [row]


def observer(tmp_path, monkeypatch):
    calls = []

    def header(n):
        calls.append(("header", n))
        return {"number": n, "timestamp": NOW, "hash": HASH}

    def transaction(h):
        calls.append(("transaction", h))
        return {"hash": h, "from": OPERATOR}

    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_block=header, get_transaction=transaction)
    )
    settings = replace(EventSettings(), bot_address=VAULT)
    return Observer(w3, {}, tmp_path, settings, lambda: NOW), calls


def test_activity_order_dedup_attribution_restart_and_reorg(tmp_path, monkeypatch):
    obs, calls = observer(tmp_path, monkeypatch)
    snapshot = {"block": 7, "block_hash": HASH, "timestamp": NOW}
    rival = obs.settings.competitors[0]
    obs.remember({"hash": TX, "from": rival})
    other_tx = "0x" + "de" * 32
    obs.remember({"hash": other_tx, "from": market.MARKET_BOT})
    logs = [
        (10000, swap(10000, 3, False)),
        (500, swap(index=1)),
        (500, swap(index=2, tx=other_tx)),
    ]
    obs.accept(snapshot, logs + logs, "simulate")
    obs.update_activity(snapshot, [], False, obs.epoch)
    value = activity.read(obs.activity_path)
    assert [r["log_index"] for r in value["rows"]] == [1, 2, 3]
    assert [r["category"] for r in value["rows"]] == [
        "competitor",
        "market_bot",
        "competitor",
    ]
    assert not any(c[0] == "transaction" for c in calls)
    # All swaps are on-chain confirmations even when observed in Simulation.
    assert all(
        r["status"] == "confirmed" and r["observed_mode"] == "simulate"
        for r in value["rows"]
    )
    again, _ = observer(tmp_path, monkeypatch)
    assert not activity.read(again.activity_path)["verified"]
    again.update_activity(snapshot, [], False, again.epoch)
    assert activity.read(again.activity_path)["verified"]
    again.w3.eth.get_block = lambda n: {
        "number": n,
        "hash": "0x" + "ef" * 32,
        "timestamp": NOW,
    }
    again.update_activity(snapshot, [], False, again.epoch)
    value = activity.read(again.activity_path)
    assert not value["verified"] and not any(r["canonical"] for r in value["rows"])
    assert "Reorg" in value["error"]


def test_actor_lookup_budget_and_exit_priority(tmp_path, monkeypatch):
    obs, calls = observer(tmp_path, monkeypatch)
    snapshot = {"block": 7, "block_hash": HASH, "timestamp": NOW}
    obs.accept(
        snapshot,
        [(500, swap(index=i, tx="0x" + f"{i:064x}")) for i in range(20)],
        "live",
    )
    obs.update_activity(snapshot, [], True, obs.epoch)
    assert not any(c[0] == "transaction" for c in calls)
    obs.update_activity(snapshot, [], False, obs.epoch)
    assert sum(c[0] == "transaction" for c in calls) == 4
    rows = activity.read(obs.activity_path)["rows"]
    assert sum(r["category"] == "other" for r in rows) == 4
    assert sum(r["category"] == "unknown" for r in rows) == 16


def test_own_legs_open_position_atomic_cycle_and_nonconfirmed_separation(
    tmp_path, monkeypatch
):
    obs, _ = observer(tmp_path, monkeypatch)
    entry = activity.swap_row(swap(), 500, timestamp=NOW)
    leg2 = activity.swap_row(swap(10000, 2, False), 10000, timestamp=NOW)
    cycle = {
        "status": "confirmed",
        "action": "arbitrage",
        "tx_hash": TX,
        "operator": OPERATOR,
        "legs": [entry, leg2],
        "profit_raw": A,
    }
    rows = activity.own_rows(
        [cycle, cycle | {"status": "simulated"}, cycle | {"status": "reverted"}], VAULT
    )
    assert len(rows) == 2 and rows[0]["tx_hash"] == rows[1]["tx_hash"]
    assert {r["fee"] for r in rows} == {500, 10000}
    assert all(r["category"] == "mine" for r in rows)
    own = cycle | {"action": "entry", "legs": [entry], "position_id": TX}
    obs.update_activity(None, [own], True, obs.epoch)
    value = activity.read(obs.activity_path)
    assert value["verified"] and value["rows"][0]["position_id"] == TX
    # Catch-up of that same log preserves receipt annotations and doesn't duplicate it.
    snap = {"block": 7, "block_hash": HASH, "timestamp": NOW}
    obs.accept(snap, [(500, swap())], "live")
    obs.update_activity(snap, [own], False, obs.epoch)
    assert len(activity.read(obs.activity_path)["rows"]) == 1
    assert activity.read(obs.activity_path)["rows"][0]["category"] == "mine"


@pytest.mark.parametrize("damaged", ["broken", "[]", "{}"])
def test_corrupt_public_history_never_overwritten_and_history_is_bounded(
    tmp_path, monkeypatch, damaged
):
    path = tmp_path / "activity.json"
    path.write_text(damaged)
    obs, _ = observer(tmp_path, monkeypatch)
    obs.update_activity = lambda *a: activity.read(path)
    obs.update(None, [], None, False, obs.epoch)
    assert path.read_text() == damaged and obs.error
    path.unlink()
    value = activity.read(path)
    monkeypatch.setattr(activity, "MAX_ROWS", 3)
    activity.merge(value, [activity.swap_row(swap(index=i), 500) for i in range(6)])
    activity.save(path, value)
    assert len(activity.read(path)["rows"]) == 3 and value["dropped"] == 3


def pinned_chain():
    reads = []
    values = {market.QUOTE: 1000 * A + 1, market.BASE: 5 * A}

    class Call:
        def __init__(self, value):
            self.value = value

        def call(self, *, block_identifier):
            reads.append(block_identifier)
            return self.value

    def contract(address, abi):
        return SimpleNamespace(
            functions=SimpleNamespace(balanceOf=lambda a: Call(values[address.lower()]))
        )

    eth = SimpleNamespace(
        contract=contract,
        get_balance=lambda a, block_identifier: reads.append(block_identifier) or 2 * A,
        get_block=lambda n: {"number": 7, "hash": HASH, "timestamp": NOW},
    )
    pools = {
        500: {
            "pool": SimpleNamespace(
                functions=SimpleNamespace(slot0=lambda: Call([2**95]))
            )
        }
    }
    return SimpleNamespace(eth=eth), pools, reads, values


def test_pinned_actual_balances_include_deposits_and_decimal_valuation(tmp_path):
    w3, pools, reads, values = pinned_chain()
    observation = balances(w3, pools, VAULT, OPERATOR, None, NOW)
    assert reads == [7, 7, 7, 7]
    assert observation["price"] == "4" and observation["base_raw"] == str(5 * A)
    (tmp_path / "portfolio.json").write_text(
        json.dumps(
            {
                "identity": event_history.IDENTITY,
                "contract": VAULT,
                "observation": observation,
            }
        )
    )
    store = EventStore(
        tmp_path,
        event_history.IDENTITY | {"bot_address": VAULT},
        "live",
        clock=lambda: NOW,
    )
    store.finish(
        "entry",
        "confirmed",
        None,
        {
            "action": "entry",
            "amount_raw": A,
            "gas_wei": 10,
            "block": 7,
            "block_hash": HASH,
            "tx_hash": TX,
        },
    )
    p = {"base_raw": A, "quote_raw": 3 * A, "opened": NOW - 20, "entry_block": 7}
    store.data["position"] = p
    store.save()
    result = portfolio.view(
        tmp_path, runtime(position=p, exit_seconds=60, execution_verified=True), NOW
    )
    assert result["observation"]["total_value"] == "1020.000000000000000001"
    assert result["observation"]["gas_eth"] == "2"
    assert result["position"]["marked_value"] == "4"
    assert result["position"]["unrealized_f564"] == "1"
    assert result["performance"]["realized_f564"] == "0"
    assert (
        portfolio.view(tmp_path, runtime(position=p, intent_pending=True), NOW)[
            "position"
        ]["unrealized_f564"]
        is None
    )
    assert result["baseline"] == "Unavailable — baseline not established."
    # External deposits change actual capital, never recorded trading P/L or the baseline.
    values[market.QUOTE] += 500 * A
    observation = balances(w3, pools, VAULT, OPERATOR, None, NOW)
    (tmp_path / "portfolio.json").write_text(
        json.dumps(
            {
                "identity": event_history.IDENTITY,
                "contract": VAULT,
                "observation": observation,
            }
        )
    )
    result2 = portfolio.view(tmp_path, runtime(position=p), NOW)
    assert result2["performance"] == result["performance"]
    assert result2["baseline"] == result["baseline"]
    assert result2["observation"]["total_value"] == "1520.000000000000000001"


def test_holdings_read_failure_retains_original_time_and_amounts(tmp_path):
    w3, pools, _, _ = pinned_chain()
    obs = Observer(
        w3, pools, tmp_path, replace(EventSettings(), bot_address=VAULT), lambda: NOW
    )
    obs.update_holdings(None, OPERATOR)
    old = json.loads(obs.portfolio_path.read_text())["observation"]

    def fail(*a, **k):
        raise ConnectionError("private-provider-body")

    w3.eth.get_block = fail
    obs.update_holdings(None, OPERATOR)
    saved = json.loads(obs.portfolio_path.read_text())
    assert saved["observation"] == old and saved["error"]
    assert "private-provider-body" not in json.dumps(saved)
    assert portfolio.view(tmp_path, runtime(), NOW)["stale"]
    saved["observation"]["base_raw"] = "unavailable"
    obs.portfolio_path.write_text(json.dumps(saved))
    before = obs.portfolio_path.read_bytes()
    with pytest.raises(ValueError, match="raw balance"):
        obs.update_holdings(None, OPERATOR)
    assert obs.portfolio_path.read_bytes() == before


def test_realized_and_gas_coverage_missing_data_and_failed_transactions():
    rows = [
        {"at": 1, "action": "entry", "status": "confirmed", "gas_wei": 10},
        {
            "at": 2,
            "action": "exit",
            "status": "confirmed",
            "profit_raw": -A,
            "gas_wei": 20,
        },
        {
            "at": 3,
            "action": "arbitrage",
            "status": "confirmed",
            "profit_raw": 3 * A,
            "gas_wei": 30,
        },
        {"at": 4, "action": "entry", "status": "reverted", "gas_wei": 40},
    ]
    data = {"trades": rows, "attempts": {str(n): "confirmed" for n in range(5)}}
    result = portfolio.performance(data)
    assert result["realized_f564"] == "2" and result["gas_eth"] == "1E-16"
    assert (
        result["earlier_history_missing"] and result["from"] == 1 and result["to"] == 4
    )
    del rows[3]["gas_wei"]
    assert portfolio.performance(data)["gas_eth"] is None
    del rows[1]["profit_raw"]
    assert portfolio.performance(data)["realized_f564"] is None
    rows[0]["status"] = "simulated"
    with pytest.raises(ValueError, match="non-live"):
        portfolio.performance(data)


@pytest.fixture
def retained_live_ledger(tmp_path):
    data = {
        "version": 1,
        "identity": event_history.IDENTITY | {"bot_address": VAULT},
        "position": None,
        "attempts": {"exit": "confirmed", "cycle": "confirmed", "failed": "reverted"},
        "trades": [
            {
                "at": 100,
                "action": "exit",
                "status": "confirmed",
                "profit_raw": -A + 1,
                "gas_wei": 10,
            },
            {
                "at": 200.5,
                "action": "arbitrage",
                "status": "confirmed",
                "profit_raw": 3 * A + 2,
                "gas_wei": 20,
            },
            {"at": 300, "action": "entry", "status": "reverted", "gas_wei": 40},
        ],
    }
    path = tmp_path / "execution_live.json"
    path.write_text(json.dumps(data))
    return path, data


@pytest.mark.parametrize("missing", ["absent", "null", "mixed", "none"])
def test_file_backed_performance_keeps_exact_totals_without_dates(
    tmp_path, retained_live_ledger, missing
):
    path, data = retained_live_ledger
    if missing in ("absent", "mixed"):
        del data["trades"][0]["at"]
    elif missing == "null":
        data["trades"][0]["at"] = None
    if missing == "mixed":
        data["trades"][2]["at"] = None
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    result = portfolio.view(tmp_path, runtime(execution_verified=True), NOW)
    perf = result["performance"]
    assert result["performance_error"] == ""
    assert result["position_verified"] is True and result["position"] is None
    assert perf["realized_f564"] == "2.000000000000000003"
    assert perf["gas_eth"] == "7E-17"
    assert perf["attempts"] == 3 and perf["completed"] == 2
    assert perf["gas_missing"] == 0 and not perf["earlier_history_missing"]
    assert (
        perf["timestamps_missing"]
        == {"absent": 1, "null": 1, "mixed": 2, "none": 0}[missing]
    )
    if missing == "none":
        assert (perf["from"], perf["to"]) == (100, 300)
    else:
        assert perf["from"] is None and perf["to"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "invalid", [True, False, "100", -1, float("nan"), float("inf"), [], {}]
)
def test_supplied_invalid_timestamp_rejects_accounting_and_flat_verification(
    tmp_path, retained_live_ledger, invalid
):
    path, data = retained_live_ledger
    # Missing data in another row must not hide an explicitly malformed timestamp.
    del data["trades"][0]["at"]
    data["trades"][1]["at"] = invalid
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="invalid accounting timestamp"):
        portfolio.performance(data)
    result = portfolio.view(tmp_path, runtime(execution_verified=True), NOW)
    assert result["performance"] is None
    assert "reconcile" in result["performance_error"]
    assert result["position_verified"] is False


@pytest.mark.parametrize(
    "guard",
    ["canonical_invalid", "halted", "contract", "unverified", "pending", "position"],
)
def test_flat_verification_requires_canonical_matching_runtime(
    tmp_path, retained_live_ledger, guard
):
    path, data = retained_live_ledger
    state = runtime(execution_verified=True)
    if guard == "canonical_invalid":
        data["canonical_invalid"] = True
    elif guard == "halted":
        state["health"] = "halted"
    elif guard == "contract":
        data["identity"]["bot_address"] = OPERATOR
    elif guard == "unverified":
        del state["execution_verified"]
    elif guard == "pending":
        state["intent_pending"] = True
    else:
        data["position"] = {"base_raw": A, "quote_raw": 3 * A, "opened": NOW - 20}
    path.write_text(json.dumps(data))
    result = portfolio.view(tmp_path, state, NOW)
    assert result["position_verified"] is False
    assert result["position"] is None
    if guard in ("canonical_invalid", "halted", "contract"):
        assert result["performance"] is None and result["performance_error"]
    else:
        assert result["performance"]["realized_f564"] == "2.000000000000000003"


def test_existing_empty_ledger_is_distinct_from_missing_accounting(tmp_path):
    state = runtime(execution_verified=True)
    absent = portfolio.view(tmp_path, state, NOW)
    assert absent["performance"] is None and not absent["position_verified"]
    EventStore(tmp_path, event_history.IDENTITY | {"bot_address": VAULT}, "live")
    empty = portfolio.view(tmp_path, state, NOW)
    assert empty["position_verified"] is True
    assert empty["performance"]["attempts"] == 0
    assert empty["performance"]["timestamps_missing"] == 0
    assert empty["performance"]["from"] is None
    assert empty["performance"]["to"] is None
    assert empty["performance"]["realized_f564"] == "0"
    assert empty["performance"]["gas_eth"] == "0"


def test_diagnostics_do_not_block_execution_thread(tmp_path, monkeypatch):
    obs, _ = observer(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)

    obs.update = slow
    ex = SimpleNamespace(live_history=None, operator=None)
    obs.schedule(ex, None, {})
    assert entered.wait(2)
    # The caller is free to execute exit/receipt checks while the diagnostic read waits.
    assert obs.thread.is_alive()
    old = obs.thread
    obs.schedule(ex, None, {})
    assert obs.thread is old
    release.set()
    obs.thread.join(2)


def test_dashboard_all_observation_views_are_file_only(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(
        requests.adapters.HTTPAdapter,
        "send",
        lambda *a, **k: pytest.fail("dashboard RPC"),
    )
    app = Dashboard(
        True,
        state_file=tmp_path / "state.json",
        control_file=tmp_path / "control.json",
        clock=lambda: NOW,
    )
    before = copy.deepcopy(list(tmp_path.rglob("*")))
    assert app.events_view()["portfolio"]["performance"] is None
    assert app.event_activity_view({})["rows"] == []
    assert (
        app.event_prices_view({"mode": ["all"], "from": ["1"], "to": [str(NOW)]})[
            "samples"
        ]
        == 0
    )
    assert list(tmp_path.rglob("*")) == before


def test_slow_diagnostic_http_does_not_lock_execution_transport(monkeypatch):
    import requests

    import config
    import rpc_metrics as rpc

    metrics = rpc.RpcMetrics()
    provider = config.ThrottledProvider(
        "http://offline.invalid",
        rate=100,
        metrics=metrics,
        exception_retry_configuration=None,
    )
    entered, release = threading.Event(), threading.Event()

    def send(_, request, **kwargs):
        payload = json.loads(request.body)
        if payload["method"] == "eth_getBalance":
            entered.set()
            assert release.wait(5)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": "0x1"}
        ).encode()
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    errors = []

    def diagnostic():
        try:
            with rpc.purpose(rpc.DIAGNOSTICS):
                provider.make_request("eth_getBalance", [])
        except Exception as exc:  # noqa: BLE001 — report thread failures to the test
            errors.append(exc)

    thread = threading.Thread(target=diagnostic)
    thread.start()
    assert entered.wait(2)
    with rpc.purpose(rpc.RECEIPTS):
        assert provider.make_request("eth_blockNumber", [])["result"] == "0x1"
    release.set()
    thread.join(2)
    assert not errors
    counts = metrics.snapshot()
    assert counts["total"] == 2
    assert (
        counts["by_purpose"][rpc.DIAGNOSTICS] == counts["by_purpose"][rpc.RECEIPTS] == 1
    )
