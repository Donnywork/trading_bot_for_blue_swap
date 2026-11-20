"""Isolated Chromium + synthetic files; never starts the connected event runtime.

Run with PHASE2_BROWSER_TESTS=1 after `uv run playwright install chromium`.
"""

import json
import os
import threading
import time
from dataclasses import replace

import pytest
from playwright.sync_api import expect, sync_playwright

import control
import event_activity as activity
import event_history
import event_market as market
import strategy
from dashboard import Dashboard, serve

pytestmark = pytest.mark.skipif(
    os.environ.get("PHASE2_BROWSER_TESTS") != "1",
    reason="Run make test-browser with isolated Chromium installed",
)
VAULT = "0x" + "11" * 20
ACTOR = "0x" + "22" * 20


@pytest.fixture
def browser_dashboard(tmp_path):
    now = int(time.time())
    events = tmp_path / "events"
    events.mkdir()
    cfg = replace(strategy.DEFAULT, event_mode="simulate")
    control.save(tmp_path / "control.json", cfg)
    value = {
        "updated": now,
        "health": "ready",
        "mode": "simulate",
        "bot_address": VAULT,
        "mode_done": 0,
        "start_floor": 0,
        "connected": True,
        "snapshot_time": now,
        "snapshot_block": 300,
        "position": None,
        "intent_pending": False,
        "controls": {k: getattr(cfg, k) for k in control.EVENT_FIELDS},
        "mode_availability": {"simulate": "", "live": ""},
        "decisions": [
            {
                "mode": "simulate",
                "now": now - i,
                "status": "prepared",
                "reason": "Synthetic decision " + str(i),
                "hash": "0x" + f"{i:064x}",
            }
            for i in range(30)
        ],
    }

    def save():
        (events / "state.json").write_text(json.dumps(value))

    save()
    rows = [
        {
            "identity": event_history.IDENTITY,
            "mode": "simulate",
            "session": 1,
            "observed": now - 3600 + i * 12,
            "timestamp": now - 3600 + i * 12,
            "block": i,
            "block_hash": "0x" + f"{i:064x}",
            "prices": {"500": 400 + (i % 30), "10000": 420 + (i % 20)},
        }
        for i in range(301)
        if i not in range(80, 90)
    ]
    (events / "prices.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    swaps = []
    for i, (fee, category, side, action) in enumerate(
        [
            (500, "mine", "buy", "entry"),
            (10000, "mine", "sell", "exit"),
            (500, "market_bot", "buy", "swap"),
            (500, "competitor", "sell", "swap"),
            (500, "mine", "buy", "arbitrage"),
            (10000, "mine", "sell", "arbitrage"),
        ]
    ):
        tx = "0x" + f"{min(4, i):064x}"
        swaps.append(
            {
                "id": tx + ":" + str(i),
                "tx_hash": tx,
                "block_hash": rows[-1]["block_hash"],
                "block": 300,
                "transaction_index": i,
                "log_index": i,
                "fee": fee,
                "pool": market.POOLS[fee],
                "timestamp": now - 720 + i * 60,
                "side": side,
                "base_raw": str(10**18),
                "quote_raw": str((405 + i) * 10**18),
                "execution_price": str(405 + i),
                "spot_price": str(406 + i),
                "actor": ACTOR,
                "category": category,
                "action": action,
                "position_id": "position-one" if i < 2 else None,
                "status": "confirmed",
                "canonical": True,
                "observed_mode": "live",
                "session": 1,
            }
        )
    activity.save(
        events / "activity.json",
        {
            "version": 1,
            "identity": event_history.IDENTITY,
            "rows": swaps,
            "anchor": {
                "block": 300,
                "block_hash": rows[-1]["block_hash"],
                "timestamp": now,
            },
            "verified": True,
            "started": now - 3600,
            "dropped": 0,
        },
    )
    app = Dashboard(
        True, state_file=tmp_path / "state.json", control_file=tmp_path / "control.json"
    )
    server = serve(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 1000}, has_touch=True
        )
        page = context.new_page()
        errors, external = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )

        def restrict(route):
            if not route.request.url.startswith(
                f"http://127.0.0.1:{server.server_port}/"
            ):
                external.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.route("**/*", restrict)
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        expect(page.locator("#chart-10000 svg")).to_be_visible()
        yield page, value, save, tmp_path, errors, external
        browser.close()
    server.shutdown()
    server.server_close()
    thread.join()
    assert not errors
    assert not external


