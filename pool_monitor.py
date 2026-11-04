import itertools
import json
import os
import statistics
import sys
import time
import traceback
from dataclasses import dataclass

from web3.exceptions import Web3RPCError

import bot_contract
import config
from main import LOG_DIR, READ_ERRORS, redact

SEEN_FILE = LOG_DIR / "monitor_seen.json"
BLOCK_SECONDS = 12  # Sepolia; only used to turn "hours back" into a start block
MIN_CHUNK = 100  # smallest eth_getLogs range to try before giving up
# an actor is "scripted" when its gaps between swaps are this steady (stdev / mean)
REGULAR_CV = 0.3
REGULAR_MIN_GAPS = 4


@dataclass(frozen=True)
class Swap:
    # "buy" = the recipient got the base token (token0), "sell" = it gave base away
    ts: int
    recipient: str
    side: str
    base_amount: float
    price: float  # token1 per token0, after the swap


@dataclass(frozen=True)
class Actor:
    address: str
    swaps: int
    buys: int
    sells: int
    net_base: float
    median_gap: float | None  # seconds between its swaps
    last_ts: int
    scripted: bool


def fetch_swaps(pool, start, end, chunk=2000):
    # ------------------------------------
    # Nodes cap eth_getLogs by block range and result count, so read in chunks and halve the
    # chunk when a request is refused.
    # ------------------------------------

    logs = []
    block = start
    while block <= end:
        to = min(block + chunk - 1, end)
        try:
            logs += pool.events.Swap.get_logs(from_block=block, to_block=to)
        except Web3RPCError:
            if chunk <= MIN_CHUNK:
                raise
            chunk //= 2
            continue
        block = to + 1
    return logs


def block_times(w3, blocks):
    return {b: w3.eth.get_block(b)["timestamp"] for b in blocks}


def to_swaps(logs, times, decimals0, decimals1):
    swaps = []
    for e in logs:
        a = e["args"]
        price = (a["sqrtPriceX96"] / 2**96) ** 2 * 10 ** (decimals0 - decimals1)
        swaps.append(
            Swap(
                ts=times[e["blockNumber"]],
                recipient=a["recipient"],
                # amount0 is from the pool's side: negative means the pool paid token0 out
                side="buy" if a["amount0"] < 0 else "sell",
                base_amount=abs(a["amount0"]) / 10**decimals0,
                price=price,
            )
        )
    return swaps


def actor_stats(swaps):
    # ------------------------------------
    # One row per swap recipient. The recipient is the vault or wallet that ends up with the
    # tokens, so it identifies a bot better than the sender (usually just the router).
    # ------------------------------------

    groups = {}
    for s in sorted(swaps, key=lambda s: s.ts):
        groups.setdefault(s.recipient, []).append(s)

    actors = []
    for address, group in groups.items():
        times = [s.ts for s in group]
        gaps = [b - a for a, b in itertools.pairwise(times)]
        scripted = False
        if len(gaps) >= REGULAR_MIN_GAPS and statistics.mean(gaps) > 0:
            scripted = statistics.pstdev(gaps) / statistics.mean(gaps) < REGULAR_CV
        buys = [s for s in group if s.side == "buy"]
        sells = [s for s in group if s.side == "sell"]
        actors.append(
            Actor(
                address=address,
                swaps=len(group),
                buys=len(buys),
                sells=len(sells),
                net_base=sum(s.base_amount for s in buys)
                - sum(s.base_amount for s in sells),
                median_gap=statistics.median(gaps) if gaps else None,
                last_ts=times[-1],
                scripted=scripted,
            )
        )
    return sorted(actors, key=lambda a: -a.swaps)


def load_seen():
    # None on the first run: everyone is "new" then, which says nothing
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (OSError, ValueError):
        return None


def save_seen(seen):
    LOG_DIR.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def span(seconds):
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def report(block, now, swaps, hours, symbols, new, mine):
    # ------------------------------------
    # Human-readable status: price, activity, and who is trading
    # ------------------------------------

    base, quote = symbols
    print(f"\n=== {base}/{quote} pool, block {block}, last {hours}h ===")
    if not swaps:
        print("no swaps in this window")
        return

    prices = [s.price for s in sorted(swaps, key=lambda s: s.ts)]
    change = (prices[-1] / prices[0] - 1) * 100
    print(
        f"price: {prices[-1]:.6g} {quote} per {base} ({change:+.2f}% over the window, "
        f"low {min(prices):.6g}, high {max(prices):.6g})"
    )

    actors = actor_stats(swaps)
    recent = [s for s in swaps if s.ts >= now - 3600]
    print(f"swaps: {len(swaps)} in the window, {len(recent)} in the last hour")
    print(
        f"actors: {len(actors)} in the window, "
        f"{len({s.recipient for s in recent})} in the last hour"
    )
    for a in actors:
        tags = []
        if a.address.lower() == mine:
            tags.append("YOU")
        if a.scripted:
            tags.append("scripted?")
        if a.address in new:
            tags.append("NEW")
        gap = f"every ~{span(a.median_gap)}" if a.median_gap is not None else "once"
        print(
            f"  {a.address} swaps={a.swaps} buy={a.buys} sell={a.sells} "
            f"net={a.net_base:+.4f} {base} {gap}, last {span(now - a.last_ts)} ago "
            + " ".join(tags)
        )
    if new:
        print(f"new since the last run: {len(new)}")


def run(w3, pool, hours, watch):
    symbol0, decimals0 = config.read_token(w3, pool.functions.token0().call())
    symbol1, decimals1 = config.read_token(w3, pool.functions.token1().call())
    mine = os.environ.get("BOT_CONTRACT_ADDRESS", "").lower()
    seen = load_seen()

    swaps = []
    last = w3.eth.block_number - hours * 3600 // BLOCK_SECONDS
    while True:
        try:
            latest = w3.eth.block_number
            logs = fetch_swaps(pool, last + 1, latest)
            times = block_times(w3, {e["blockNumber"] for e in logs})
            fresh = to_swaps(logs, times, decimals0, decimals1)
            now = w3.eth.get_block(latest)["timestamp"]
        except READ_ERRORS as e:
            if not watch:
                raise
            print(f"read failed, will retry: {redact(str(e))}")
        else:
            last = latest
            swaps = [s for s in swaps + fresh if s.ts >= now - hours * 3600]
            current = {s.recipient for s in swaps}
            new = set() if seen is None else current - seen
            report(latest, now, swaps, hours, (symbol0, symbol1), new, mine)
            seen = current | (seen or set())
            save_seen(seen)
        if not watch:
            return
        time.sleep(watch)


def main(hours, watch):
    w3 = bot_contract.connect_read_only()
    pool = w3.eth.contract(
        address=w3.to_checksum_address(os.environ["POOL_ADDRESS"]), abi=config.pool_abi
    )
    run(w3, pool, hours, watch)


def cli():
    # usage: python pool_monitor.py [--hours N] [--watch SECONDS]
    hours, watch = 24, 0
    args = sys.argv[1:]
    try:
        while args:
            flag, value, args = args[0], int(args[1]), args[2:]
            if value <= 0 or flag not in ("--hours", "--watch"):
                raise ValueError
            if flag == "--hours":
                hours = value
            else:
                watch = value
    except (ValueError, IndexError):
        raise SystemExit(
            "usage: pool_monitor.py [--hours N] [--watch SECONDS]"
        ) from None

    # a raw traceback could print the RPC URL, so it goes through redact()
    try:
        main(hours, watch)
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - printed through redact(), then exit 1
        print(redact(traceback.format_exc()), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
