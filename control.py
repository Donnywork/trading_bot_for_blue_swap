import json
from pathlib import Path

import ledger
import strategy

CONTROL_FILE = ledger.LOG_DIR / "control.json"

# ------------------------------------
# The settings a person can change while the bot runs: which strategy, take profit, stop loss and
# the strategy's parameters, plus the dashboard's buttons (pause new buys; scan the pool's
# liquidity; sell the open position back to the quote token). The dashboard writes this file; the
# bot re-reads it every tick. It holds nothing secret and cannot change DRY_RUN, the trade size or
# any key.
# ------------------------------------


def load(path):
    # a missing file means the defaults; an invalid one is an error naming the file
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return strategy.DEFAULT
    try:
        return strategy.parse_config(json.loads(text))
    except (ValueError, TypeError, OverflowError, RecursionError) as e:
        raise ValueError(f"{path}: {e}") from e


def save(path, config):
    # to_dict() is what the settings form shows; the dashboard's buttons are saved beside it
    data = {
        **strategy.to_dict(config),
        "paused": config.paused,
        "scan_request": config.scan_request,
        "liquidate_request": config.liquidate_request,
    }
    ledger.atomic_write(path, json.dumps(data, indent=2))
