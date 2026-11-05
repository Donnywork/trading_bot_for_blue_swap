import base64
import bisect
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

import compare
import control
import history
import ledger
import state
import status
import strategy
import v3pool
from main import PRELOAD_MAX_AGE, redact

# Loopback only, on purpose: the page can change what the bot does, and it has no login. Reach it
# from your laptop through an SSH tunnel (see docs/DEPLOY.md), never by opening a port.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PAGE = Path(__file__).parent / "dashboard.html"
MAX_BODY = 10_000
# what /api/status carries: the price card's change over a few hours, the guard, the last trades.
# The chart and the trades list ask /api/prices and /api/trades for what they show.
CHART_POINTS = 720  # 6 hours at one sample per 30s
RECENT_TRADES = 20
HISTORY_POINTS = 100_000  # more than the history file can hold (see history.MAX_BYTES)
CHART_MAX_POINTS = (50, 3000)  # what one chart request may ask for
CHART_DEFAULT_POINTS = 900
TRADES_PAGE_MAX = 100
COMPARE_MAX_HOURS = 24 * 30
COMPARE_REASONS = ("take_profit", "stop_loss", "time_stop")
CACHE_ENTRIES = 24
# only these host names may be used to reach the page (any port, as a tunnel may remap it): a
# web page on another site can point a name of its own at 127.0.0.1 ("DNS rebinding")
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(:\d+)?")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# JSON and error responses load nothing at all
BASE_CSP = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def page_csp(page):
    # The page has exactly one inline <script> and one inline <style>. Allow those two by hash and
    # nothing else, so injected markup could not run even if an XSS sink were ever introduced.
    html = page.decode()

    def digest(tag):
        inner = re.search(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL).group(1)
        return (
            "'sha256-"
            + base64.b64encode(hashlib.sha256(inner.encode()).digest()).decode()
            + "'"
        )

    return (
        f"default-src 'none'; script-src {digest('script')}; style-src {digest('style')}; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def text_values(chain):
    # JSON has no Decimal
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in chain.items()}


class QueryError(ValueError):
    """A request's query string is not acceptable; the message says why."""


def query_number(query, name, default, low, high):
    # one number from a query string, inside limits (an HTTP 400 with the reason if it is not)
    raw = query.get(name, [None])[0]
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not low <= value <= high:  # also false for nan
        raise QueryError(f"{name} must be a number between {low:g} and {high:g}")
    return value


