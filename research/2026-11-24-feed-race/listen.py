"""Public-node pending-feed arrival stamps (read-only observation).

Stamps the arrival time of every pending transaction from the known race
signers at one or more public websocket endpoints, appended as JSONL:

    {"hash": "0x..", "sender": "0x..", "seen_at": <unix float>,
     "endpoint": "wss://..", "full_tx": true|false}

Hash-only endpoints stamp the NOTIFICATION arrival (before the filtering
fetch), so the two subscription flavours stay comparable. Join against the
live bot's logs/events/decisions.jsonl (feed_arrival per hash) with
analyze.py to measure how much earlier or later each public source sees
the same pending than the Alchemy subscription the bot races on.
Endpoints come from the command line only; this never reads .env, never
signs and never sends anything.

Usage:
    python listen.py wss://ethereum-sepolia-rpc.publicnode.com [more wss urls]
    # output: observations.jsonl next to this script (override: --out PATH)
"""

import asyncio
import itertools
import json
import time
from pathlib import Path

import websockets

# Public testnet addresses only (the market bot we react to, the two
# competitors we race, and the third observed swap signer).
SIGNERS = {
    "0x6011139d5f16689bde1d245c6247b088f9582026",  # market bot (targets)
    "0xd067640b2e032b1a878bd87b4d85cb5a87ef64ef",  # competitor 1 (15 gwei sweeps)
    "0x21b1a1e9f20377f4cbf6d874cfcf3df37699c7b7",  # competitor 2 (tip-1 cycles)
    "0x2885f394be3a5c36a0ce67701bfb6c49bb1fb8db",  # third observed signer
}

HERE = Path(__file__).parent
DEFAULT_OUT = HERE / "observations.jsonl"


def matched(sender):
    return sender is not None and sender.lower() in SIGNERS


class Listener:
    def __init__(self, endpoint, out, stats):
        self.endpoint = endpoint
        self.out = out
        self.stats = stats

    def record(self, hash_value, sender, seen_at, full_tx):
        with self.out.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "hash": hash_value,
                        "sender": sender,
                        "seen_at": seen_at,
                        "endpoint": self.endpoint,
                        "full_tx": full_tx,
                    }
                )
                + "\n"
            )
        self.stats["matched"] += 1

    async def await_reply(self, ws, want_id):
        """Receive until the reply with `want_id` lands; defer everything else.

        A notification that races ahead of the subscription ACK must not be
        swallowed — it is returned for normal dispatch afterwards.
        """
        deferred = []
        while True:
            decoded = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if decoded.get("id") == want_id:
                return decoded, deferred
            deferred.append(decoded)

    async def subscribe(self, ws):
        """Try the full-tx flavour, fall back to hashes; validate each ACK.

        A node that REJECTS a flavour answers a JSON-RPC error, not an
        exception — an unchecked ACK leaves a silently dead subscription
        (the bot's own feed checks the same case).
        """
        for params, full_tx in (
            (["newPendingTransactions", True], True),
            (["newPendingTransactions"], False),
        ):
            await ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": params}
                )
            )
            reply, deferred = await self.await_reply(ws, 1)
            if isinstance(reply.get("result"), str):
                return full_tx, deferred
        raise OSError("node rejected both pending subscription flavours")

    async def dispatch(self, decoded, ws, fetches, ids):
        if decoded.get("id") in fetches:
            # A filtering fetch for a hash-only notification: stamp was
            # taken at arrival, this only resolves the sender.
            hash_value, arrived = fetches.pop(decoded["id"])
            tx = decoded.get("result")
            if isinstance(tx, dict) and matched(tx.get("from")):
                self.record(hash_value, tx["from"], arrived, False)
            return
        item = (decoded.get("params") or {}).get("result")
        if item is None:
            return
        self.stats["pending_seen"] += 1
        if isinstance(item, dict):
            if matched(item.get("from")):
                self.record(item["hash"], item["from"], time.time(), True)
            return
        # Hash-only subscription: stamp NOW, then fetch asynchronously to
        # read the sender — the notification stream keeps flowing while the
        # round trip is in flight, so nothing is dropped.
        request_id = next(ids)
        fetches[request_id] = (item, time.time())
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "eth_getTransactionByHash",
                    "params": [item],
                }
            )
        )

    async def run(self):
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.endpoint, open_timeout=15) as ws:
                    print(
                        f"{time.strftime('%H:%M:%S')} connected {self.endpoint}",
                        flush=True,
                    )
                    backoff = 1.0
                    full_tx, deferred = await self.subscribe(ws)
                    print(
                        f"{time.strftime('%H:%M:%S')} subscribed"
                        f" ({'full tx' if full_tx else 'hashes + fetch'})",
                        flush=True,
                    )
                    fetches, ids = {}, itertools.count(2)
                    for decoded in deferred:
                        await self.dispatch(decoded, ws, fetches, ids)
                    async for message in ws:
                        try:
                            decoded = json.loads(message)
                        except ValueError:
                            self.stats["errors"] += 1
                            continue
                        try:
                            await self.dispatch(decoded, ws, fetches, ids)
                        except (ValueError, KeyError, TypeError):
                            # One malformed frame must not stop the night's
                            # collection; transport errors still reconnect.
                            self.stats["errors"] += 1
            except (
                OSError,
                websockets.WebSocketException,
                asyncio.TimeoutError,
            ) as exc:
                print(
                    f"{time.strftime('%H:%M:%S')} {self.endpoint} down"
                    f" ({type(exc).__name__}), retry in {backoff:.0f}s",
                    flush=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def heartbeat(stats):
    while True:
        await asyncio.sleep(60)
        print(
            f"{time.strftime('%H:%M:%S')} pendings={stats['pending_seen']}"
            f" matched={stats['matched']} errors={stats['errors']}",
            flush=True,
        )


async def amain(endpoints, out):
    stats = {"pending_seen": 0, "matched": 0, "errors": 0}
    out.parent.mkdir(parents=True, exist_ok=True)
    listeners = [Listener(url, out, stats) for url in endpoints]
    print(f"writing {out}", flush=True)
    await asyncio.gather(
        *[each.run() for each in listeners] + [heartbeat(stats)]
    )


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoints", nargs="+", help="wss:// endpoint URLs")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.endpoints, args.out))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
