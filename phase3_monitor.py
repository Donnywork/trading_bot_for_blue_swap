"""Read confirmed Sepolia pool, token and credit activity for every Phase 3 actor."""

import argparse
import fcntl
import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from traceback import extract_tb

import requests

from ledger import atomic_write
from phase3_chain import PUBLIC_RPC, WAD, Chain
from phase3_notices import actor, digest, notices
from phase_markets import PHASE3, PHASE3_START
from runtime import error_kind, rejected_log_range
from watchdog import post_webhook

ROOT = Path(__file__).resolve().parent
CONTROL = "https://fin564.vercel.app/api/competition/control"


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS anchors (block INTEGER PRIMARY KEY, hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (block INTEGER, idx INTEGER, kind TEXT, row TEXT, PRIMARY KEY(block, idx));
            CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
        """)

    def tip(self):
        return self.db.execute(
            "SELECT block,hash FROM anchors ORDER BY block DESC LIMIT 1"
        ).fetchone()

    def rewind(self, block):
        with self.db:
            self.db.execute("DELETE FROM events WHERE block >= ?", (block,))
            self.db.execute("DELETE FROM anchors WHERE block >= ?", (block,))

    def commit(self, block, block_hash, rows):
        with self.db:
            self.db.executemany(
                "INSERT INTO events VALUES(?,?,?,?)",
                ((r["block"], r["index"], r["event"], json.dumps(r)) for r in rows),
            )
            self.db.execute("INSERT INTO anchors VALUES(?,?)", (block, block_hash))

    def depth(self, tick, active):
        net = Counter()
        for (text,) in self.db.execute(
            "SELECT row FROM events WHERE kind IN ('Mint','Burn')"
        ):
            row = json.loads(text)
            if row["contract"] != "pool":
                continue
            args = row["args"]
            amount = args["amount"] if row["event"] == "Mint" else -args["amount"]
            net[args["tickLower"]] += amount
            net[args["tickUpper"]] -= amount
        if sum(net.values()) or sum(v for t, v in net.items() if t <= tick) != active:
            raise ValueError("incomplete pool liquidity history")
        return {k: v for k, v in sorted(net.items()) if v}


def sync(chain, journal, target):
    tip = journal.tip()
    rewound = False
    if tip and tip[0] > target:
        raise ValueError("head behind checkpoint")
    while tip and chain.header(tip[0])[1] != tip[1]:
        previous = journal.db.execute(
            "SELECT MAX(block) FROM anchors WHERE block < ?", (tip[0],)
        ).fetchone()[0]
        journal.rewind((previous + 1) if previous is not None else PHASE3_START)
        tip = journal.tip()
        rewound = True
    start = tip[0] + 1 if tip else PHASE3_START
    new = []
    chunk = 1000
    while start <= target:
        end = min(start + chunk - 1, target)
        block_hash = chain.header(end)[1]
        try:
            rows = chain.events_in(start, end)
        except Exception as exc:
            if end > start and rejected_log_range(exc):
                chunk = max(1, (end - start + 1) // 2)
                continue
            raise
        if chain.header(end)[1] != block_hash or (
            tip and chain.header(tip[0])[1] != tip[1]
        ):
            raise ValueError("chain moved during log query")
        journal.commit(end, block_hash, rows)
        new.extend(rows)
        tip = end, block_hash
        start = end + 1
    return new, rewound


def control():
    reply = requests.get(CONTROL, timeout=8)
    reply.raise_for_status()
    doc = reply.json()
    phase = doc["phase"]
    return {
        k: doc[k]
        for k in ("controlVersion", "status", "participantEnabled", "tradingEnabled")
    } | {
        "phase": phase["id"],
        "tradeBps": phase["bot"]["tradeBps"],
        "intervalSeconds": phase["bot"]["intervalSeconds"],
        "adjustmentNote": phase.get("adjustmentNote"),
        "pricingPool": phase["pricingPool"],
        "creditFacility": phase["creditFacility"],
    }


def alert(text):
    print(text, flush=True)
    url = os.environ.get("WATCHDOG_WEBHOOK_URL")
    if url:
        try:
            status = post_webhook(
                url,
                json.dumps(
                    {
                        "content": f"[FIN564 Phase 3] {text}",
                        "allowed_mentions": {"parse": []},
                    }
                ).encode(),
            )
            print(f"Discord HTTP {status}", flush=True)
        except (OSError, ValueError) as exc:
            print(
                f"Discord delivery failed ({getattr(exc, 'code', None) or type(exc).__name__})",
                flush=True,
            )


def risk_levels(health):
    return {
        a: ("liquidatable" if h < WAD else "near liquidation")
        for a, h in health.items()
        if h < 11 * WAD // 10
    }


def run(directory, once=False):
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "monitor.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    chain = Chain(os.getenv("PHASE3_RPC_URL") or PUBLIC_RPC)
    journal = Journal(directory / "events.sqlite3")
    state = {"phase": 3, "pool": PHASE3.pools[0][1], "credit": PHASE3.credit}
    path = directory / "state.json"
    validated = ready = False
    last_site = last_snapshot = 0
    previous_error = None
    while True:
        try:
            now = time.time()
            head, _, timestamp = chain.header("latest")
            if not -30 <= now - timestamp <= 180:
                raise ValueError("stale Sepolia head")
            target = head - 2
            if not validated:
                chain.validate(target)
                validated = True
            rows, reorg = sync(chain, journal, target)
            state.update(head=head, indexed_block=target, indexed_hash=journal.tip()[1])
            if ready and rows:
                message = digest(notices(rows))
                if message:
                    alert(message)
            if reorg:
                alert(f"Rebuilt reorganized Phase 3 history through block {target}")
                state.pop("market", None)
            if (
                not ready
                or reorg
                or now - last_snapshot >= 60
                or any(r["contract"] in ("credit", "pool") for r in rows)
            ):
                snap = chain.snapshot(target)
                snap["liquidity_net_by_tick"] = journal.depth(
                    snap["tick"], snap["liquidity"]
                )
                if chain.header(target)[1] != state["indexed_hash"]:
                    raise ValueError("market snapshot reorganized")
                before = risk_levels(
                    state.get("market", {}).get("borrower_health_wad", {})
                )
                for addr, level in risk_levels(snap["borrower_health_wad"]).items():
                    if before.get(addr) != level:
                        alert(
                            f"{actor(addr)} is {level}. Borrower health {snap['borrower_health_wad'][addr] / WAD:.4f} (liquidation threshold 1.00)."
                        )
                state["market"] = snap
                last_snapshot = now
            if now - last_site >= 300:
                last_site = now
                try:
                    site = control()
                except (OSError, KeyError, ValueError, TypeError) as exc:
                    state["site_error"] = error_kind(exc)
                else:
                    if state.get("site") and site != state["site"]:
                        alert(
                            f"Competition control changed to version {site['controlVersion']}, phase {site['phase']}"
                        )
                    state.update(site=site, site_checked_at=int(now))
                    state.pop("site_error", None)
            state.update(health="ready", updated=int(time.time()))
            state.pop("error", None)
            atomic_write(path, json.dumps(state, indent=2))
            if not ready or previous_error:
                alert(
                    f"Monitoring block {target}, spot {state['market']['spot']:.2f} f564/fETH, {len(state['market']['borrower_health_wad'])} borrower records"
                )
            ready = True
            previous_error = None
            if once:
                return 0
        except Exception as exc:  # noqa: BLE001 — keep serving, record diagnostic without provider text
            # Exception messages can contain endpoint credentials. Only type and source location are public.
            frame = extract_tb(exc.__traceback__)[-1]
            reason = f"{error_kind(exc)} at {Path(frame.filename).name}:{frame.lineno}"
            state.update(health="degraded", error=reason, updated=int(time.time()))
            atomic_write(path, json.dumps(state, indent=2))
            if reason != previous_error:
                alert(f"Monitor degraded ({reason})")
            previous_error = reason
            validated = False
            last_snapshot = 0
            if once:
                return 1
        time.sleep(15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--directory", type=Path, default=ROOT / "logs/phase3")
    args = parser.parse_args()
    raise SystemExit(run(args.directory, args.once))
