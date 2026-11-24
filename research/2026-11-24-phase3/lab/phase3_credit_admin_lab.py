"""Offline lab: admin controls of the Phase 3 credit facility.

The organizers are hard-coded PUSH20 constants; the lab stands throwaway
lab accounts in for them so pause / ceilings / bonus / bad-debt settlement
can be exercised on the in-memory chain.  No network, no real keys.
    uv run python research/2026-11-24-phase3/lab/phase3_credit_admin_lab.py
"""

import sys
from pathlib import Path

from web3.logs import DISCARD

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase3_credit_labkit import A, ORGANIZERS, Lab  # noqa: E402

lab = Lab(admins=True)
w3, c = lab.w3, lab.c
chao, john = (lab.admins[real] for real in ORGANIZERS)
alice, bob, carol, liq = lab.alice, lab.bob, lab.carol, lab.liquidator
F564, FETH = lab.f564.address, lab.feth.address
CORE_L = 57_341_279_504_686_431_094_117
lab.pool.functions.setLiquidity(CORE_L).transact()
lab.pool.functions.setCardinality(180).transact()
out = []
EVENTS = {e["name"]: getattr(lab.credit.events, e["name"]) for e in lab.credit.abi if e.get("type") == "event"}


def p(*parts):
    line = " ".join(str(x) for x in parts)
    out.append(line)
    print(line)


def f(v, n=4):
    return f"{v / A:.{n}f}"


def section(title):
    p("\n== " + title + " ==")


def health(who):
    h = c.healthOf(who).call()
    return "inf" if h > 2**255 else f"{h / A:.6f}"


def market(tok):
    m = c.market(tok).call()
    return f"debt {f(m[0])} ceiling {f(m[1])} util {f(m[2], 6)} rate {f(m[3], 6)}/yr"


def pos(who):
    return (f"collateral {f(c.collateralOf(who, FETH).call())} fETH + {f(c.collateralOf(who, F564).call())} f564, "
            f"debt {f(c.debtOf(who, FETH).call())} fETH + {f(c.debtOf(who, F564).call())} f564, health {health(who)}, "
            f"badDebt {f(c.badDebtOf(who, F564).call())} f564 / {f(c.badDebtOf(who, FETH).call())} fETH")


def fmt(k, v):
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int) and v > 10**9:
        return f"{v / A:,.4f}"
    return str(v)[:10]


def send(label, fn, **kw):
    """Mined transaction with its facility events decoded."""
    try:
        receipt = w3.eth.wait_for_transaction_receipt(fn.transact(kw))
    except Exception as exc:  # noqa: BLE001
        from phase3_credit_labkit import reason

        p(f"{label}: {reason(exc)}")
        return None
    names = []
    for name, event in EVENTS.items():
        for log in event.process_receipt(receipt, errors=DISCARD):
            names.append(f"{name}(" + ", ".join(f"{k}={fmt(k, v)}" for k, v in log["args"].items()) + ")")
    p(f"{label}: ok | {' ; '.join(names)}")
    return receipt


def why(fn, **kw):
    status, res = lab.try_call(fn, **kw)
    if status == "revert":
        return res
    return "ok" + (" -> " + " / ".join(f(x, 6) for x in res) if isinstance(res, (tuple, list)) else "")


def skip(seconds):
    w3.provider.ethereum_tester.time_travel(w3.eth.get_block("latest")["timestamp"] + seconds)
    w3.provider.ethereum_tester.mine_blocks(1)