def downsample(points, limit):
    # Fewer than `limit` points that keep the shape: the price jumps in steps, so each bucket keeps
    # its lowest and highest sample (in time order) rather than an average that would hide them.
    if len(points) <= limit:
        return points
    # the first and last sample are always kept, and each of the other buckets adds two at most
    size = -(-len(points) // max(1, limit // 2 - 1))  # ceiling division
    out = [points[0]]
    for i in range(0, len(points), size):
        bucket = points[i : i + size]
        low = min(bucket, key=lambda p: p[1])
        high = max(bucket, key=lambda p: p[1])
        out.extend(sorted({low, high}))
    out.append(points[-1])
    return list(
        dict.fromkeys(out)
    )  # a sample picked twice (an end that is also an extreme) once


class Dashboard:
    # ------------------------------------
    # Everything the page needs, kept apart from HTTP so it can be tested. Read-only except
    # update_config(), which writes the settings file the bot re-reads every tick. It never asks
    # the RPC anything: the price and balances come from the file the bot writes each tick.
    # ------------------------------------

    def __init__(
        self,
        dry_run,
        state_file=state.STATE_FILE,
        control_file=control.CONTROL_FILE,
        history_file=history.HISTORY_FILE,
        ledger_file=None,
        clock=time.time,
        cooldown=300,
        size=Decimal(1000),
        depth_file=None,
    ):
        self.dry_run, self.state_file, self.clock = dry_run, state_file, clock
        # only for the strategy comparison's paper replay (the bot's own COOLDOWN_SECONDS and
        # MAX_TRADE_AMOUNT); the dashboard never changes either
        self.cooldown, self.size = cooldown, size
        # charge the replay's trades what their size really costs on the pool's liquidity layout
        # (v3pool: the one the bot scanned and saved in `depth_file`, else the one that ships with
        # the code), scaled by how deep the bot last found the pool (in the state file); None: the
        # replay uses the fixed cost per trade. Only ever read here, never scanned from here.
        self.depth_file = depth_file
        self.cache = {}
        self.cache_lock = (
            threading.Lock()
        )  # the server answers each request in its own thread
        # one replay at a time: it is CPU work, and a page asking in two tabs must not start the
        # same one twice (re-entrant: a view's make() may ask for the replay it is built from)
        self.compute_lock = threading.RLock()
        self.control_file, self.history_file = control_file, history_file
        self.ledger_file = ledger_file or ledger.file_for(dry_run)
        self.write_lock = threading.Lock()  # one settings write at a time

    def snapshot(self):
        now = self.clock()

        problems = []
        try:
            chain = state.load(self.state_file)
            if chain is None:
                problems.append(
                    f"no price data yet: the bot has not written {self.state_file.name} "
                    "(not started, or running on another machine)"
                )
            elif now - chain["updated"] > status.STALE_AFTER_SECONDS:
                problems.append(
                    f"the bot last read the chain {status.span(now - chain['updated'])} ago, "
                    "so the price and balances are old"
                )
        except ValueError as e:
            chain = None
            problems.append(f"{e}. The bot rewrites it on its next tick.")
        try:
            data = ledger.load(self.ledger_file)
        except ValueError as e:
            data = ledger.empty()
            problems.append(str(e))
        try:
            cfg = control.load(self.control_file)
        except ValueError as e:
            cfg = strategy.DEFAULT
            problems.append(f"{e}. The bot keeps its last valid settings.")

        points = history.tail(self.history_file, CHART_POINTS)
        kind, text = status.read_health(now)
        summary = ledger.summarize(data)
        return {
            "now": now,
            "mode": "dry_run" if self.dry_run else "live",
            "health": {"kind": kind, "text": text},
            "problems": problems,
            "chain": text_values(chain) if chain else None,
            "position": self.position_view(data["open"], chain, cfg),
            "summary": {
                k: str(v) if isinstance(v, Decimal) else v for k, v in summary.items()
            },
            "trades": [
                {**t, "profit": str(ledger.profit(t))}
                for t in reversed(data["closed"][-RECENT_TRADES:])
            ],
            "prices": points,
            # the same prices the bot would restore after a restart: if the file is stale or has
            # a long gap, both of them judge the pattern from nothing
            "guard": strategy.guard_status(
                history.trailing_run(points, now, PRELOAD_MAX_AGE), cfg
            ),
            "config": strategy.to_dict(cfg),
            "control": self.control_view(cfg),
            "schema": strategy.schema(),
        }

    def ledger_data(self):
        try:
            return ledger.load(self.ledger_file)
        except ValueError:  # snapshot() reports it; the other views just show nothing
            return ledger.empty()

    def prices_view(self, query):
        # ------------------------------------
        # The chart's data for one time window, thinned to at most `max` points, plus the trades
        # that touch the window (for the markers). `range` is seconds back from now or "all";
        # `from` / `to` (unix times) pick an exact window, as a zoom does.
        # ------------------------------------

        now = self.clock()
        points = self.recorded_prices()
        limit = int(query_number(query, "max", CHART_DEFAULT_POINTS, *CHART_MAX_POINTS))
        if "from" in query or "to" in query:
            lo = query_number(query, "from", 0, 0, now + 3600)
            hi = query_number(query, "to", now, 0, now + 3600)
            if hi <= lo:
                raise QueryError("to must be after from")
        elif query.get("range", ["21600"])[0] == "all":
            lo, hi = 0, now
        else:
            lo, hi = now - query_number(query, "range", 21600, 60, 86400 * 400), now
        stamps = [p[0] for p in points]
        window = points[
            bisect.bisect_left(stamps, lo) : bisect.bisect_right(stamps, hi)
        ]
        trades = [
            t
            for t in self.ledger_data()["closed"]
            if t["closed"] >= lo and t["opened"] <= hi
        ]
        return {
            "points": downsample(window, limit),
            "samples": len(window),
            "trades": trades,
        }

    def trades_view(self, query):
        # one page of closed trades, newest first, filtered by why they closed
        reason = query.get("reason", ["all"])[0]
        if reason != "all" and reason not in COMPARE_REASONS:
            raise QueryError(f"reason must be all or one of {list(COMPARE_REASONS)}")
        limit = int(query_number(query, "limit", 25, 1, TRADES_PAGE_MAX))
        offset = int(query_number(query, "offset", 0, 0, 10**9))
        closed = [
            t
            for t in reversed(self.ledger_data()["closed"])
            if reason == "all" or t["reason"] == reason
        ]
        total = sum((ledger.profit(t) for t in closed), Decimal(0))
        spent = sum((Decimal(t["quote_spent"]) for t in closed), Decimal(0))
        return {
            "total": len(closed),
            "profit": str(total),
            "profit_pct": float(total / spent * 100) if spent else 0.0,
            "items": [
                {**t, "profit": str(ledger.profit(t))}
                for t in closed[offset : offset + limit]
            ],
        }

    def remembered(self, key, make):
        # a few recent answers, so a view that is asked again with nothing new is not recomputed
        with self.cache_lock:
            if key in self.cache:
                return self.cache[key]
        with self.compute_lock:
            with self.cache_lock:  # another thread may have just made it
                if key in self.cache:
                    return self.cache[key]
            value = make()
            with self.cache_lock:
                self.cache[key] = value
                while len(self.cache) > CACHE_ENTRIES:
                    self.cache.pop(next(iter(self.cache)))
            return value

    def recorded_prices(self):
        # oldest first, whatever order the file is in (a clock that stepped back, a hand edit): the
        # views cut windows with bisect, and a replay reads time forwards
        return sorted(history.tail(self.history_file, HISTORY_POINTS))

    def replay_window(self, query):
        # the recorded prices and where the replay's window starts, for compare_view and
        # preview_view; `signature` changes whenever a new price arrives
        now = self.clock()
        raw = query.get("hours", ["24"])[0]
        hours = (
            None
            if raw == "all"
            else query_number(query, "hours", 24, 1, COMPARE_MAX_HOURS)
        )
        points = self.recorded_prices()
        start = points[0][0] if hours is None and points else now - (hours or 24) * 3600
        # the window's start moves with the clock, so it is part of what an answer is for
        signature = (
            points[-1] if points else None,
            len(points),
            hours,
            int(start // 60),
        )
        return points, start, signature

    def pool_depth(self):
        # The saved layout scaled by what the bot last wrote in the state file (never a chain
        # read: 1.0, as saved, until the bot has sized a buy); None when not asked for.
        if self.depth_file is None:
            return None
        try:
            chain = state.load(self.state_file)
        except ValueError:
            chain = None  # snapshot() reports it
        return v3pool.scaled(
            v3pool.current_profile(self.depth_file),
            chain["depth_scale"] if chain else 1.0,
        )

    @staticmethod
    def depth_key(depth):
        # what a cached replay depends on: which layout and how deep
        return (depth["block"], round(depth["scale"], 2)) if depth else None

    def replayed(self, points, start, signature, cfg):
        # every strategy's replay under `cfg`, with warnings; cached per (prices, settings, depth)
        depth = self.pool_depth()
        key = (
            "rows",
            signature,
            json.dumps(strategy.to_dict(cfg), sort_keys=True),
            self.depth_key(depth),
        )

        def make():
            window = [p for p in points if p[0] >= start]
            stats = compare.step_stats(window)
            rows = compare.compare(
                points, cfg, start, self.cooldown, self.size, stats, depth
            )
            return {"rows": rows, "steps": stats}

        return self.remembered(key, make) if points else {"rows": [], "steps": None}

    def saved_config(self):
        try:
            return control.load(self.control_file)
        except ValueError:
            return strategy.DEFAULT

    def compare_view(self, query):
        # ------------------------------------
        # Every strategy replayed on the recorded prices with today's settings (compare.py), next
        # to what the bot really did in the same window. Cached: the answer only changes when a
        # new price or a new setting arrives, and the page asks every few seconds.
        # ------------------------------------

        cfg = self.saved_config()
        points, start, signature = self.replay_window(query)
        closed = self.ledger_data()["closed"]
        key = (
            "compare",
            signature,
            json.dumps(strategy.to_dict(cfg), sort_keys=True),
            len(closed),
            closed[-1]["closed"] if closed else None,
            self.depth_key(self.pool_depth()),
        )

        def make():
            replayed = self.replayed(points, start, signature, cfg)
            actual = [t for t in closed if t["closed"] >= start]
            return {
                "from": start,
                "size": str(self.size),
                "active": cfg.strategy,
                **replayed,
                "actual": {
                    "trades": len(actual),
                    "wins": sum(1 for t in actual if ledger.profit(t) > 0),
                    "profit": str(sum((ledger.profit(t) for t in actual), Decimal(0))),
                },
            }

        return self.remembered(key, make)

    def preview_view(self, query):
        # ------------------------------------
        # What-if for the settings form: the settings being typed (`cfg`, a JSON object, checked
        # like a save but NOT saved) against the saved ones, each strategy replayed on the same
        # prices. Per strategy: the result with the saved settings, with the typed ones, the
        # estimated change, and the typed settings' warnings. Reads only; nothing is written.
        # ------------------------------------

        raw = query.get("cfg", [None])[0]
        if raw is None or len(raw) > MAX_BODY:
            raise QueryError(
                "cfg must be the settings as JSON (at most 10,000 characters)"
            )
        try:
            draft = strategy.parse_config(json.loads(raw))
        except (ValueError, TypeError, OverflowError, RecursionError) as e:
            raise QueryError(f"these settings cannot be used: {e}") from e
        saved = self.saved_config()
        # the buttons are not part of the form: a draft is the saved pause and scan state, so a
        # pause alone never shows up as a change
        draft = replace(draft, paused=saved.paused, scan_request=saved.scan_request)
        points, start, signature = self.replay_window(query)
        before = self.replayed(points, start, signature, saved)
        after = (
            before if draft == saved else self.replayed(points, start, signature, draft)
        )
        rows = []
        for old, new in zip(before["rows"], after["rows"], strict=True):
            was = old["profit"] + old["open"]
            now = new["profit"] + new["open"]
            rows.append(
                {
                    "strategy": new["strategy"],
                    "label": new["label"],
                    "saved": old,
                    "draft": new,
                    "change": now - was,
                    # (a base of floating-point dust would make a huge percentage out of nothing)
                    "change_pct": (now - was) / abs(was) * 100
                    if abs(was) > 1e-9
                    else None,
                }
            )
        return {
            "from": start,
            "size": str(self.size),
            "changed": draft != saved,
            "saved_strategy": saved.strategy,
            "draft_strategy": draft.strategy,
            "steps": after["steps"],
            "rows": rows,
        }

    @staticmethod
    def position_view(held, chain, cfg):
        if held is None:
            return None
        entry = held["entry_price"]
        view = {
            **held,
            "take_profit_price": entry * (1 + cfg.take_profit_pct / 100),
            "stop_loss_price": entry * (1 - cfg.stop_loss_pct / 100),
        }
        cost = Decimal(held["quote_spent"])
        if chain and cost:
            worth = Decimal(held["base"]) * Decimal(chain["price"])
            view["change_pct"] = (chain["price"] / entry - 1) * 100
            view["unrealized"] = str(worth - cost)
            view["unrealized_pct"] = float((worth - cost) / cost * 100)
        return view

    def update_config(self, raw):
        config = strategy.parse_config(
            raw
        )  # ValueError if anything is off: nothing saved
        with self.write_lock:
            # saving the form must not undo a click on a button made since it was loaded
            try:
                current = control.load(self.control_file)
                paused, scan_request = current.paused, current.scan_request
            except ValueError:
                # A damaged file: the bot is still running on its last good settings, which may be
                # paused. Fixing the file with the form must not lift that unseen, so it fails
                # closed (paused; the button resumes). The scan number needs no care: a click
                # always asks above whatever the bot served.
                paused, scan_request = True, 0
            config = replace(config, paused=paused, scan_request=scan_request)
            control.save(self.control_file, config)
        return strategy.to_dict(config)

    COMMANDS: ClassVar[tuple] = ("pause", "resume", "scan")

    def command(self, raw):
        # ------------------------------------
        # The buttons. All they do is write logs/control.json, like the settings form: "pause" and
        # "resume" hold back or allow NEW buys (a sale is never held back), "scan" raises the
        # request number the bot compares with the last one it served, and the bot then reads the
        # pool's layout itself on its next tick. This page makes no RPC call. ValueError if the
        # request is not one of these or the settings file is damaged (it is not overwritten: the
        # settings form is how it gets fixed).
        # ------------------------------------

        if (
            not isinstance(raw, dict)
            or set(raw) != {"command"}
            or raw["command"] not in self.COMMANDS
        ):
            raise ValueError(
                f'send exactly {{"command": one of {list(self.COMMANDS)}}}'
            )
        name = raw["command"]
        with self.write_lock:
            current = control.load(self.control_file)  # ValueError if damaged
            if name == "scan":
                # always above the last request (and so above what the bot served)
                request = max(int(self.clock()), current.scan_request + 1)
                changed = replace(current, scan_request=request)
            else:
                changed = replace(current, paused=name == "pause")
            control.save(self.control_file, changed)
        return {"paused": changed.paused, "scan_request": changed.scan_request}

    def control_view(self, cfg):
        # what the buttons show: the saved pause and scan request, how deep the bot last found the
        # pool, and its last answer to a scan request (from the state file)
        try:
            chain = state.load(self.state_file)
        except ValueError:
            chain = None
        depth = self.pool_depth()
        return {
            "paused": cfg.paused,
            "scan_request": cfg.scan_request,
            "scan": chain["scan"] if chain else None,
            "pool": None
            if depth is None
            else {
                "block": depth["block"],
                "scale": depth["scale"],
                "scanned": "request" in depth,
            },
        }


class Handler(BaseHTTPRequestHandler):
    app = None  # the Dashboard; set per server in serve()
    # a client that stops sending must not hold a thread (and a file descriptor) forever
    timeout = 10
    server_version = "dashboard"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # the page polls every few seconds: a line each would bury the real output

    def reply(self, code, body, content_type="application/json", csp=BASE_CSP):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # the library's own error pages (unsupported method, malformed request) are HTML with a
        # server banner and none of our headers: answer them like everything else
        self.close_connection = True
        self.reply(code, {"error": message or "error"})

    def local_only(self):
        if not LOCAL_HOST.fullmatch(self.headers.get("Host", "")):
            self.reply(403, {"error": "use http://localhost through the SSH tunnel"})
            return False
        return True

    def same_site(self):
        # A page on another site can make the browser GET these routes (an <img> is enough) even
        # though it cannot read the answer; each replay costs CPU, so refuse them like a cross-site
        # POST. Browsers say where a request comes from; a command-line client says nothing.
        origin = self.headers.get("Origin")
        try:
            foreign = bool(origin) and not LOCAL_HOST.fullmatch(urlsplit(origin).netloc)
        except ValueError:
            foreign = True
        if foreign or self.headers.get("Sec-Fetch-Site", "same-origin") not in (
            "same-origin",
            "none",
        ):
            self.reply(403, {"error": "cross-site request refused"})
            return False
        return True

    # read-only data views that take a query string: path -> Dashboard method
    VIEWS: ClassVar[dict[str, str]] = {
        "/api/prices": "prices_view",
        "/api/trades": "trades_view",
        "/api/compare": "compare_view",
        "/api/preview": "preview_view",
    }

    def do_GET(self):
        if not self.local_only():
            return
        parts = urlsplit(self.path)
        if parts.path.startswith("/api/") and not self.same_site():
            return
        try:
            if self.path in ("/", "/index.html"):
                page = PAGE.read_bytes()
                self.reply(200, page, "text/html; charset=utf-8", page_csp(page))
            elif self.path == "/api/status":
                self.reply(200, self.app.snapshot())
            elif parts.path in self.VIEWS:
                query = parse_qs(parts.query)
                self.reply(200, getattr(self.app, self.VIEWS[parts.path])(query))
            else:
                self.reply(404, {"error": "not found"})
        except QueryError as e:
            self.reply(400, {"error": str(e)})
        except Exception:  # noqa: BLE001 - redacted, reported to the page, never a stack dump
            print(redact(traceback.format_exc()), file=sys.stderr)
            self.reply(500, {"error": "internal error, see the dashboard's console"})

    def do_POST(self):
        if not self.local_only():
            return
        if self.path not in ("/api/config", "/api/command"):
            return self.reply(404, {"error": "not found"})
        # A browser lets any web page POST to localhost. These two checks stop that: a
        # cross-site page cannot set a JSON content type without a preflight we never answer,
        # and its Origin is not a local host name.
        origin = self.headers.get("Origin")
        try:
            foreign = bool(origin) and not LOCAL_HOST.fullmatch(urlsplit(origin).netloc)
        except ValueError:  # a malformed Origin is not ours either
            foreign = True
        if foreign:
            return self.reply(403, {"error": "cross-site request refused"})
        if self.headers.get_content_type() != "application/json":
            return self.reply(415, {"error": "send application/json"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.reply(411, {"error": "Content-Length required"})
        if not 0 < length <= MAX_BODY:
            return self.reply(413, {"error": "body too large"})
        try:
            raw = json.loads(self.rfile.read(length))
            if self.path == "/api/command":
                self.reply(200, {"control": self.app.command(raw)})
            else:
                self.reply(200, {"config": self.app.update_config(raw)})
        except (
            ValueError
        ) as e:  # bad JSON and bad settings both end here, with the reason
            self.reply(400, {"error": str(e)})
        except TimeoutError:  # the client announced a body and stopped sending it
            self.reply(408, {"error": "request timed out"})
        except Exception:  # noqa: BLE001
            print(redact(traceback.format_exc()), file=sys.stderr)
            self.reply(500, {"error": "internal error, see the dashboard's console"})


def serve(app, port):
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def cli():
    # usage: python dashboard.py [--port N]
    args = sys.argv[1:]
    port = DEFAULT_PORT
    if (
        len(args) == 2
        and args[0] == "--port"
        and args[1].isdigit()
        and 0 < int(args[1]) < 65536
    ):
        port = int(args[1])
    elif args:
        raise SystemExit("usage: dashboard.py [--port N]")

    load_dotenv()
    dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
    # the same two numbers the bot uses, for the comparison's paper replay only
    app = Dashboard(
        dry_run,
        cooldown=int(os.environ.get("COOLDOWN_SECONDS", "300")),
        size=Decimal(os.environ.get("MAX_TRADE_AMOUNT", "1000")),
        depth_file=v3pool.LIVE_PROFILE_FILE,
    )
    server = serve(app, port)
    print(
        f"Dashboard for the {'DRY RUN' if dry_run else 'LIVE'} bot on http://{HOST}:{port}"
    )
    print(f"From your laptop:  ssh -L {port}:127.0.0.1:{port} <user>@<server>")
    print(f"then open http://localhost:{port}  (Ctrl-C stops it)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    cli()
