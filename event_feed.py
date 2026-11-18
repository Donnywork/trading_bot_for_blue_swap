"""Bounded Alchemy feed. Any disconnect or overflow invalidates pending work."""

import json
import logging
import os
import queue
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect

from runtime import ConfigurationError, error_summary


def endpoint(env=None):
    env = os.environ if env is None else env
    for value in [
        env.get("ALCHEMY_WS_URL", ""),
        env.get("RPC_URL", ""),
        *env.get("RPC_FALLBACK_URLS", "").split(","),
    ]:
        try:
            p = urlsplit(value.strip())
        except ValueError:
            continue
        if (
            p.scheme in ("https", "wss")
            and p.hostname == "eth-sepolia.g.alchemy.com"
            and p.path.startswith("/v2/")
            and p.path[4:]
            and not p.username
            and not p.password
        ):
            return urlunsplit(("wss", p.netloc, p.path, p.query, ""))
    raise ConfigurationError(
        "Configure an Alchemy Sepolia endpoint using ALCHEMY_WS_URL, RPC_URL or RPC_FALLBACK_URLS"
    )


class Feed:
    def __init__(self, uri, signers, stop, capacity=512, metrics=None):
        self.uri, self.signers, self.stop = uri, sorted(set(signers)), stop
        self.queue = queue.Queue(capacity)
        self.ready = threading.Event()
        self.gap = threading.Event()
        self.gap.set()
        self.generation = 0
        self.last_error = None
        self.metrics = metrics
        self.activity_lock = threading.Lock()
        self.activity = {
            kind: {"count": 0, "last_received": None}
            for kind in ("head", "pending", "mined")
        }

    def status(self):
        with self.activity_lock:
            return {
                "activity": {
                    kind: dict(value) for kind, value in self.activity.items()
                },
                "queued": self.queue.qsize(),
            }

    def send(self, ws, message):
        if self.metrics:
            self.metrics.attempt(websocket=True)
        ws.send(message)

    def put(self, event, received=None):
        received = time.time() if received is None else received
        with self.activity_lock:
            if event["kind"] in self.activity:
                row = self.activity[event["kind"]]
                row["count"] += 1
                row["last_received"] = received
        try:
            self.queue.put_nowait(
                event
                | {
                    "received": received,
                    "generation": self.generation,
                }
            )
        except queue.Full:
            self.ready.clear()
            self.gap.set()
            raise RuntimeError("feed overflow") from None

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="event-feed").start()

    def run(self):
        quiet = logging.getLogger("event_transport")
        quiet.disabled = True
        while not self.stop.is_set():
            self.generation += 1
            self.ready.clear()
            self.gap.set()
            # A new connection invalidates all queued observations from the old generation.
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            try:
                with connect(
                    self.uri,
                    open_timeout=15,
                    close_timeout=2,
                    max_size=2**20,
                    logger=quiet,
                ) as ws:
                    self.send(
                        ws,
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "eth_chainId",
                                "params": [],
                            }
                        ),
                    )
                    reply = json.loads(ws.recv(timeout=15))
                    if reply.get("id") != 1 or reply.get("result") != "0xaa36a7":
                        raise ValueError("feed chain mismatch")
                    requests = [
                        (
                            "pending",
                            [
                                "alchemy_pendingTransactions",
                                {"fromAddress": self.signers, "hashesOnly": False},
                            ],
                        ),
                        (
                            "mined",
                            [
                                "alchemy_minedTransactions",
                                {
                                    "addresses": [{"from": a} for a in self.signers],
                                    "includeRemoved": True,
                                    "hashesOnly": False,
                                },
                            ],
                        ),
                        ("head", ["newHeads"]),
                    ]
                    ids, deferred = {}, []
                    for ident, (kind, params) in enumerate(requests, 2):
                        self.send(
                            ws,
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": ident,
                                    "method": "eth_subscribe",
                                    "params": params,
                                }
                            ),
                        )
                        deadline = time.monotonic() + 15
                        while True:
                            reply = json.loads(
                                ws.recv(timeout=max(0.01, deadline - time.monotonic()))
                            )
                            if reply.get("id") == ident:
                                break
                            deferred.append((reply, time.time()))
                            if len(deferred) > 512 or time.monotonic() >= deadline:
                                raise TimeoutError("subscription backlog")
                        if not isinstance(reply.get("result"), str):
                            raise TypeError("subscription rejected")
                        ids[reply["result"]] = kind
                    self.ready.set()
                    self.last_error = None
                    last_message = time.monotonic()
                    while not self.stop.is_set():
                        try:
                            if deferred:
                                message, received = deferred.pop(0)
                            else:
                                message = json.loads(ws.recv(timeout=1))
                                received = time.time()
                        except TimeoutError:
                            if time.monotonic() - last_message > 60:
                                raise ConnectionError(
                                    "feed heartbeat missing"
                                ) from None
                            continue
                        last_message = time.monotonic()
                        params = message.get("params", {})
                        kind = ids.get(params.get("subscription"))
                        if kind:
                            result = params["result"]
                            event = {"kind": kind}
                            if kind == "head":
                                event["header"] = result
                            else:
                                event["transaction"] = (
                                    result
                                    if kind == "pending"
                                    else result["transaction"]
                                )
                                if kind == "mined":
                                    event["removed"] = bool(result.get("removed"))
                            self.put(event, received)
            except Exception as exc:  # noqa: BLE001 — transport boundary suppresses provider bodies
                self.last_error = error_summary(exc)
                self.ready.clear()
                self.gap.set()
                self.stop.wait(5)
            finally:
                self.ready.clear()
                self.gap.set()
