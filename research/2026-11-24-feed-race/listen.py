"""Public-node pending-feed arrival stamps (read-only observation).

Stamps the arrival time of every pending transaction from the known race
signers at one or more public websocket endpoints, appended as JSONL:

    {"hash": "0x..", "sender": "0x..", "seen_at": <unix float>,
     "endpoint": "wss://..", "full_tx": true|false}

Join against the live bot's logs/events/decisions.jsonl (first_seen_at per
hash) with analyze.py to measure how much earlier or later each public
source sees the same pending than the Alchemy subscription the bot races
on. Endpoints come from the command line only; this never reads .env,
never signs and never sends anything.

Usage:
    python listen.py wss://ethereum-sepolia-rpc.publicnode.com [more wss urls]
    # output: observations.jsonl next to this script (override: --out PATH)
"""

import asyncio
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

    def record(self, hash_value, sender, full_tx):
        row = {
            "hash": hash_value,
            "sender": sender,
            "seen_at": time.time(),
            "endpoint": self.endpoint,
            "full_tx": full_tx,
        }
        with self.out.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        self.stats["matched"] += 1

    async def run(self):
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.endpoint, open_timeout=15) as ws:
                    print(f"{time.strftime('%H:%M:%S')} connected {self.endpoint}", flush=True)
                    backoff = 1.0
                    full_tx = True
                    try:
                        await ws.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": 1,
                                    "method": "eth_subscribe",
                                    "params": ["newPendingTransactions", True],
                                }
                            )
                        )
                        await asyncio.wait_for(ws.recv(), timeout=15)
                    except (websockets.WebSocketException, asyncio.TimeoutError):
                        # Node rejected the full-tx flavour: fall back to
                        # hashes and fetch each transaction to filter it.
                        full_tx = False
                        await ws.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": 1,
                                    "method": "eth_subscribe",
                                    "params": ["newPendingTransactions"],
                                }
                            )
                        )
                        await asyncio.wait_for(ws.recv(), timeout=15)
                    async for message in ws:
                        self.stats["notifications"] += 1
                        await self.handle(message, ws, full_tx)
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
                print(
                    f"{time.strftime('%H:%M:%S')} {self.endpoint} down"
                    f" ({type(exc).__name__}), retry in {backoff:.0f}s",
                    flush=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def handle(self, message, ws, full_tx):
        note = json.loads(message).get("params", {})
        item = note.get("result") if isinstance(note, dict) else None
        if item is None:
            return
        if isinstance(item, dict):
            self.stats["pending_seen"] += 1
            if matched(item.get("from")):
                self.record(item["hash"], item["from"], True)
            return
        # Hash-only subscription: fetch the transaction to read its sender.
        self.stats["pending_seen"] += 1
        try:
            reply = json.loads(await self.fetch(ws, item))
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
            return
        tx = reply.get("result")
        if isinstance(tx, dict) and matched(tx.get("from")):
            self.record(tx["hash"], tx["from"], False)

    @staticmethod
    async def fetch(ws, hash_value):
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_getTransactionByHash",
                    "params": [hash_value],
                }
            )
        )
        # Notifications keep arriving; skip them until the fetch reply lands.
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=10)
            decoded = json.loads(message)
            if decoded.get("id") == 2:
                return json.dumps(decoded)


async def heartbeat(stats):
    while True:
        await asyncio.sleep(60)
        print(
            f"{time.strftime('%H:%M:%S')} notifications={stats['notifications']}"
            f" pendings={stats['pending_seen']} matched={stats['matched']}",
            flush=True,
        )


async def amain(endpoints, out):
    stats = {"notifications": 0, "pending_seen": 0, "matched": 0}
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
