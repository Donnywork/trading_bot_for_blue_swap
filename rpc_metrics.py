"""Per-process aggregates. Neither request contents nor endpoints enter published state."""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

PURPOSES = (
    "Startup checks",
    "Snapshot catch-up",
    "Manual liquidity scans",
    "Feed setup",
    "Receipts and nonce checks",
    "Trade quotes and simulation",
    "Live execution",
    "Portfolio and chart diagnostics",
    "Fee evidence refresh",
)
STARTUP, SNAPSHOT, SCAN, FEED, RECEIPTS, QUOTES, LIVE, DIAGNOSTICS, FEES = PURPOSES
_current = ContextVar("rpc_purpose", default=STARTUP)


@contextmanager
def purpose(label):
    token = _current.set(label)
    try:
        yield
    finally:
        _current.reset(token)


class RpcMetrics:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.started = clock()
        self.counts = dict.fromkeys(PURPOSES, 0)
        self.http = 0
        self.lock = threading.Lock()

    def attempt(self, items=1, *, websocket=False, method=None):
        label = FEED if websocket else _current.get()
        if method in ("eth_getTransactionReceipt", "eth_getTransactionCount"):
            label = RECEIPTS
        with self.lock:
            self.counts[label] += items
            if not websocket:
                self.http += 1

    def snapshot(self):
        with self.lock:
            return {
                "started": self.started,
                "updated": self.clock(),
                "total": sum(self.counts.values()),
                "http_attempts": self.http,
                "by_purpose": dict(self.counts),
            }
