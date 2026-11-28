"""Plain-language Discord messages from confirmed Phase 3 events and fills."""

import re
from decimal import Decimal

from phase3_chain import FEE, SQRT_FIELD, WAD
from phase_markets import PHASE3


def actor(address):
    known = {
        PHASE3.market_bot.lower(): "Market bot",
        "0xb6bf0ba3f43d4386216df037dca1a1203038e135": "Our enrolled wallet",
        "0x9cb82edacbda4bdb053a3e7d5eccd6798addc731": "Our trading operator",
        "0x418f86d4f99ce7fb7d045db23c933558072a02df": "Our spot vault",
        "0x346b0c324963b91afb6f722d126e90506623bb8d": "Our credit vault",
    }
    if not address:
        return "An account"
    return known.get(address.lower(), f"Account {address[:6]}…{address[-4:]}")


def token(address):
    return {PHASE3.quote.lower(): "f564", PHASE3.base.lower(): "fETH"}.get(
        address.lower(), "tokens"
    )


def amount(raw, symbol):
    value = Decimal(raw) / WAD
    places = 6 if symbol != "f564" else 4
    text = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    if raw > 0 and text == "0":
        text = f"<{10**-places:.{places}f}"
    return f"{text} {symbol}"


def receipt(tx):
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", tx or ""):
        return f" [View transaction](<https://sepolia.etherscan.io/tx/{tx}>)"
    return ""


def pool_notice(event, a, who):
    if event == "Swap":
        q, b = a["amount0"], a["amount1"]
        paid, received = amount(abs(q), "f564"), amount(abs(b), "fETH")
        if q > 0:
            trade = f"bought {received} for {paid}"
        else:
            trade = f"sold {received} for {paid}"
        sqrt = a[SQRT_FIELD]
        price = Decimal(2**192) / sqrt**2
        fee, symbol = (q, "f564") if q > 0 else (b, "fETH")
        return f"{who} {trade}. Pool price {price:,.2f} f564/fETH. Pool fee ≈ {amount(Decimal(fee) * FEE / 10**6, symbol)}."
    if event in ("Mint", "Burn", "Collect", "CollectProtocol", "Flash"):
        if event == "Burn" and not a["amount"]:
            return None  # Fee-accounting update, no liquidity removed.
        verb = {
            "Mint": "added liquidity",
            "Burn": "removed liquidity",
            "Collect": "collected tokens from an LP",
            "CollectProtocol": "collected protocol fees",
            "Flash": "requested a flash loan",
        }[event]
        assets = f"{amount(a['amount0'], 'f564')} and {amount(a['amount1'], 'fETH')}"
        if event in ("Mint", "Burn"):
            lo, hi = a["tickLower"], a["tickUpper"]
            if (lo, hi) == (-887220, 887220):
                band = "full"
            else:
                band = f"{Decimal('1.0001') ** -hi:,.2f}–{Decimal('1.0001') ** -lo:,.2f} f564/fETH"
            return f"{who} {verb} ({assets}) in the {band} range."
        return f"{who} {verb} ({assets})."
    summary = {
        "Initialize": "Pool initialized",
        "SetFeeProtocol": "Pool protocol fee settings changed",
        "IncreaseObservationCardinalityNext": "Pool price-history capacity increased",
    }
    return summary.get(event, "Pool activity recorded") + f". Signed by {who}."


def credit_notice(event, a, who):
    if event in (
        "Borrowed",
        "Deposited",
        "Repaid",
        "Withdrawn",
        "BadDebtRecorded",
        "DebtCeilingUpdated",
    ):
        assets = amount(a["amount"], token(a["token"]))
        if event == "BadDebtRecorded":
            return f"Bad debt of {assets} recorded for {actor(a['borrower'])}."
        if event == "DebtCeilingUpdated":
            return f"Credit facility debt ceiling changed to {assets}."
        if event == "Repaid":
            return f"{actor(a['payer'])} repaid {assets} on the debt of {actor(a['account'])}."
        verb = {
            "Borrowed": "borrowed",
            "Deposited": "deposited collateral of",
            "Withdrawn": "withdrew collateral of",
        }[event]
        return f"{actor(a['account'])} {verb} {assets}."
    if event == "Liquidated":
        repaid = amount(a["repaid"], token(a["debtToken"]))
        seized = amount(a["seized"], token(a["collateralToken"]))
        return f"{actor(a['liquidator'])} repaid {repaid} of {actor(a['borrower'])}’s debt and received {seized} in collateral."
    if event == "LiquidationBonusUpdated":
        return f"Liquidation bonus changed to {a['basisPoints'] / 100:g}%."
    if event == "PauseUpdated":
        return f"Credit facility {'paused' if a['paused'] else 'resumed'}."
    return f"Credit facility activity recorded. Signed by {who}."


def notices(rows):
    for row in rows:
        label, event, a = row["contract"], row["event"], row["args"]
        who = actor(row.get("signer"))
        if label == "pool":
            text = pool_notice(event, a, who)
        elif label == "credit":
            text = credit_notice(event, a, who)
        elif event == "Approval" and a["spender"].lower() == PHASE3.credit.lower():
            value = (
                f"unlimited {label}"
                if a["value"] == 2**256 - 1
                else amount(a["value"], label)
            )
            text = f"{actor(a['owner'])} set the credit facility allowance to {value}."
        elif event == "EndowmentGranted":
            text = f"{actor(a['recipient'])} received a competition endowment of {amount(a['amount'], label)}."
        elif event in ("Transfer", "Approval"):
            continue
        else:
            text = f"{label} token settings or activity changed."
        if text:
            yield text + receipt(row.get("tx"))


def digest(lines):
    # Keep whole receipt links and reserve room for the Discord prefix and summary.
    lines = list(lines)
    included = []
    for line in lines[:8]:
        if len("\n".join([*included, line])) > 1850:
            break
        included.append(line)
    remaining = len(lines) - len(included)
    if remaining:
        included.append(f"{remaining} more events in the monitor journal.")
    return "\n".join(included)
