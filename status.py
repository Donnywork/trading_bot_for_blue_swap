import os
import sys
import time
import traceback
from decimal import Decimal

from dotenv import load_dotenv

import bot_contract
import config
import control
import ledger
import strategy
from main import HALT_FILE, HEARTBEAT_FILE, READ_ERRORS, redact
from pool_monitor import span

STALE_AFTER_SECONDS = 300  # the bot ticks every POLL_SECONDS (30 by default)
RECENT_TRADES = 5


def read_chain(w3):
    # ------------------------------------
    # Price and vault balances: all read-only, no key needed
    # ------------------------------------

    pool = w3.eth.contract(
        address=w3.to_checksum_address(os.environ["POOL_ADDRESS"]), abi=config.pool_abi
    )
    base_address = pool.functions.token0().call()
    quote_address = pool.functions.token1().call()
    base, base_decimals = config.read_token(w3, base_address)
    quote, quote_decimals = config.read_token(w3, quote_address)
    sqrt_price_x96 = pool.functions.slot0().call()[0]
    price = (sqrt_price_x96 / 2**96) ** 2 * 10 ** (base_decimals - quote_decimals)

    bot = bot_contract.load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])

    def vault(token_address, decimals):
        erc20 = bot_contract.token_contract(w3, token_address)
        return Decimal(erc20.functions.balanceOf(bot.address).call()) / 10**decimals

    operator = bot.functions.operator().call()
    return {
        "base": base,
        "quote": quote,
        "price": price,
        "vault_base": vault(base_address, base_decimals),
        "vault_quote": vault(quote_address, quote_decimals),
        "operator_eth": Decimal(w3.eth.get_balance(operator)) / 10**18,
    }


def read_health(now):
    # (kind, text): the bot's own files tell whether it is ticking or was halted
    if HALT_FILE.exists():
        return "halted", HALT_FILE.read_text().strip()
    if not HEARTBEAT_FILE.exists():
        return (
            "unknown",
            "no heartbeat file: not started yet, or running on another machine",
        )
    age = now - HEARTBEAT_FILE.stat().st_mtime
    if age > STALE_AFTER_SECONDS:
        return "stale", f"last tick {span(age)} ago"
    return "running", f"last tick {span(age)} ago"


def money(value, digits=6):
    return f"{value:+.{digits}f}"


def render(data, chain, health, dry_run, now, cfg=strategy.DEFAULT):
    # ------------------------------------
    # The report as a list of lines (pure, so it is testable). `chain` is None when the RPC
    # could not be read: the ledger part still shows.
    # ------------------------------------

    base = chain["base"] if chain else "base"
    quote = chain["quote"] if chain else "quote"
    lines = [f"=== BOT STATUS ({'DRY RUN, paper trades' if dry_run else 'LIVE'}) ==="]

    kind, text = health
    lines.append(f"bot:       {kind.upper()}: {text}")

    if chain:
        lines.append(f"price:     {chain['price']:.6f} {quote} per {base}")
        lines.append(
            f"vault:     {chain['vault_base']} {base}, {chain['vault_quote']} {quote}"
            f"   (operator gas: {chain['operator_eth']:.4f} ETH)"
        )
    else:
        lines.append("price:     unavailable (could not read the chain)")

    held = data["open"]
    if held is None:
        lines.append(
            "position:  none, waiting for a buy signal "
            f"({strategy.STRATEGIES[cfg.strategy].label})"
        )
    else:
        entry = held["entry_price"]
        cost = Decimal(held["quote_spent"])
        lines.append(
            f"position:  OPEN for {span(now - held['opened'])}: {held['base']} {base} "
            f"bought for {cost} {quote} at {entry:.6f}"
        )
        take = entry * (1 + cfg.take_profit_pct / 100)
        stop = entry * (1 - cfg.stop_loss_pct / 100)
        lines.append(
            f"           take profit at {take:.6f} (+{cfg.take_profit_pct:g}%), "
            f"stop loss at {stop:.6f} (-{cfg.stop_loss_pct:g}%)"
            + (
                f", time stop after {cfg.time_stop_min:g} min"
                if cfg.time_stop_on
                else ""
            )
        )
        if chain:
            change = (chain["price"] / entry - 1) * 100
            worth = Decimal(held["base"]) * Decimal(chain["price"])
            lines.append(
                f"           now {chain['price']:.6f} ({change:+.2f}% from entry), "
                f"unrealized {money(worth - cost)} {quote} "
                f"({(worth - cost) / cost * 100:+.2f}%) at the spot price, before exit fees"
            )

    s = ledger.summarize(data)
    if s["trades"] == 0:
        lines.append("realized:  no closed trades yet")
    else:
        lines.append(
            f"realized:  {s['trades']} closed trades: {s['wins']} won, {s['losses']} lost "
            f"({s['take_profit']} take profit, {s['stop_loss']} stop loss"
            f"{f', {s["time_stop"]} time stop' if s['time_stop'] else ''})"
        )
        lines.append(
            f"           P/L {money(s['profit'])} {quote} on {s['spent']} {quote} spent "
            f"({s['profit_pct']:+.2f}%), after fees and price impact"
        )
        lines.append("recent:")
        for t in reversed(data["closed"][-RECENT_TRADES:]):
            when = time.strftime("%m-%d %H:%M", time.localtime(t["closed"]))
            lines.append(
                f"  {when}  {t['reason']:<11} {t['entry_price']:.4f} -> "
                f"{t['exit_price']:.4f}  P/L {money(ledger.profit(t))} {quote}"
            )
    return lines


def main():
    dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
    data = ledger.load(ledger.file_for(dry_run))
    now = time.time()

    chain = None
    try:
        chain = read_chain(bot_contract.connect_read_only())
    except READ_ERRORS as e:
        print(f"could not read the chain: {redact(str(e))}", file=sys.stderr)

    try:
        cfg = control.load(control.CONTROL_FILE)
    except ValueError as e:
        print(f"settings file ignored, showing defaults: {e}", file=sys.stderr)
        cfg = strategy.DEFAULT
    print("\n".join(render(data, chain, read_health(now), dry_run, now, cfg)))


def cli():
    # usage: python status.py   (read-only; DRY_RUN in .env picks the paper or the live ledger)
    if sys.argv[1:]:
        raise SystemExit("usage: status.py")
    # a raw traceback could print the RPC URL, so it goes through redact()
    try:
        load_dotenv()
        main()
    except Exception:  # noqa: BLE001 - printed through redact(), then exit 1
        print(redact(traceback.format_exc()), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
