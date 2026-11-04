import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

import bot_contract
import control
import history
import ledger
import status
import strategy
from main import PRELOAD_MAX_AGE, READ_ERRORS, redact

# Loopback only, on purpose: the page can change what the bot does, and it has no login. Reach it
# from your laptop through an SSH tunnel (see docs/DEPLOY.md), never by opening a port.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PAGE = Path(__file__).parent / "dashboard.html"
CHAIN_CACHE_SECONDS = (
    15  # the page refreshes often; the RPC (and its quota) is asked far less
)
MAX_BODY = 10_000
CHART_POINTS = 720  # 6 hours at one sample per 30s
RECENT_TRADES = 20
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


class Dashboard:
    # ------------------------------------
    # Everything the page needs, kept apart from HTTP so it can be tested. Read-only except
    # update_config(), which writes the settings file the bot re-reads every tick.
    # ------------------------------------

    def __init__(
        self,
        dry_run,
        read_chain,
        control_file=control.CONTROL_FILE,
        history_file=history.HISTORY_FILE,
        ledger_file=None,
        clock=time.time,
    ):
        self.dry_run, self.read_chain, self.clock = dry_run, read_chain, clock
        self.control_file, self.history_file = control_file, history_file
        self.ledger_file = ledger_file or ledger.file_for(dry_run)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()  # one settings write at a time
        self.chain_data, self.chain_error, self.chain_at = None, None, None

    def chain(self):
        with self.lock:
            now = self.clock()
            if self.chain_at is None or now - self.chain_at >= CHAIN_CACHE_SECONDS:
                self.chain_at = now
                try:
                    self.chain_data, self.chain_error = self.read_chain(), None
                except (
                    *READ_ERRORS,
                    SystemExit,
                ) as e:  # SystemExit: load_bot found no contract
                    self.chain_data, self.chain_error = None, redact(str(e))
            return self.chain_data, self.chain_error

    def snapshot(self):
        now = self.clock()
        chain, chain_error = self.chain()

        problems = []
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
        if chain_error:
            problems.append(f"could not read the chain: {chain_error}")

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
            "schema": strategy.schema(),
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
            control.save(self.control_file, config)
        return strategy.to_dict(config)


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

    def do_GET(self):
        if not self.local_only():
            return
        try:
            if self.path in ("/", "/index.html"):
                page = PAGE.read_bytes()
                self.reply(200, page, "text/html; charset=utf-8", page_csp(page))
            elif self.path == "/api/status":
                self.reply(200, self.app.snapshot())
            else:
                self.reply(404, {"error": "not found"})
        except Exception:  # noqa: BLE001 - redacted, reported to the page, never a stack dump
            print(redact(traceback.format_exc()), file=sys.stderr)
            self.reply(500, {"error": "internal error, see the dashboard's console"})

    def do_POST(self):
        if not self.local_only():
            return
        if self.path != "/api/config":
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


def chain_reader():
    # one connection, rebuilt after a read error
    held = {}

    def read():
        try:
            if "w3" not in held:
                held["w3"] = bot_contract.connect_read_only()
            return status.read_chain(held["w3"])
        except (*READ_ERRORS, SystemExit):
            held.clear()
            raise

    return read


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
    server = serve(Dashboard(dry_run, chain_reader()), port)
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
