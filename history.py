import math
from pathlib import Path

import ledger

HISTORY_FILE = ledger.LOG_DIR / "prices.csv"
MAX_BYTES = 1_000_000  # about 3 weeks of one sample per 30s; the oldest half goes when it is hit

# ------------------------------------
# One "unix_time,price" line per bot tick, so the dashboard can draw a chart without asking
# the chain for history.
# ------------------------------------


def append(path, ts, price):
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as f:
        # a crash in the middle of an earlier write can leave a line without its newline: start
        # on a fresh line so the torn one cannot swallow this sample
        if path.stat().st_size and path.read_bytes()[-1:] != b"\n":
            f.write("\n")
        f.write(f"{int(ts)},{price!r}\n")
    if path.stat().st_size > MAX_BYTES:
        lines = path.read_text().splitlines()
        ledger.atomic_write(path, "\n".join(lines[len(lines) // 2 :]) + "\n")


def tail(path, count):
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    points = []
    for line in lines[-count:]:
        try:
            ts, price = line.split(",")
            ts, price = int(ts), float(price)
        except ValueError:
            continue  # a torn or hand-edited line is skipped, not fatal
        if math.isfinite(price) and price > 0:  # "nan", "inf" and 0 are not prices
            points.append((ts, price))
    return points


def trailing_run(points, now, limit):
    # The newest unbroken stretch of (time, price) points, as prices. Empty when the newest point
    # is older than `limit` seconds (or dated in the future), and cut off at the first gap longer
    # than `limit`: prices on either side of a long gap belong to different markets.
    if not points or not -60 <= now - points[-1][0] <= limit:
        return []
    start = len(points) - 1
    while start > 0 and 0 <= points[start][0] - points[start - 1][0] <= limit:
        start -= 1
    return [price for _, price in points[start:]]
