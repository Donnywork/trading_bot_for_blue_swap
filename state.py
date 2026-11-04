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
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as e:
        raise ValueError(f"{path}: {e}") from e
    return chain
