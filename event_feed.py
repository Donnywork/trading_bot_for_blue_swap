"""Bounded Alchemy feed. Any disconnect or overflow invalidates pending work."""

import itertools
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


def race_endpoint(env=None):
    """Optional second pending-tx source raced against the primary feed."""
    env = os.environ if env is None else env
    value = env.get("EVENT_PENDING_RACE_URL", "").strip()
    if not value:
        return None
    try:
        p = urlsplit(value)
    except ValueError:
        p = None
    if not p or p.scheme not in ("ws", "wss") or not p.hostname:
        raise ConfigurationError(
            "EVENT_PENDING_RACE_URL must be a ws:// or wss:// endpoint (empty disables it)"
        )
    return value


class EventQueue(queue.Queue):
    """Bounded FIFO with invalidations selected atomically before ordinary work."""

    def _get(self):
        # Queue.get holds the mutex: producers cannot refill between selection
        # and removal. Never drain/requeue; that can deadlock a bounded queue.
        for index, event in enumerate(self.queue):
            if event.get("removed") or event.get("kind") == "reorg":
                del self.queue[index]
                return event
        return self.queue.popleft()

    def has_removal(self):
        with self.mutex:
            return any(
                event.get("removed") or event.get("kind") == "reorg"
                for event in self.queue
            )


# A pending hash is "claimed" by whichever source sees it first; entries
# expire so replacements and long-lived pendings never grow the table.
CLAIM_SECONDS = 120
CLAIM_CAPACITY = 4096


class PendingRacer:
    """Second, independent pending source raced against the primary feed.

    The 2026-11-24 feed-race measurement (research/2026-11-24-feed-race)
    showed a public node delivering the same pendings a median 456 ms
    before the Alchemy subscription pushed them — earlier sight is worth
    more than every internal optimization combined. This thread turns that
    into earlier first-seen stamps WITHOUT touching the primary's
    semantics: first arrival wins per hash (a claim table shared with the
    primary), and a racer failure of any kind never clears ready, sets gap
    or bumps generation — it is additive redundancy, not a primary. Events
    it delivers carry source="racer" and the primary's generation, so the
    service's recovery checks discard them exactly like its own.
    """

    def __init__(self, feed, uri):
        self.feed, self.uri = feed, uri
        self.connected = threading.Event()
        self.error = ""
        self.stats = {"delivered": 0, "duplicates": 0, "filtered": 0, "overflow": 0}

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="pending-racer").start()

    def deliver(self, tx, received, received_mono):
        feed = self.feed
        if (tx.get("from") or "").lower() not in feed.signer_set:
            self.stats["filtered"] += 1
            return
        if not feed.claim_pending(tx["hash"]):
            self.stats["duplicates"] += 1
            return
        event = {"kind": "pending", "transaction": tx, "source": "racer"}
        try:
            feed.queue.put_nowait(
                event
                | {
                    "received": received,
                    "received_mono": received_mono,
                    "generation": feed.generation,
                }
            )
        except queue.Full:
            # Redundancy must not invalidate work: drop and count, and
            # release the claim so the primary's copy can still deliver
            # once the queue drains.
            feed.release_pending(tx["hash"])
            self.stats["overflow"] += 1
            return
        self.stats["delivered"] += 1

    def _await(self, ws, want_id):
        """Receive until the reply with want_id lands; defer notifications.

        A notification racing ahead of the ACK keeps its arrival stamps —
        it is returned for normal dispatch instead of being swallowed.
        """
        deferred = []
        while True:
            message = json.loads(ws.recv(timeout=15))
            if message.get("id") == want_id:
                return message, deferred
            deferred.append((message, time.time(), time.monotonic()))

    def _dispatch(self, message, received, received_mono, ws, fetches, ids, full):
        if message.get("id") in fetches:
            _, at, at_mono = fetches.pop(message["id"])
            tx = message.get("result")
            if isinstance(tx, dict):
                self.deliver(tx, at, at_mono)
            return
        item = (message.get("params") or {}).get("result")
        if item is None:
            return
        if isinstance(item, dict):
            self.deliver(item, received, received_mono)
            return
        # Hash-only endpoint: the stamp is the NOTIFICATION arrival; the
        # sender fetch runs in flight so the stream never blocks on it.
        if not full:
            request_id = next(ids)
            fetches[request_id] = (item, received, received_mono)
            self.feed.send(
                ws,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "eth_getTransactionByHash",
                        "params": [item],
                    }
                ),
            )

    def run(self):
        quiet = logging.getLogger("event_transport_racer")
        quiet.disabled = True
        while not self.feed.stop.is_set():
            try:
                with connect(
                    self.uri,
                    open_timeout=15,
                    close_timeout=2,
                    max_size=2**20,
                    logger=quiet,
                ) as ws:
                    # A wrong-network endpoint must never feed the pipeline.
                    self.feed.send(
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
                    reply, deferred = self._await(ws, 1)
                    if reply.get("id") != 1 or reply.get("result") != "0xaa36a7":
                        raise ValueError("race feed chain mismatch")
                    full = None
                    for params, flavour in (
                        (["newPendingTransactions", True], True),
                        (["newPendingTransactions"], False),
                    ):
                        self.feed.send(
                            ws,
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": 2,
                                    "method": "eth_subscribe",
                                    "params": params,
                                }
                            ),
                        )
                        reply, more = self._await(ws, 2)
                        deferred.extend(more)
                        if isinstance(reply.get("result"), str):
                            full = flavour
                            break
                    if full is None:
                        raise TypeError("race subscription rejected")
                    self.error = ""
                    self.connected.set()
                    fetches, ids = {}, itertools.count(3)
                    for message, received, received_mono in deferred:
                        self._dispatch(
                            message, received, received_mono, ws, fetches, ids, full
                        )
                    while not self.feed.stop.is_set():
                        try:
                            message = json.loads(ws.recv(timeout=1))
                            received, received_mono = time.time(), time.monotonic()
                        except TimeoutError:
                            continue
                        self._dispatch(
                            message, received, received_mono, ws, fetches, ids, full
                        )
            except Exception as exc:  # noqa: BLE001 — the racer must never take the feed down
                self.error = error_summary(exc)
                self.feed.stop.wait(5)
            finally:
                self.connected.clear()


