"""Public Sepolia history only: no dotenv, keys, signing or transactions.

Run from repo root with `logs`, then `enrich`. Files are resumable and block-pinned.
"""

import concurrent.futures
import json
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

OUT = Path(__file__).parent
RPC = "https://fin564.vercel.app/api/rpc/sepolia"
POOLS = [
    "0x68D8bC396df8e791EFd45A9b214CB07fe4300f48",
    "0xC67Fcb8459C792aE1E271f24CE212Ba90eCC2920",
]
LOCK = threading.Lock()
NEXT_CALL = 0.0
LOCAL = threading.local()


def rpc(method, params):
    global NEXT_CALL
    if not hasattr(LOCAL, "session"):
        LOCAL.session = requests.Session()
    for attempt in range(4):
        with LOCK:
            now = time.monotonic()
            wait = max(0, NEXT_CALL - now)
            NEXT_CALL = max(now, NEXT_CALL) + 0.35
        time.sleep(wait)
        try:
            response = LOCAL.session.post(
                RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=45,
            )
            if response.status_code in (429, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise ValueError(data["error"])
            return data["result"]
        except (requests.RequestException, json.JSONDecodeError):
            if attempt == 3:
                raise RuntimeError(f"public RPC failed for {method}") from None
    raise RuntimeError(f"public RPC retries exhausted for {method}")


def write(name, data):
    path = OUT / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    temporary.replace(path)


def read(name, default=None):
    path = OUT / name
    return json.loads(path.read_text()) if path.exists() else default


def logs():
    assert int(rpc("eth_chainId", []), 16) == 11155111
    manifest = read("manifest.json")
    if manifest is None:
        end = rpc("eth_getBlockByNumber", ["latest", False])
        target = int(end["timestamp"], 16) - 7 * 86400
        # Locate the first block at/after the exact 7-day timestamp; do not assume 12s slots.
        hi = int(end["number"], 16)
        lo = max(0, hi - 7 * 86400 // 8)
        while lo < hi:
            mid = (lo + hi) // 2
            block = rpc("eth_getBlockByNumber", [hex(mid), False])
            if int(block["timestamp"], 16) < target:
                lo = mid + 1
            else:
                hi = mid
        manifest = {
            "chain_id": 11155111,
            "source": RPC,
            "pools": POOLS,
            "start_block": lo,
            "end_block": int(end["number"], 16),
            "end_hash": end["hash"],
            "start_timestamp": target,
            "end_timestamp": int(end["timestamp"], 16),
            "end_utc": datetime.fromtimestamp(
                int(end["timestamp"], 16), UTC
            ).isoformat(),
            "next_block": lo,
            "complete": False,
        }
        write("manifest.json", manifest)
    events = read("pool_events.json", [])
    cursor, chunk = manifest["next_block"], 4000
    while cursor <= manifest["end_block"]:
        end = min(cursor + chunk - 1, manifest["end_block"])
        try:
            batch = rpc(
                "eth_getLogs",
                [{"fromBlock": hex(cursor), "toBlock": hex(end), "address": POOLS}],
            )
        except ValueError:
            if chunk <= 10:
                raise
            chunk = max(10, chunk // 2)
            continue
        events.extend(batch)
        # Data first; deduplication makes replay after a crash safe.
        events = list(
            {(e["transactionHash"], e["logIndex"]): e for e in events}.values()
        )
        write("pool_events.json", events)
        cursor = end + 1
        manifest["next_block"] = cursor
        write("manifest.json", manifest)
        print(f"through block {end}: {len(events)} pool events", flush=True)
    check = rpc("eth_getBlockByNumber", [hex(manifest["end_block"]), False])
    assert check["hash"] == manifest["end_hash"], "end block reorganized"
    manifest["complete"] = True
    manifest["event_count"] = len(events)
    manifest["event_blocks"] = len({e["blockNumber"] for e in events})
    manifest["transactions"] = len({e["transactionHash"] for e in events})
    write("manifest.json", manifest)
    print(json.dumps(manifest, indent=2), flush=True)


def enrich():
    manifest = read("manifest.json")
    assert manifest and manifest["complete"], "collect logs first"
    events = read("pool_events.json")
    hashes = {e["transactionHash"] for e in events}
    block_numbers = sorted({int(e["blockNumber"], 16) for e in events})
    blocks, transactions = read("blocks.json", {}), read("transactions.json", {})

    def get_block(number):
        # The public proxy refuses full transaction objects in block responses.
        block = rpc("eth_getBlockByNumber", [hex(number), False])
        return number, block

    missing = [n for n in block_numbers if str(n) not in blocks]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for count, (number, block) in enumerate(executor.map(get_block, missing), 1):
            blocks[str(number)] = block
            if count % 50 == 0:
                write("transactions.json", transactions)
                write("blocks.json", blocks)
                print(
                    f"blocks {count}/{len(missing)}; transactions {len(transactions)}",
                    flush=True,
                )
    write("transactions.json", transactions)
    write("blocks.json", blocks)
    missing = sorted(hashes - transactions.keys())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for count, (tx_hash, tx) in enumerate(
            executor.map(lambda h: (h, rpc("eth_getTransactionByHash", [h])), missing),
            1,
        ):
            assert tx is not None
            transactions[tx_hash] = tx
            if count % 50 == 0:
                write("transactions.json", transactions)
                print(f"transactions {count}/{len(missing)}", flush=True)
    write("transactions.json", transactions)
    assert hashes <= transactions.keys(), "missing transactions from blocks"
    receipts = read("receipts.json", {})
    missing = sorted(hashes - receipts.keys())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for count, (tx_hash, receipt) in enumerate(
            executor.map(lambda h: (h, rpc("eth_getTransactionReceipt", [h])), missing),
            1,
        ):
            assert receipt is not None
            receipts[tx_hash] = receipt
            if count % 50 == 0:
                write("receipts.json", receipts)
                print(f"receipts {count}/{len(missing)}", flush=True)
    write("receipts.json", receipts)
    for event in events:
        receipt = receipts[event["transactionHash"]]
        block = blocks[str(int(event["blockNumber"], 16))]
        assert event["blockHash"] == receipt["blockHash"] == block["hash"]
        assert receipt["status"] == "0x1"
    check = rpc("eth_getBlockByNumber", [hex(manifest["end_block"]), False])
    assert check["hash"] == manifest["end_hash"], "end block reorganized"
    manifest["enriched"] = True
    write("manifest.json", manifest)
    print(
        f"Complete: {len(events)} events, {len(transactions)} transactions, {len(blocks)} blocks",
        flush=True,
    )


if __name__ == "__main__":
    {"logs": logs, "enrich": enrich}[sys.argv[1]]()
