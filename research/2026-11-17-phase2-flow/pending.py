"""Read-only Alchemy Sepolia pending/mined observation. Never signs or broadcasts.

Uses existing PROCESS environment RPC_URL/RPC_FALLBACK_URLS or ALCHEMY_WS_URL.
Never reads .env. Credentials never enter output.
"""

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect

OUT = Path(__file__).parent
TARGET = "0x6011139d5f16689bde1d245c6247b088f9582026"
HOST = "eth-sepolia.g.alchemy.com"


def endpoint():
    values = dict(os.environ)
    candidates = [values.get("ALCHEMY_WS_URL", ""), values.get("RPC_URL", "")]
    candidates.extend(values.get("RPC_FALLBACK_URLS", "").split(","))
    for value in candidates:
        try:
            parsed = urlsplit(value.strip())
            if (
                parsed.scheme in ("https", "wss")
                and parsed.hostname == HOST
                and parsed.path.startswith("/v2/")
                and parsed.path[4:]
                and not parsed.username
                and not parsed.password
            ):
                return urlunsplit(("wss", parsed.netloc, parsed.path, parsed.query, ""))
        except ValueError:
            continue
    return None


def addresses():
    path = OUT / "actors.json"
    actors = json.loads(path.read_text()) if path.exists() else []
    # Track every observed swap signer; frequency isn't sufficient proof of automation.
    return sorted({TARGET, *(a["address"].lower() for a in actors)})


def subscriptions(signers):
    return [
        (
            "pending",
            [
                "alchemy_pendingTransactions",
                {"fromAddress": signers, "hashesOnly": False},
            ],
        ),
        (
            "mined",
            [
                "alchemy_minedTransactions",
                {
                    "addresses": [{"from": a} for a in signers],
                    "includeRemoved": True,
                    "hashesOnly": False,
                },
            ],
        ),
    ]


def observe(state, kind, result, now):
    tx = result if kind == "pending" else result["transaction"]
    tx_hash = tx["hash"]
    record = {"kind": kind, "hash": tx_hash, "transaction": tx}
    if kind == "pending":
        # A non-null block number isn't evidence of pre-inclusion visibility.
        eligible = tx.get("blockNumber") is None and tx_hash not in state["mined"]
        record["pending_fields"] = tx.get("blockNumber") is None
        if eligible:
            state["pending"].setdefault(tx_hash, now)
        nonce_key = (tx.get("from", "").lower(), tx.get("nonce"))
        prior = state["nonces"].get(nonce_key)
        if prior and prior != tx_hash:
            record["same_nonce_previously_seen_hash"] = prior
        state["nonces"][nonce_key] = tx_hash
        state["pending_notifications"] += 1
    else:
        removed = bool(result.get("removed"))
        record["removed"] = removed
        if not removed:
            state["mined"].add(tx_hash)
            first = state["pending"].get(tx_hash)
            record["seen_pending_before_mined_notification"] = (
                first is not None and first < now
            )
            if record["seen_pending_before_mined_notification"]:
                record["notification_lead_seconds"] = now - first
            state["mined_notifications"] += 1
    return record


def run(seconds, probe=False):
    # Suppress library messages: exception bodies/handshake URLs can include credentials.
    quiet_logger = logging.getLogger("pending_observer_transport")
    quiet_logger.disabled = True
    status = {
        "provider": "Alchemy",
        "chain_id": 11155111,
        "addresses": addresses(),
        "started_utc": datetime.now(UTC).isoformat(),
        "subscribed": False,
    }
    status_path = OUT / "pending_status.json"

    def update(**changes):
        status.update(changes)
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, indent=2) + "\n")
        temporary.replace(status_path)

    uri = endpoint()
    if uri is None:
        update(state="needs_connection_configuration")
        print(
            "No Alchemy Sepolia endpoint in the process environment. This observer never reads .env.",
            flush=True,
        )
        return 2
    state = {
        "pending": {},
        "mined": set(),
        "nonces": {},
        "pending_notifications": 0,
        "mined_notifications": 0,
    }
    output = OUT / (
        "pending_observations-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        + ".jsonl"
    )
    start = time.monotonic()
    update(state="connecting", output_file=output.name)
    try:
        with connect(
            uri, open_timeout=15, close_timeout=5, max_size=2**20, logger=quiet_logger
        ) as ws:
            ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
                )
            )
            reply = json.loads(ws.recv(timeout=15))
            if reply.get("id") != 1 or reply.get("result") != hex(11155111):
                update(state="chain_check_failed")
                return 3
            ids, deferred = {}, []
            for request_id, (kind, params) in enumerate(
                subscriptions(status["addresses"]), 2
            ):
                ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "eth_subscribe",
                            "params": params,
                        }
                    )
                )
                deadline = time.monotonic() + 15
                while True:
                    reply = json.loads(
                        ws.recv(timeout=max(0.01, deadline - time.monotonic()))
                    )
                    if reply.get("id") == request_id:
                        break
                    deferred.append(
                        (reply, time.monotonic(), datetime.now(UTC).isoformat())
                    )
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                if "error" in reply:
                    update(
                        state="subscription_rejected",
                        rejected_subscription=kind,
                        rpc_error_code=reply["error"].get("code"),
                    )
                    print(
                        f"Alchemy rejected the {kind} subscription; credential-bearing error text suppressed.",
                        flush=True,
                    )
                    return 4
                ids[reply["result"]] = kind
            update(state="subscribed", subscribed=True)
            print(
                f"Alchemy Sepolia pending + mined subscriptions active for {len(status['addresses'])} swap signers.",
                flush=True,
            )
            if probe:
                update(state="probe_complete")
                return 0
            with output.open("a") as stream:
                while time.monotonic() - start < seconds:
                    if deferred:
                        message, now, received_utc = deferred.pop(0)
                    else:
                        try:
                            message = json.loads(
                                ws.recv(
                                    timeout=min(
                                        30,
                                        max(0.01, seconds - (time.monotonic() - start)),
                                    )
                                )
                            )
                            now, received_utc = (
                                time.monotonic(),
                                datetime.now(UTC).isoformat(),
                            )
                        except TimeoutError:
                            continue
                    params = message.get("params", {})
                    kind = ids.get(params.get("subscription"))
                    if kind is None:
                        continue
                    record = observe(state, kind, params["result"], now)
                    record.update(
                        {"received_utc": received_utc, "elapsed_seconds": now - start}
                    )
                    stream.write(json.dumps(record) + "\n")
                    stream.flush()
                    update(
                        pending_notifications=state["pending_notifications"],
                        mined_notifications=state["mined_notifications"],
                    )
                    print(f"{kind}: {record['hash']}", flush=True)
        update(state="complete", elapsed_seconds=time.monotonic() - start)
        return 0
    except KeyboardInterrupt:
        update(state="stopped", elapsed_seconds=time.monotonic() - start)
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI boundary must never print credential-bearing errors
        # A terminated connection leaves a visible observation gap; never silently claim coverage.
        update(
            state="connection_failed",
            exception_type=type(exc).__name__,
            elapsed_seconds=time.monotonic() - start,
        )
        print(
            f"Alchemy observation stopped ({type(exc).__name__}); connection details suppressed.",
            flush=True,
        )
        return 5


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument(
        "--probe", action="store_true", help="check subscriptions and disconnect"
    )
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86400:
        parser.error("seconds must be 1..86400")
    raise SystemExit(run(args.seconds, args.probe))
