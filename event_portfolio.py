"""Decimal-only display accounting, separate from execution and deposited inventory."""

import json
import math
from decimal import Decimal, localcontext

import event_history
import event_market as market

UNIT = Decimal(market.UNIT)


def validate_observation(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise TypeError("invalid holdings observation")
    for key in ("quote_raw", "base_raw", "gas_wei"):
        raw = value[key]
        if key == "gas_wei" and raw is None:
            continue
        if (
            type(raw) not in (str, int)
            or not str(raw).isdigit()
            or not 0 <= int(raw) < 2**256
        ):
            raise ValueError("invalid raw balance")
    if type(value["block"]) is not int or value["block"] < 0:
        raise ValueError("invalid holdings block")
    market.hex_value(value["block_hash"], 32)
    for key in ("timestamp", "observed"):
        if (
            type(value[key]) not in (int, float)
            or not math.isfinite(value[key])
            or value[key] < 0
        ):
            raise ValueError("invalid holdings time")
    price = Decimal(value["price"])
    if (
        not price.is_finite()
        or price <= 0
        or value["pool"] != market.POOLS[500]
        or value["fee"] != 500
    ):
        raise ValueError("invalid valuation price/source")
    if value["operator"] is not None:
        market.hex_value(value["operator"].lower(), 20)


def amount(raw):
    with localcontext() as ctx:
        ctx.prec = 100
        return Decimal(raw) / UNIT


def performance(data):
    """The ledger intentionally retains 200 attempts. Never call this lifetime P/L."""
    rows = data["trades"]
    if not isinstance(rows, list) or not isinstance(data["attempts"], dict):
        raise TypeError("invalid accounting schema")
    if any(r.get("status") not in ("confirmed", "not_sent", "reverted") for r in rows):
        raise ValueError("non-live rows in confirmed accounting")
    if any(
        r.get("action") not in ("entry", "exit", "backrun", "arbitrage") for r in rows
    ):
        raise ValueError("unrecognized execution action")
    if not rows and data["attempts"]:
        raise ValueError("execution history missing")
    confirmed = [r for r in rows if r.get("status") == "confirmed"]
    closed = [
        r for r in confirmed if r.get("action") in ("exit", "backrun", "arbitrage")
    ]
    mined = [r for r in rows if r.get("status") in ("confirmed", "reverted")]
    complete = len(rows) == len(data["attempts"])
    # Reconciled older rows may lack display timestamps while retaining exact
    # receipt accounting. Withhold the date range, not valid profit/gas totals.
    times = [r["at"] for r in rows if r.get("at") is not None]
    if any(
        type(at) not in (int, float) or not math.isfinite(at) or at < 0 for at in times
    ):
        raise ValueError("invalid accounting timestamp")
    timestamps_missing = len(rows) - len(times)
    # An empty *existing ledger* establishes no executions in this ledger, not lifetime zero.
    return {
        "basis": "Retained execution ledger (at most 200 attempts); excludes gas and external flows.",
        "from": min(times, default=None) if not timestamps_missing else None,
        "to": max(times, default=None) if not timestamps_missing else None,
        "timestamps_missing": timestamps_missing,
        "attempts": len(rows),
        "completed": len(closed),
        "earlier_history_missing": not complete,
        "realized_f564": str(amount(sum(r["profit_raw"] for r in closed)))
        if all(type(r.get("profit_raw")) is int for r in closed)
        else None,
        "gas_eth": str(amount(sum(r["gas_wei"] for r in mined)))
        if all(type(r.get("gas_wei")) is int for r in mined)
        else None,
        "gas_missing": sum("gas_wei" not in r for r in mined),
        "simulated_attempts": sum(r.get("status") == "simulated" for r in rows),
    }


def position_view(position, observation, now, exit_seconds):
    if position is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 100
        base, cost = amount(position["base_raw"]), amount(position["quote_raw"])
        value = {
            **position,
            "base": str(base),
            "cost": str(cost),
            "entry_price": str(cost / base),
            "age": max(0, now - position["opened"]),
            "strategy": "Front-run",
            "mode": "live",
            "marked_value": None,
            "unrealized_f564": None,
            "exit_rule": f"Exit when target nonce is included, after {exit_seconds}s, or on an explicit Exit Position request. Failed attempts obey the execution cooldown.",
        }
        if observation and observation["block"] >= position.get("entry_block", 0):
            marked = base * Decimal(observation["price"])
            value.update(marked_value=str(marked), unrealized_f564=str(marked - cost))
        return value


def view(directory, runtime, now):
    position_verified = False
    result = {
        "observation": None,
        "error": "",
        "stale": True,
        "performance": None,
        "performance_error": "",
        "position": None,
        "position_verified": False,
        "baseline": "Unavailable — baseline not established.",
        "flow_note": "Deposits and withdrawals are included in holdings, never in trading P/L. External flows are not reconciled; no flow-adjusted portfolio return is available.",
    }
    try:
        value = json.loads((directory / "portfolio.json").read_text())
        if (
            value.get("identity") != event_history.IDENTITY
            or value["contract"].lower() != (runtime.get("bot_address") or "").lower()
        ):
            raise ValueError("holdings contract mismatch")
        result["error"] = value.get("error", "")
        obs = value.get("observation")
        validate_observation(obs)
        if obs:
            with localcontext() as ctx:
                ctx.prec = 100
                quote, base, price = (
                    amount(obs["quote_raw"]),
                    amount(obs["base_raw"]),
                    Decimal(obs["price"]),
                )
                if not price.is_finite() or price <= 0 or min(quote, base) < 0:
                    raise ValueError("invalid holdings")
                result["observation"] = obs | {
                    "quote": str(quote),
                    "base": str(base),
                    "base_value": str(base * price),
                    "total_value": str(quote + base * price),
                    "gas_eth": str(amount(obs["gas_wei"]))
                    if obs.get("gas_wei") is not None
                    else None,
                }
                result["stale"] = (
                    bool(result["error"]) or not 0 <= now - obs["timestamp"] <= 30
                )
    except FileNotFoundError:
        result["error"] = "No contract holdings observation recorded yet."
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        result["error"] = (
            "Contract holdings file is unreadable or belongs to a different contract."
        )
    try:
        data = json.loads((directory / "execution_live.json").read_text())
        if (
            not runtime.get("bot_address")
            or data.get("version") != 1
            or data["identity"].get("bot_address") != runtime.get("bot_address")
            or {k: data["identity"].get(k) for k in event_history.IDENTITY}
            != event_history.IDENTITY
        ):
            raise ValueError("execution contract mismatch")
        if data.get("canonical_invalid") or runtime.get("health") == "halted":
            raise ValueError("execution receipt reorganized")
        position_verified = (
            runtime.get("execution_verified") is True
            and not runtime.get("intent_pending")
            and data.get("position") == runtime.get("position")
        )
        result["performance"] = performance(data)
        result["position_verified"] = position_verified
    except FileNotFoundError:
        result["performance_error"] = (
            "Confirmed live accounting is unavailable; no live ledger recorded."
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        result["performance_error"] = (
            "Live accounting is incomplete, invalidated, or unreadable; reconcile the execution ledger."
        )
    try:
        result["position"] = position_view(
            runtime.get("position"),
            None if not position_verified else result["observation"],
            now,
            runtime.get("exit_seconds", "configured limit in runtime"),
        )
        if result["position"]:
            result["position"]["canonical"] = position_verified
    except (ValueError, KeyError, TypeError, ArithmeticError):
        result["position_error"] = "Recorded position accounting is incomplete."
    return result