def refresh(page):
    page.wait_for_function("() => !busy && !commanding")
    page.evaluate("refresh()")
    page.wait_for_function("() => !busy && !commanding")


def test_fee_evidence_distinguishes_selection_acceptance_and_inclusion(
    browser_dashboard,
):
    page, value, save, tmp, _, _ = browser_dashboard
    before = (tmp / "control.json").read_text()
    value["fee_policy"] = "competitive"
    value["execution_attempt"] = {
        "fee_decision": {
            "policy": "competitive",
            "action": "exit",
            "decided_at": time.time(),
            "reference_block": 300,
            "desired_priority_wei": 300_000_000_000,
            "selected_priority_wei": 100_000_000_000,
            "selected_max_fee_wei": 120_000_000_000,
            "allowed": True,
            "reasons": ["priority_ceiling"],
            "network_status": "stale",
        },
        "timing": {"provider_accepted": True, "broadcast_ms": 200},
    }
    save()
    refresh(page)
    expect(page.locator("#fee-evidence")).to_contain_text("priority_ceiling")
    expect(page.locator("#fee-evidence")).to_contain_text("Network: stale")
    expect(page.locator("#fee-evidence")).to_contain_text(
        "first seen to broadcast unavailable"
    )
    expect(page.locator("#fee-evidence")).to_contain_text(
        "Provider accepted. Inclusion unavailable."
    )
    assert (tmp / "control.json").read_text() == before


def test_two_controls_acknowledgements_refusal_and_stale_stop(browser_dashboard):
    page, value, save, tmp, _, _ = browser_dashboard
    expect(page.locator("#controls button")).to_have_count(2)
    expect(page.locator("#controls button").nth(0)).to_have_text("Mode: Simulation")
    page.get_by_role(
        "button",
        name="Active mode: Simulation. Request Live (Sepolia); pauses entries.",
        exact=True,
    ).click()
    expect(page.locator("#controls button").nth(0)).to_have_text("Switching to Live…")
    expect(page.locator("#controls button").nth(1)).to_be_disabled()
    saved = control.load(tmp / "control.json")
    assert saved.event_paused
    value["mode_done"] = saved.event_mode_request
    value["mode_block"] = "Synthetic signer refusal"
    save()
    refresh(page)
    expect(page.locator("#controls button").nth(0)).to_have_text("Mode: Simulation")
    expect(page.locator("#control-help")).to_contain_text("Synthetic signer refusal")
    value.update(mode="live", mode_block="")
    value["controls"] = {k: getattr(saved, k) for k in control.EVENT_FIELDS}
    save()
    refresh(page)
    expect(page.locator("#controls button").nth(1)).to_have_text("Start Trading")
    page.locator("#controls button").nth(1).click()
    expect(page.locator("#control-state")).to_contain_text(
        "Start request awaiting runtime"
    )
    expect(page.locator("#controls button").nth(1)).to_have_text("Stop Trading")
    saved = control.load(tmp / "control.json")
    value["controls"] = {k: getattr(saved, k) for k in control.EVENT_FIELDS}
    save()
    refresh(page)
    expect(page.locator("#control-state")).to_contain_text("Entries enabled")
    value["updated"] = time.time() - 40
    save()
    refresh(page)
    expect(page.locator("#controls button").nth(1)).to_be_enabled()
    page.locator("#controls button").nth(1).click()
    page.wait_for_function("() => !commanding")
    assert control.load(tmp / "control.json").event_paused
    expect(page.locator("#controls button").nth(1)).to_have_text("Stop Trading")