try:
    section("who is admin")
    p("isAdmin(CHAO, JOHN, alice):", c.isAdmin(chao).call(), c.isAdmin(john).call(), c.isAdmin(alice).call(),
      "| CHAO()", c.CHAO().call(), "| JOHN()", c.JOHN().call(), "| stand-in balances", f(w3.eth.get_balance(chao), 2), f(w3.eth.get_balance(john), 2))

    section("positions and the events they emit")
    send("alice deposit fETH", c.deposit(FETH, 10 * A), **{"from": alice})
    send("alice deposit f564", c.deposit(F564, 10_000 * A), **{"from": alice})
    send("alice borrow", c.borrow(F564, 6_000 * A), **{"from": alice})
    send("bob deposit", c.deposit(FETH, 20 * A), **{"from": bob})
    send("bob borrow", c.borrow(F564, 12_000 * A), **{"from": bob})
    send("carol deposit", c.deposit(F564, 60_000 * A), **{"from": carol})
    send("carol borrow fETH", c.borrow(FETH, int(35.9 * A)), **{"from": carol})
    send("alice repay", c.repay(F564, 100 * A), **{"from": alice})
    send("alice withdraw", c.withdraw(F564, 100 * A), **{"from": alice})
    p("alice:", pos(alice))
    p("bob:", pos(bob))
    p("carol:", pos(carol))
    p("market f564:", market(F564), "| fETH:", market(FETH))

    section("1. pause")
    p("pause by stranger:", lab.try_send(c.pause(), **{"from": liq}))
    send("pause by JOHN:", c.pause(), **{"from": john})
    p("paused()", c.paused().call())
    p("during pause -> deposit:", why(c.deposit(F564, 1), **{"from": alice}), "| borrow:", why(c.borrow(F564, 1), **{"from": alice}),
      "| withdraw:", why(c.withdraw(F564, 1), **{"from": alice}), "| repay:", why(c.repay(F564, 1), **{"from": alice}))
    lab.set_price(700.0)
    p("  bob at 700 is liquidatable -> liquidate during pause:", why(c.liquidate(bob, F564, FETH, 2**256 - 1), **{"from": liq}))
    lab.set_price(1000.0)
    d0 = c.debtOf(bob, F564).call()
    skip(86400)
    p(f"  debt over a 1-day pause: bob {f(d0, 8)} -> {f(c.debtOf(bob, F564).call(), 8)} (rate {f(c.market(F564).call()[3], 6)}/yr)")
    p("unpause by stranger:", lab.try_send(c.unpause(), **{"from": liq}))
    send("unpause by CHAO:", c.unpause(), **{"from": chao})
    p("paused()", c.paused().call())
    d0 = c.debtOf(bob, F564).call()
    skip(86400)
    p(f"  debt over 1 day unpaused: bob {f(d0, 8)} -> {f(c.debtOf(bob, F564).call(), 8)}")

    section("2. setDebtCeiling")
    p("by stranger:", lab.try_send(c.setDebtCeiling(F564, 1), **{"from": liq}), "| unknown token by admin:", lab.try_send(c.setDebtCeiling(liq, 1), **{"from": john}))
    send("CHAO sets the f564 ceiling to 10,000 (below the 17,900 outstanding)", c.setDebtCeiling(F564, 10_000 * A), **{"from": chao})
    p("market f564:", market(F564))
    p("  borrow 1:", why(c.borrow(F564, 1), **{"from": alice}), "| repay:", why(c.repay(F564, 1), **{"from": alice}),
      "| withdraw:", why(c.withdraw(F564, 1), **{"from": alice}), "| liquidate bob:", why(c.liquidate(bob, F564, FETH, 1), **{"from": liq}))
    send("ceiling to 0", c.setDebtCeiling(F564, 0), **{"from": chao})
    p("market f564:", market(F564))
    send("ceiling back to 100,000 (by JOHN)", c.setDebtCeiling(F564, 100_000 * A), **{"from": john})
    send("ceiling to 2^255", c.setDebtCeiling(F564, 2**255), **{"from": john})
    send("ceiling back to 100,000", c.setDebtCeiling(F564, 100_000 * A), **{"from": john})

    section("3. setLiquidationBonus")
    p("by stranger:", lab.try_send(c.setLiquidationBonus(1000), **{"from": liq}))
    allowed, refused = [], set()
    for bps in list(range(0, 1201)) + [1500, 2000, 5000, 10000, 2**256 - 1]:
        status, res = lab.try_call(c.setLiquidationBonus(bps), **{"from": chao})
        if status == "ok":
            allowed.append(bps)
        else:
            refused.add(res)
    p("allowed bonus (bps), by eth_call:", allowed[:8], "...", allowed[-8:], "| refused reasons:", sorted(refused))
    send("bonus to 1000 for one liquidation", c.setLiquidationBonus(1000), **{"from": chao})
    lab.set_price(700.0)
    send("liquidate bob at the unchanged 500 bps bonus", c.liquidate(bob, F564, FETH, 2**256 - 1), **{"from": liq})
    p("bob:", pos(bob))
    send("bonus back to 500", c.setLiquidationBonus(500), **{"from": chao})
    lab.set_price(1000.0)

    section("3b. maximum debt ceiling (binary search by eth_call)")
    for name, tok in (("f564", F564), ("fETH", FETH)):
        lo, hi = 0, 2**200
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if lab.try_call(c.setDebtCeiling(tok, mid), **{"from": chao})[0] == "ok":
                lo = mid
            else:
                hi = mid
        p(f"  {name}: max ceiling {lo} = {lo / A:,.4f} tokens")

    section("4. bad debt and settleBadDebt")
    frank = w3.eth.accounts[6]
    for tok in (lab.f564, lab.feth):
        tok.functions.mint(frank, 1_000_000 * A).transact()
        tok.functions.approve(lab.credit.address, 2**256 - 1).transact({"from": frank})
    send("dave (the liquidator account) deposits 20 fETH", c.deposit(FETH, 20 * A), **{"from": liq})
    send("  and borrows 11,900 f564", c.borrow(F564, 11_900 * A), **{"from": liq})
    lab.set_price(1.0)
    p("dave underwater:", pos(liq))
    p("settleBadDebt(dave) by a stranger BEFORE any liquidation:", why(c.settleBadDebt(liq), **{"from": carol}))
    send("  send it", c.settleBadDebt(liq), **{"from": carol})
    p("dave:", pos(liq), "| carol wallet fETH", f(lab.feth.functions.balanceOf(carol).call()))
    lab.set_price(1000.0)
    send("frank deposits 20 fETH", c.deposit(FETH, 20 * A), **{"from": frank})
    send("  and borrows 11,900 f564", c.borrow(F564, 11_900 * A), **{"from": frank})
    lab.set_price(1.0)
    p("frank underwater:", pos(frank))
    send("alice liquidates frank (collateral exhausted)", c.liquidate(frank, F564, FETH, 2**256 - 1), **{"from": alice})
    p("frank:", pos(frank))
    p("the borrower afterwards -> deposit:", why(c.deposit(F564, 1), **{"from": frank}), "| withdraw:", why(c.withdraw(F564, 1), **{"from": frank}),
      "| borrow:", why(c.borrow(F564, 1), **{"from": frank}), "| repay:", why(c.repay(F564, 1), **{"from": frank}))
    send("frank deposits 100 f564", c.deposit(F564, 100 * A), **{"from": frank})
    p("  -> withdraw:", why(c.withdraw(F564, 1), **{"from": frank}), "| borrow:", why(c.borrow(F564, 1), **{"from": frank}),
      "| repay:", why(c.repay(F564, 1), **{"from": frank}), "| liquidate (f564/f564):", why(c.liquidate(frank, F564, F564, 1), **{"from": carol}))
    send("frank repays 1 f564 toward the bad debt", c.repay(F564, A), **{"from": frank})
    p("frank:", pos(frank))
    send("settleBadDebt(frank) by a stranger", c.settleBadDebt(frank), **{"from": carol})
    p("frank:", pos(frank))
    send("settleBadDebt(frank) again, by CHAO", c.settleBadDebt(frank), **{"from": chao})
    p("  -> withdraw:", why(c.withdraw(F564, 1), **{"from": frank}), "| borrow:", why(c.borrow(F564, 1), **{"from": frank}),
      "| repay:", why(c.repay(F564, 1), **{"from": frank}))
    need, have = c.badDebtOf(frank, F564).call(), c.collateralOf(frank, F564).call()
    send(f"frank deposits up to exactly the bad debt ({f(need)})", c.deposit(F564, need - have), **{"from": frank})
    p("settleBadDebt with collateral == bad debt:", why(c.settleBadDebt(frank), **{"from": carol}))
    send("frank deposits 100 more", c.deposit(F564, 100 * A), **{"from": frank})
    p("settleBadDebt with collateral > bad debt:", why(c.settleBadDebt(frank), **{"from": carol}))
    send("settleBadDebt(frank), a stranger", c.settleBadDebt(frank), **{"from": carol})
    p("frank:", pos(frank), "| borrow now:", why(c.borrow(F564, 1), **{"from": frank}))
    for multiple in (1.05, 1 / 0.8, 1 / 0.6, 2.0, 2.5):
        target = int(need * multiple)
        have = c.collateralOf(frank, F564).call()
        if target > have:
            send(f"  frank tops up to {multiple:.4f} x bad debt ({f(target)})", c.deposit(F564, target - have), **{"from": frank})
        p(f"  settleBadDebt with collateral = {multiple:.4f} x bad debt:", why(c.settleBadDebt(frank), **{"from": carol}))
        if lab.try_call(c.settleBadDebt(frank), **{"from": carol})[0] == "ok":
            break
    send("settleBadDebt(frank), a stranger", c.settleBadDebt(frank), **{"from": carol})
    p("frank:", pos(frank), "| borrow now:", why(c.borrow(F564, 1), **{"from": frank}))
    lab.set_price(1000.0)
    send("settleBadDebt on a healthy account (alice)", c.settleBadDebt(alice), **{"from": chao})
    send("settleBadDebt on an account with no position (JOHN)", c.settleBadDebt(john), **{"from": chao})
    p("borrowing by others after the bad debt:", why(c.borrow(F564, 1), **{"from": carol}), "| market f564:", market(F564))
except Exception as exc:  # noqa: BLE001 — lab: show what broke and keep the output
    raise SystemExit(f"Lab failed: {type(exc).__name__}") from exc
