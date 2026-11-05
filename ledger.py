import json
import math
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"

# ------------------------------------
# What the bot bought and what became of it: one open position (or none) plus the closed
# round trips. Amounts are strings so Decimals survive the JSON round trip exactly; prices
# are floats (signals only). Live and dry-run keep separate files, so a paper position can
# never be mistaken for a real one after DRY_RUN is switched.
# ------------------------------------


def file_for(dry_run):
    return LOG_DIR / ("ledger_dryrun.json" if dry_run else "ledger.json")


def empty():
    return {"open": None, "closed": []}


OPEN_KEYS = {"opened", "entry_price", "base", "quote_spent"}
CLOSED_KEYS = OPEN_KEYS | {"closed", "exit_price", "quote_received", "reason"}


def check_trade(trade, keys, where):
    # everything the bot and the dashboard later read from a record, so a hand-edited or damaged
    # file fails here with a clear message instead of as a KeyError in the middle of a tick
    if not isinstance(trade, dict) or not keys <= set(trade):
        raise ValueError(
            f"{where} is missing fields: {sorted(keys - set(trade or {}))}"
        )
    try:
        for name in ("base", "quote_spent", "quote_received"):
            if name in keys:
                Decimal(trade[name])
        price = float(trade["entry_price"])
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError(f"{where} has a number that cannot be read") from e
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"{where} has an invalid entry price")
    # the time stop reads the age of the position from `opened` on every tick
    for name in ("opened", "closed"):
        stamp = trade.get(name, 0)
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, int | float)
            or not math.isfinite(stamp)
        ):
            raise ValueError(f"{where} has an invalid {name} time")


def check(data):
    if (
        not isinstance(data, dict)
        or "open" not in data
        or not isinstance(data.get("closed"), list)
    ):
        raise ValueError("expected an object with 'open' and a list 'closed'")
    if data["open"] is not None:
        check_trade(data["open"], OPEN_KEYS, "the open position")
    for i, trade in enumerate(data["closed"]):
        check_trade(trade, CLOSED_KEYS, f"closed trade {i}")


def load(path):
    # A missing file is a fresh start. An unreadable one is NOT: silently starting over would
    # forget a position the vault may really hold, so the caller must stop and tell a human.
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return empty()
    try:
        data = json.loads(text)
        check(data)
    except ValueError as e:
        raise ValueError(
            f"{path} is unreadable ({e}). Fix it, or move it away if you are sure the "
            "vault holds no position from this bot"
        ) from e
    return data


def atomic_write(path, text):
    # Write to a temp file of its own, flush it to disk, then rename over the target: a crash or a
    # reader at the wrong moment never sees half a file, and two writers never share a temp file.
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(path, data):
    atomic_write(path, json.dumps(data, indent=2))


def open_position(data, price, base, quote_spent, now):
    entry = {
        "opened": now,
        "entry_price": price,
        "base": str(base),
        "quote_spent": str(quote_spent),
    }
    return {**data, "open": entry}


def close_position(data, price, quote_received, reason, now):
    trade = {
        **data["open"],
        "closed": now,
        "exit_price": price,
        "quote_received": str(quote_received),
        "reason": reason,
    }
    return {"open": None, "closed": [*data["closed"], trade]}


def profit(trade):
    # in quote-token units, after fees and price impact (what the vault really gained)
    return Decimal(trade["quote_received"]) - Decimal(trade["quote_spent"])


def summarize(data):
    closed = data["closed"]
    spent = sum((Decimal(t["quote_spent"]) for t in closed), Decimal(0))
    total = sum((profit(t) for t in closed), Decimal(0))
    return {
        "trades": len(closed),
        "wins": sum(1 for t in closed if profit(t) > 0),
        "losses": sum(1 for t in closed if profit(t) <= 0),
        "take_profit": sum(1 for t in closed if t["reason"] == "take_profit"),
        "stop_loss": sum(1 for t in closed if t["reason"] == "stop_loss"),
        "time_stop": sum(1 for t in closed if t["reason"] == "time_stop"),
        "spent": spent,
        "profit": total,
        "profit_pct": float(total / spent * 100) if spent else 0.0,
    }