def test_chart_interactions_annotations_and_decision_pages(browser_dashboard):
    page, _, _, tmp, _, _ = browser_dashboard
    first = page.locator("#chart-500").bounding_box()
    second = page.locator("#chart-10000").bounding_box()
    assert first["x"] == second["x"] and second["y"] > first["y"] + first["height"]
    assert page.locator("#chart-500 .entry-level").count() == 1
    assert page.locator("#chart-10000 .holding").count() == 1
    page.locator("#chart-500").hover(position={"x": 300, "y": 120})
    expect(page.locator("#crosshair-500")).to_have_attribute("visibility", "visible")
    expect(page.locator("#crosshair-10000")).to_have_attribute("visibility", "visible")
    expect(page.locator("#crosshair-price-500")).to_have_attribute(
        "visibility", "visible"
    )
    expect(page.locator("#cursor-500")).to_contain_text("Recorded")
    before = page.evaluate("({...view})")
    page.mouse.wheel(0, -200)
    page.wait_for_function("() => !following")
    after = page.evaluate("({...view})")
    assert after["to"] - after["from"] < before["to"] - before["from"]
    # Polling must preserve a manually inspected interval exactly.
    history = tmp / "events/prices.jsonl"
    points = history.read_text().splitlines()
    newest = json.loads(points[-1])
    newest.update(
        timestamp=int(time.time()),
        observed=int(time.time()),
        block=301,
        block_hash="0x" + f"{301:064x}",
    )
    newest["prices"] = {"500": 450, "10000": 460}
    history.write_text("\n".join(points + [json.dumps(newest)]))
    refresh(page)
    assert page.evaluate("({...view})") == after
    box = page.locator("#chart-500").bounding_box()
    page.mouse.move(box["x"] + 400, box["y"] + 180)
    page.mouse.down()
    page.mouse.move(box["x"] + 500, box["y"] + 180, steps=6)
    page.mouse.up()
    assert page.evaluate("view.from") < after["from"]
    page.get_by_role("button", name="Reset", exact=True).click()
    page.wait_for_function("() => view.to-view.from===3600")
    page.get_by_role("button", name="Follow latest", exact=True).click()
    expect(page.locator("#follow")).to_have_attribute("aria-pressed", "true")
    page.locator("#chart-500 [data-marker]").first.focus()
    expect(page.locator("#tooltip-500")).to_contain_text("execution price")
    expect(page.locator("#tooltip-10000")).to_contain_text("position-one")
    page.get_by_text("Recent decisions and watched actors", exact=True).click()
    expect(page.locator("#decisions tr")).to_have_count(10)
    page.get_by_role("button", name="Next decisions", exact=True).click()
    expect(page.locator("#decisions-page")).to_contain_text("page 2 / 3")
    held = page.locator("#decisions").inner_text()
    refresh(page)
    assert page.locator("#decisions").inner_text() == held
    assert (
        page.locator('[aria-label="Recent decisions table"]').evaluate(
            "e=>getComputedStyle(e).overflowY"
        )
        == "auto"
    )
    page.screenshot(path=str(tmp / "desktop.png"), full_page=True)
    print("Desktop browser screenshot:", tmp / "desktop.png")


