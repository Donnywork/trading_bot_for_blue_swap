"""One local live sender at a time, shared by the legacy and event services."""

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

from runtime import ConfigurationError


class SendBlocked(ConfigurationError, RuntimeError):
    """Local execution state requires reconciliation before another sender starts."""


@contextmanager
def sender(directory, service, *, live=True):
    """Hold the shared lock before loading execution state and until sending finishes.

    A service may resume its own position. Manual swaps and deployments require
    both ledgers to be flat. Every live sender respects both halt markers.
    """
    if service not in ("legacy", "events", "manual"):
        raise ValueError("unknown sender")
    if not live:
        yield
        return
    directory = Path(directory)
    with acquire(directory / "operator.lock"):
        for marker in (directory / "HALTED", directory / "events/HALTED"):
            if marker.exists():
                raise SendBlocked(f"Reconcile {marker} before starting a live sender")
        if service != "legacy":
            require_flat(directory / "ledger.json", "open")
        if service != "events":
            require_flat(directory / "events/execution_live.json", "position", "intent")
        yield


def require_flat(path, position_key, intent_key=None):
    """Do not switch services while the other owns inventory or an unresolved send."""
    try:
        data = json.loads(Path(path).read_text())
        occupied = data[position_key] is not None or (
            intent_key and data[intent_key] is not None
        )
    except FileNotFoundError:
        return
    except (ValueError, KeyError, TypeError, OSError):
        raise SendBlocked(
            "cannot verify the other service's execution ledger"
        ) from None
    if occupied:
        raise SendBlocked(
            "the other service has inventory or an unresolved intent; reconcile before switching"
        )


@contextmanager
def acquire(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SendBlocked("another local service holds the operator lock") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