class Feed:
    def __init__(self, uri, signers, stop, capacity=512, metrics=None, race_uri=None):
        self.uri, self.signers, self.stop = uri, sorted(set(signers)), stop
        self.signer_set = {address.lower() for address in self.signers}
        self.queue = EventQueue(capacity)
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
        self.claimed = {}
        self.claim_lock = threading.Lock()
        self.racer = PendingRacer(self, race_uri) if race_uri else None

    def status(self):
        with self.activity_lock:
            result = {
                "activity": {
                    kind: dict(value) for kind, value in self.activity.items()
                },
                "queued": self.queue.qsize(),
            }
        if self.racer is not None:
            # Counts and booleans only: the racer's endpoint URL never
            # reaches status output.
            result["racer"] = {
                "connected": self.racer.connected.is_set(),
                **self.racer.stats,
                "error": self.racer.error,
            }
        return result

    def send(self, ws, message):
        if self.metrics:
            self.metrics.attempt(websocket=True)
        ws.send(message)

    def put(self, event, received=None, received_mono=None):
        received = time.time() if received is None else received
        received_mono = time.monotonic() if received_mono is None else received_mono
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
                    "received_mono": received_mono,
                    "generation": self.generation,
                }
            )
        except queue.Full:
            self.ready.clear()
            self.gap.set()
            raise RuntimeError("feed overflow") from None

    def claim_pending(self, tx_hash):
        """First arrival wins across sources; bounded by time and size."""
        now = time.monotonic()
        with self.claim_lock:
            if len(self.claimed) > CLAIM_CAPACITY:
                for hash_value, at in list(self.claimed.items()):
                    if now - at > CLAIM_SECONDS:
                        del self.claimed[hash_value]
                if len(self.claimed) > CLAIM_CAPACITY:
                    # Everything is fresh but the table is full (far beyond
                    # realistic pending volume): evict the oldest quarter.
                    oldest = sorted(self.claimed.items(), key=lambda kv: kv[1])
                    for hash_value, _ in oldest[: len(oldest) // 4]:
                        del self.claimed[hash_value]
            if tx_hash in self.claimed:
                return False
            self.claimed[tx_hash] = now
            return True

    def release_pending(self, tx_hash):
        with self.claim_lock:
            self.claimed.pop(tx_hash, None)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="event-feed").start()
        if self.racer is not None:
            self.racer.start()

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
                            deferred.append((reply, time.time(), time.monotonic()))
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
                                message, received, received_mono = deferred.pop(0)
                            else:
                                message = json.loads(ws.recv(timeout=1))
                                received = time.time()
                                received_mono = time.monotonic()
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
                            if kind == "pending":
                                # The racer may have delivered this hash
                                # already: first arrival wins, and the
                                # claim table is shared so this drops out.
                                if not self.claim_pending(event["transaction"]["hash"]):
                                    continue
                                event["source"] = "primary"
                            self.put(event, received, received_mono)
            except Exception as exc:  # noqa: BLE001 — transport boundary suppresses provider bodies
                self.last_error = error_summary(exc)
                self.ready.clear()
                self.gap.set()
                self.stop.wait(5)
            finally:
                self.ready.clear()
                self.gap.set()
