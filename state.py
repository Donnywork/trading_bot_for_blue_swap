import json
import math
from decimal import Decimal
from pathlib import Path

import ledger

STATE_FILE = ledger.LOG_DIR / "state.json"

# ------------------------------------
# What the bot last read from the chain: the price, the vault balances and the operator's gas. The
# bot writes it every tick and the dashboard only reads it, so the dashboard never asks the RPC
# anything (an open tab would otherwise cost more quota than the bot itself). It holds nothing
# secret: public prices and balances, no key and no URL.
# ------------------------------------


def save(path, now, chain):
    # `chain`: base, quote (token symbols), price, vault_base, vault_quote, operator_eth
    ledger.atomic_write(path, json.dumps({"time": int(now), **chain}, default=str))


def load(path):
    # None when the bot has not written one yet; an unreadable one is an error naming the file.
    # The result has the shape the dashboard shows, plus "updated" (unix time of the reading).
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(text)
        chain = {
            "base": str(raw["base"]),
            "quote": str(raw["quote"]),
            "price": float(raw["price"]),
            "vault_base": Decimal(raw["vault_base"]),
            "vault_quote": Decimal(raw["vault_quote"]),
            "operator_eth": Decimal(raw["operator_eth"]),
            "updated": float(raw["time"]),
        }
        numbers = [chain["price"], chain["updated"]] + [
            float(chain[k]) for k in ("vault_base", "vault_quote", "operator_eth")
        ]
        if not all(math.isfinite(n) for n in numbers) or chain["price"] <= 0:
            raise ValueError("a number is not finite, or the price is not positive")
        # how deep the pool was when the bot last sized a buy (1.0 = as saved in pool_profile.json);
        # missing until it has
        scale = float(raw.get("depth_scale", 1.0))
        if not math.isfinite(scale) or scale < 0:
            raise ValueError(
                "the pool depth scale is not a finite, non-negative number"
            )
        chain["depth_scale"] = scale
        # the bot's last answer to the dashboard's "scan the pool" button: which request it served
        # ("done"), and either the block it read ("block", "at") or why it could not ("error")
        scan = raw.get("scan")
        chain["scan"] = None
        if scan is not None:
            chain["scan"] = {
                "done": int(scan["done"]),
                "block": None if scan.get("block") is None else int(scan["block"]),
                "at": None if scan.get("at") is None else float(scan["at"]),
                "error": None
                if scan.get("error") is None
                else str(scan["error"])[:300],
            }
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as e:
        raise ValueError(f"{path}: {e}") from e
    return chain