@pytest.mark.parametrize("missing_at", ["absent", "null"])
def test_stale_holdings_flat_inventory_and_incomplete_ledger_dates(
    browser_dashboard, missing_at
):
    page, value, save, tmp, _, _ = browser_dashboard
    now = value["updated"]
    value.update(mode="live", execution_verified=True)
    save()
    (tmp / "events/portfolio.json").write_text(
        json.dumps(
            {
                "identity": event_history.IDENTITY,
                "contract": VAULT,
                "observation": {
                    "quote_raw": str(1024 * 10**18),
                    "base_raw": str(2 * 10**18),
                    "gas_wei": str(10**18),
                    "block": 300,
                    "block_hash": "0x" + "ab" * 32,
                    "timestamp": now - 120,
                    "observed": now - 120,
                    "price": "400",
                    "pool": market.POOLS[500],
                    "fee": 500,
                    "operator": ACTOR,
                },
            }
        )
    )
    execution = {
        "version": 1,
        "identity": event_history.IDENTITY | {"bot_address": VAULT},
        "position": None,
        "attempts": {"first": "confirmed", "second": "confirmed"},
        "trades": [
            {
                "action": "arbitrage",
                "status": "confirmed",
                "profit_raw": 2298 * 10**16,
                "gas_wei": 2 * 10**15,
            },
            {
                "at": now - 60,
                "action": "backrun",
                "status": "confirmed",
                "profit_raw": 125 * 10**16,
                "gas_wei": 15 * 10**14,
            },
        ],
    }
    if missing_at == "null":
        execution["trades"][0]["at"] = None
    path = tmp / "events/execution_live.json"
    path.write_text(json.dumps(execution))
    before = path.read_bytes()
    refresh(page)
    assert page.evaluate("status.portfolio.stale") is True
    assert page.evaluate("status.portfolio.position_verified") is True
    expect(page.locator("#holdings-time")).to_contain_text("STALE / last successful")
    pnl = page.locator("#pnl-metrics")
    expect(
        pnl.locator(".metric").filter(has_text="Realized trading P/L")
    ).to_contain_text("24.23 f564")
    expect(pnl.locator(".metric").filter(has_text="Execution gas")).to_contain_text(
        "0.0035 ETH"
    )
    unrealized = pnl.locator(".metric").filter(has_text="Open-position unrealized")
    expect(unrealized).to_contain_text("No open position")
    expect(pnl).not_to_contain_text("stale mark")
    expect(page.locator("#position")).not_to_contain_text("stale mark")
    expect(page.locator("#pnl-basis")).to_contain_text(
        "2 retained attempts; 2 completed"
    )
    expect(page.locator("#pnl-basis")).to_contain_text(
        "date range unavailable — 1 retained attempt(s) missing timestamps"
    )
    expect(page.locator("#pnl-basis")).not_to_contain_text("ledger has no executions")
    assert path.read_bytes() == before

    # A real position may carry a stale mark, but an unverified position may not.
    position = {
        "base_raw": 10**18,
        "quote_raw": 390 * 10**18,
        "opened": now - 180,
        "entry_block": 299,
    }
    execution["position"] = value["position"] = position
    path.write_text(json.dumps(execution))
    save()
    refresh(page)
    expect(unrealized).to_contain_text("stale mark")
    expect(unrealized).to_contain_text("10 f564")
    expect(page.locator("#position")).to_contain_text("(stale mark)")
    value["execution_verified"] = False
    save()
    refresh(page)
    expect(unrealized).not_to_contain_text("stale mark")
    expect(unrealized).not_to_contain_text("No open position")
    expect(page.locator("#position")).not_to_contain_text("stale mark")

    # Empty accounting has different coverage copy from reconciled rows lacking dates.
    value.update(position=None, execution_verified=True)
    execution.update(position=None, attempts={}, trades=[])
    path.write_text(json.dumps(execution))
    save()
    refresh(page)
    expect(unrealized).to_contain_text("No open position")
    expect(page.locator("#pnl-basis")).to_contain_text(
        "existing ledger has no executions"
    )
    expect(page.locator("#pnl-basis")).not_to_contain_text("missing timestamps")
    execution["canonical_invalid"] = True
    path.write_text(json.dumps(execution))
    refresh(page)
    expect(unrealized).not_to_contain_text("No open position")
    expect(page.locator("#pnl-basis")).to_contain_text("reconcile the execution ledger")


