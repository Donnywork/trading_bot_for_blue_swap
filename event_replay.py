"""Replay paper event strategies offline. No network, dotenv, signing, or transactions."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from event_engine import PaperEngine
from event_market import UNIT
from event_settings import token_amount


def raw(value):
    try:
        return token_amount(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected positive token amount with at most 18 decimals"
        ) from exc


def replay(engine, events):
    for event in events:
        if "now" not in event:
            event = event | {
                "now": datetime.fromisoformat(event["received_utc"]).timestamp()
            }
        yield engine.handle(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events",
        type=Path,
        required=True,
        help="JSONL events, including fresh snapshot events",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new paper journal; refuses overwrite",
    )
    parser.add_argument(
        "--quote-cap", type=raw, default=1000 * UNIT, help="human f564 units"
    )
    parser.add_argument(
        "--base-cap", type=raw, default=10 * UNIT, help="human fETH units"
    )
    parser.add_argument(
        "--min-profit",
        type=raw,
        default=UNIT,
        help="human f564 units, before native ETH gas",
    )
    parser.add_argument(
        "--market-bot",
        action="append",
        help="explicit signer allowlist; repeat for multiple bots",
    )
    parser.add_argument(
        "--competitor",
        action="append",
        default=[],
        help="signer whose activity invalidates preparations",
    )
    args = parser.parse_args()
    options = {"market_bots": args.market_bot} if args.market_bot else {}
    engine = PaperEngine(
        {
            "max_quote_raw": args.quote_cap,
            "max_base_raw": args.base_cap,
            "min_profit_raw": args.min_profit,
        },
        competitors=args.competitor,
        **options,
    )
    with args.events.open() as source, args.output.open("x") as journal:
        for decision in replay(
            engine, (json.loads(line) for line in source if line.strip())
        ):
            journal.write(json.dumps(decision, allow_nan=False) + "\n")
    print("Offline paper replay complete; no transactions submitted.")


if __name__ == "__main__":
    main()
