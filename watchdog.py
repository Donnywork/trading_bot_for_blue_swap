"""Event-runtime watchdog: alert when the trading runtime needs an operator.

It ONLY watches and alerts. It never restarts, reconciles, clears halt
markers, writes control files or touches the ledger: an uncertain-send halt
requires manual reconciliation by design, and an automated restart of a
halted runtime is forbidden (see AGENTS.md runtime invariants). The cost of
a halt should be minutes of dead time, not hours, because someone is told.

Checks logs/events/state.json every WATCHDOG_SECONDS (default 30):

- health "halted"/"failed"                     CRITICAL  reconcile/restart by hand
- health "awaiting_scan"                       WARNING   entries blocked; click Scan
- state file missing/corrupt/stale updates     CRITICAL  process dead or wedged
  (WATCHDOG_STALE_SECONDS, default 90; the service
   rewrites state.json about once a second)
- anything else ("ready", "scanning", ...)     healthy   "scanning" is transient

Alerts fire on transitions into and out of trouble - deduplicated on a
stable (severity, key) pair, so a slowly worsening stale age stays one
alert - and go to stdout (visible in tmux), to logs/events/watchdog.log,
and, when WATCHDOG_WEBHOOK_URL is set in the process environment, as one
JSON line POSTed to that URL (a Discord/Slack-style webhook). The URL is
never logged. No .env is loaded and no chain access happens: the watchdog
costs nothing to run.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "logs/events/state.json"
LOG = ROOT / "logs/events/watchdog.log"

HEALTHY = ("ready", "scanning")
CRITICAL = ("halted", "failed")


def config(env=None):
    env = os.environ if env is None else env

    def seconds(name, default, lo, hi):
        try:
            value = int(env.get(name, str(default)))
        except ValueError:
            raise SystemExit(f"{name} must be an integer in seconds") from None
        if not lo <= value <= hi:
            raise SystemExit(f"{name} must be {lo}..{hi} seconds")
        return value

    return {
        "interval": seconds("WATCHDOG_SECONDS", 30, 5, 600),
        "stale": seconds("WATCHDOG_STALE_SECONDS", 90, 15, 3600),
        "webhook": str(env.get("WATCHDOG_WEBHOOK_URL", "")).strip(),
    }


def read_state(path, now):
    # A missing or corrupt state file is itself the alarm: the runtime owns
    # that file and rewrites it every second.
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, f"state unreadable: {type(exc).__name__}"
    age = now - value.get("updated", 0)
    return value, None if age >= 0 else "state timestamp is in the future"


def classify(state, error, now, stale_after):
    # The key is the stable identity of the condition; the text may carry
    # details (like a worsening age) without re-triggering alerts.
    if error:
        return "critical", "error", error
    health = str(state.get("health", ""))
    age = now - state.get("updated", 0)
    if health in CRITICAL:
        message = str(state.get("message", "")).strip() or "no message"
        return "critical", health, f"health={health}: {message}"
    if age > stale_after:
        return (
            "critical",
            "stale",
            f"state is {int(age)}s stale (process dead or wedged)",
        )
    if health == "awaiting_scan":
        return (
            "warning",
            "awaiting_scan",
            "awaiting_scan: entries blocked, request a liquidity scan",
        )
    if health in HEALTHY:
        return "ok", health, f"health={health}"
    return "warning", "unknown", f"health={health!r} (unknown)"


def post_webhook(url, body):
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def notify(severity, text, now, settings, emit, post):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
    emit(f"{stamp} UTC [{severity}] {text}")
    if settings["webhook"]:
        # One fixed-shape JSON line; the URL itself is never part of any log.
        body = json.dumps(
            {"content": f"[trading-bot watchdog] [{severity}] {text}"}
        ).encode()
        try:
            post(settings["webhook"], body)
        except OSError:
            # Delivery failure must never take the watchdog down; the log
            # line above is the durable record.
            emit(f"{stamp} UTC [info] webhook delivery failed")


def run(settings, now, sleep, read_state=read_state, emit=None, post=post_webhook):
    emit = emit or (lambda line: print(line, flush=True))
    previous = None
    while True:
        state, error = read_state(STATE, now())
        severity, key, text = classify(state, error, now(), settings["stale"])
        if (severity, key) != previous:
            if severity == "ok":
                if previous is None:
                    emit(f"healthy ({text})")
                else:
                    notify("recovered", text, now(), settings, emit, post)
            else:
                notify(severity, text, now(), settings, emit, post)
            previous = (severity, key)
        sleep(settings["interval"])


def main():
    settings = config()
    LOG.parent.mkdir(parents=True, exist_ok=True)

    def emit(line):
        print(line, flush=True)
        try:
            with LOG.open("a") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    try:
        run(settings, time.time, time.sleep, emit=emit)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