def test_mobile_empty_stale_and_read_error_states(browser_dashboard):
    page, value, save, tmp, _, _ = browser_dashboard
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#chart-500 svg")).to_be_visible()
    expect(page.locator("#chart-10000 svg")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#chart-500").scroll_into_view_if_needed()
    box = page.locator("#chart-500").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 100
    page.touchscreen.tap(x, y)
    expect(page.locator("#crosshair-500")).to_have_attribute("visibility", "visible")
    cdp = page.context.new_cdp_session(page)
    width = page.evaluate("view.to-view.from")
    cdp.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchStart",
            "touchPoints": [{"x": x - 30, "y": y}, {"x": x + 30, "y": y}],
        },
    )
    cdp.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchMove",
            "touchPoints": [{"x": x - 60, "y": y}, {"x": x + 60, "y": y}],
        },
    )
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    assert page.evaluate("view.to-view.from") < width
    page.locator("#chart-500").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#window-state")).to_have_text("Inspecting history")
    (tmp / "events/prices.jsonl").write_text("")
    refresh(page)
    expect(page.locator("#chart-message")).to_contain_text("Empty history")
    (tmp / "events/prices.jsonl").write_text("damaged")
    refresh(page)
    expect(page.locator("#chart-message")).to_contain_text("unreadable")
    value["updated"] = time.time() - 60
    value.update(mode="paper", mode_availability={"simulate": "Configure a vault"})
    save()
    refresh(page)
    expect(page.locator("#controls button").nth(0)).to_have_text("Mode: Paper")
    expect(page.locator("#control-help")).to_contain_text("Configure a vault")
    expect(page.locator("#control-state")).to_contain_text("stale")
    expect(page.locator("#holdings-time")).to_contain_text(
        "No contract holdings observation"
    )
    page.screenshot(path=str(tmp / "mobile.png"), full_page=True)
    print("Mobile browser screenshot:", tmp / "mobile.png")


@pytest.mark.parametrize(
    "failure",
    [
        "halted",
        "unverified_activity",
        "invalid_ledger",
        "unverified_execution",
        "pending_intent",
    ],
)
def test_entry_fallback_requires_canonical_execution_and_activity(
    browser_dashboard, failure
):
    page, value, save, tmp, _, _ = browser_dashboard
    now = value["updated"]
    position = {
        "base_raw": 10**18,
        "quote_raw": 10 * 10**18,
        "opened": now - 90,
        "entry_time": now - 90,
        "entry_fee": 500,
        "exit_fee": 10000,
        "entry_block": 299,
        "entry_tx": "0x" + "cd" * 32,
        "entry_block_hash": "0x" + "ef" * 32,
    }
    value.update(position=position, execution_verified=True, mode="live")
    execution = {
        "version": 1,
        "identity": event_history.IDENTITY | {"bot_address": VAULT},
        "position": position,
        "trades": [],
        "attempts": {},
    }
    execution_path = tmp / "events/execution_live.json"
    execution_path.write_text(json.dumps(execution))
    activity_path = tmp / "events/activity.json"
    history = activity.read(activity_path)
    history["rows"] = []  # A retained position can outlive its activity annotation.
    activity.save(activity_path, history)
    save()
    refresh(page)
    expect(page.locator("#chart-500 .entry-level")).to_have_count(1)
    assert page.evaluate("bounds(500)[0]") < 10
    if failure == "halted":
        value["health"] = "halted"
    elif failure == "unverified_activity":
        history["verified"] = False
        activity.save(activity_path, history)
    elif failure == "invalid_ledger":
        execution["canonical_invalid"] = True
        execution_path.write_text(json.dumps(execution))
    elif failure == "unverified_execution":
        value["execution_verified"] = False
    else:
        value["intent_pending"] = True
    save()
    refresh(page)
    expect(page.locator("#chart-500 .entry-level")).to_have_count(0)
    expect(page.locator("#chart-500")).not_to_contain_text("Entry 10")
    assert page.evaluate("bounds(500)[0]") > 350
    assert page.evaluate("status.portfolio.position.base") == "1"
