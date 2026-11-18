"""Explain saved Phase 2 entry state without reading the chain or changing controls."""


def trading_controls(value, requested, error, now):
    applied = value.get("controls") or {}
    fresh = (
        type(value.get("updated")) in (int, float) and 0 <= now - value["updated"] <= 30
    )
    known = type(applied.get("event_paused")) is bool and value.get("mode") in (
        "paper",
        "simulate",
        "live",
    )
    enabled = known and not applied["event_paused"]
    mode_pending = requested.get("event_mode_request", 0) > (
        value.get("mode_done") or 0
    )
    start_pending = not requested.get("event_paused", True) and requested.get(
        "event_start_request", 0
    ) > applied.get("event_start_request", 0)
    pause_pending = requested.get("event_paused", True) and enabled
    reason = error or value.get("control_error") or value.get("mode_block") or ""
    if not fresh or not known:
        reason = (
            reason
            or "Runtime status is unknown or stale. Start Trading requires a fresh acknowledgement."
        )
    elif mode_pending:
        reason = (
            reason
            or "Wait for the runtime to acknowledge the mode before starting entries."
        )
    elif value.get("health") in (
        "failed",
        "halted",
        "stopped",
        "unreadable",
        "not_started",
        "starting",
    ):
        reason = reason or "Runtime is not available for Start Trading."
    # With stale/unknown status, always offer a safe pause, even when the last
    # applied state said paused. A successful file write is not an acknowledgement.
    stop = enabled or start_pending or not fresh or not known
    target = "simulate" if value.get("mode") == "live" else "live"
    availability = value.get("mode_availability", {})
    mode_reason = availability.get(target, "")
    if value.get("mode") == "paper":
        target = "simulate"
        mode_reason = (
            availability.get(target)
            or "Paper has no validated execution contract. Configure a Phase2TradingBot to use Simulation."
        )
    return {
        "enabled": enabled if known else None,
        "fresh": fresh,
        "mode_pending": mode_pending,
        "start_pending": start_pending,
        "pause_pending": pause_pending,
        "start_block": reason,
        "trading_command": "event_pause" if stop else "event_resume",
        "trading_label": "Stop Trading" if stop else "Start Trading",
        "trading_disabled": bool(error) or (not stop and bool(reason)),
        "mode_target": target,
        "mode_reason": mode_reason,
        "mode_disabled": bool(error)
        or not fresh
        or not known
        or mode_pending
        or bool(mode_reason),
    }


def entry_status(value, now):
    cfg = value.get("controls") or {}
    blockers = []

    def block(code, reason, action):
        blockers.append({"code": code, "reason": reason, "action": action})

    health = value.get("health", "unknown")
    updated = value.get("updated")
    if updated is None or not 0 <= now - updated <= 30:
        block(
            "stale_status",
            "Runtime status is missing or stale.",
            "Check the bot process and its logs.",
        )
    if health in ("halted", "failed", "stopped", "unreadable", "not_started"):
        block(
            health,
            value.get("message", health),
            "Resolve the reported runtime problem before starting entries.",
        )
    if value.get("intent_pending"):
        block(
            "intent",
            "An execution intent is unresolved.",
            "Reconcile its outcome before further execution.",
        )
    if value.get("control_error"):
        block(
            "control_error",
            value["control_error"],
            "Repair the control file, then request Start Trading again.",
        )
    if value.get("mode_block"):
        block(
            "mode_block",
            value["mode_block"],
            "Resolve the mode refusal, request the mode again, then Start Trading.",
        )
    if cfg.get("event_mode_request", 0) > (value.get("mode_done") or 0):
        block(
            "mode_pending",
            "Execution mode change is awaiting acknowledgement.",
            "Wait for the active mode, then click Start Trading.",
        )
    start = cfg.get("event_start_request", 0)
    floor = value.get("start_floor")
    fresh_start = start > (
        floor if floor is not None else cfg.get("event_mode_request", 0)
    )
    if cfg.get("event_paused", True) or not fresh_start:
        block(
            "paused",
            "New entries are paused; a fresh Start Trading request is required.",
            "Verify the active mode, then click Start Trading when ready.",
        )
    modules = {
        name: cfg.get("event_" + name, False)
        for name in ("backrun", "frontrun", "arbitrage")
    }
    if not any(modules.values()):
        block(
            "modules_off",
            "All entry strategies are disabled.",
            "Enable the intended Phase 2 strategy.",
        )
    feed = value.get("feed") or {}
    if not value.get("connected") or feed.get("gap") or feed.get("recovering"):
        block(
            "feed",
            "Event feed is disconnected or recovering.",
            "Wait for feed recovery and snapshot reconciliation; check feed diagnostics if it persists.",
        )
    stamp = value.get("snapshot_time")
    age = now - stamp if stamp is not None else None
    if health == "awaiting_scan":
        block(
            "awaiting_scan",
            "A validated liquidity layout is unavailable.",
            "Request Scan Pool Liquidity; failed scans require another request.",
        )
    elif health == "scanning":
        block(
            "scanning",
            "The requested liquidity scan is in progress.",
            "Wait for both pools to reconcile.",
        )
    elif age is None or not 0 <= age <= 30:
        block(
            "snapshot",
            "The trading snapshot is missing or stale.",
            "Wait for a fresh reconciled snapshot; inspect runtime diagnostics if it persists.",
        )
    if value.get("position") is not None:
        block(
            "position",
            "An open event position reserves the shared vault balance.",
            "Wait for the recorded exit or request Exit event position.",
        )
    remaining = max(0, value.get("cooldown_until", 0) - now)
    if remaining:
        block(
            "cooldown",
            f"Execution cooldown: {remaining:.0f}s remaining.",
            "Wait for the cooldown to finish.",
        )
    if health == "degraded":
        block(
            "degraded",
            value.get("message", "Read or validation failed."),
            "Wait for read recovery; inspect the reported validation failure if it persists.",
        )
    latest = value.get("last_evaluation")
    if latest is None:
        latest = next(
            (
                d
                for d in reversed(value.get("decisions", []))
                if d.get("kind") in ("pending", "mined", "arbitrage")
            ),
            None,
        )
    mode = value.get("mode")
    return {
        "blockers": blockers,
        "summary": blockers[0]["reason"]
        if blockers
        else "Monitoring enabled strategies for an eligible opportunity.",
        "modules": modules,
        "snapshot_age": age,
        "cooldown_seconds": remaining,
        "last_evaluation": latest,
        "last_execution": value.get("last_execution"),
        "mode_note": {
            "paper": "Paper models opportunities only; it does not simulate contract execution or send trades.",
            "simulate": "Simulation validates eligible executions without sending transactions.",
            "live": "Live can send Sepolia transactions only after every entry and execution check passes.",
        }.get(mode, "Execution mode is unavailable."),
    }
