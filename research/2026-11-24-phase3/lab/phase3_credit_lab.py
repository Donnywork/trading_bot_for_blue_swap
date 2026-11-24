"""Offline lab: measure the Phase 3 credit facility's participant rules.

Deploys the facility's PUBLIC creation bytecode on the in-memory eth-tester
chain with mock tokens and a mock pricing pool.  No network, no keys, no
sends to a real chain.
    uv run python research/2026-11-24-phase3/lab/phase3_credit_lab.py
"""

import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase3_credit_labkit import A, Lab  # noqa: E402

lab = Lab()
w3, c = lab.w3, lab.c
alice, bob, carol, liq = lab.alice, lab.bob, lab.carol, lab.liquidator
dave, frank, gina = w3.eth.accounts[6], w3.eth.accounts[7], w3.eth.accounts[8]
START564 = 1_000_000 * A
for who in (dave, frank, gina):
    lab.f564.functions.mint(who, START564).transact()
    lab.feth.functions.mint(who, 1_000 * A).transact()
    lab.f564.functions.approve(lab.credit.address, 2**256 - 1).transact({"from": who})
    lab.feth.functions.approve(lab.credit.address, 2**256 - 1).transact({"from": who})
F564, FETH = lab.f564.address, lab.feth.address
CORE_L = 57_341_279_504_686_431_094_117
lab.pool.functions.setLiquidity(CORE_L).transact()
lab.pool.functions.setCardinality(180).transact()
out = []


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


def supplies():
    return (f"supplies f564 {f(lab.f564.functions.totalSupply().call())} fETH {f(lab.feth.functions.totalSupply().call())} | "
            f"facility cash f564 {f(lab.f564.functions.balanceOf(lab.credit.address).call())} "
            f"fETH {f(lab.feth.functions.balanceOf(lab.credit.address).call())}")


def bal(who, tok):
    return (lab.f564 if tok == F564 else lab.feth).functions.balanceOf(who).call()


def price():
    return c.oraclePrice().call()[0]


def sides(who):
    """(fETH-collateral value, f564 collateral, fETH-debt value, f564 debt) at the current TWAP."""
    pr = price()
    return (c.collateralOf(who, FETH).call() * pr // A, c.collateralOf(who, F564).call(),
            c.debtOf(who, FETH).call() * pr // A, c.debtOf(who, F564).call())


def liq_x(who):
    """TWAP multiplier at which health hits 1: 0.8 (a x + b) == e x + d."""
    a, b, e, d = sides(who)
    if e + d == 0 or 8 * a == 10 * e:
        return None
    return Fraction(10 * d - 8 * b, 8 * a - 10 * e)


def pos(who):
    a, b, e, d = sides(who)
    text = (f"collateral {f(c.collateralOf(who, FETH).call())} fETH + {f(b)} f564, debt {f(c.debtOf(who, FETH).call())} fETH"
            f" + {f(d)} f564, health {health(who)}")
    x = liq_x(who)
    if e + d == 0:
        return text
    if 8 * a > 10 * e:
        return text + f", liquidatable below x{float(x):.4f}"
    if 8 * a < 10 * e:
        return text + (f", liquidatable above x{float(x):.4f}" if x > 0 else ", underwater at any price")
    return text + (", underwater at any price" if 8 * b < 10 * d else ", never liquidatable")


def show(res):
    if isinstance(res, (tuple, list)):
        return " / ".join(f"{v / A:,.6f}".rstrip("0").rstrip(".") for v in res)
    return str(res)


def why(fn, **kw):
    """eth_call: 'ok -> <return values>' or the custom-error name."""
    status, res = lab.try_call(fn, **kw)
    return res if status == "revert" else "ok -> " + show(res)


def do(fn, who, label):
    res = lab.try_send(fn, **{"from": who})
    p(f"{label}: {res}")
    return res == "ok"


def max_amount(who, build, lo=0, hi=10_000_000 * A):
    """Largest amount for which the eth_call succeeds (the exact boundary; interest moves it)."""
    if lab.try_call(build(hi), **{"from": who})[0] == "ok":
        return hi
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if lab.try_call(build(mid), **{"from": who})[0] == "ok":
            lo = mid
        else:
            hi = mid
    return lo


def skip(seconds):
    w3.provider.ethereum_tester.time_travel(w3.eth.get_block("latest")["timestamp"] + seconds)
    w3.provider.ethereum_tester.mine_blocks(1)


def swap_ideal(who, amount_564):
    """f564 -> fETH at the TWAP, no fee, no impact (lab only: the loop's arithmetic)."""
    got = amount_564 * A // price()
    lab.f564.functions.burn(who, amount_564).transact()
    lab.feth.functions.mint(who, got).transact()
    return got


def loop(who, rounds=12):
    """borrow f564 -> sell -> deposit fETH, until the 60% LTV binds."""
    for round_no in range(1, rounds + 1):
        amt = max_amount(who, lambda x: c.borrow(F564, x)) * 999 // 1000
        if not do(c.borrow(F564, amt), who, f"  round {round_no}: borrow {f(amt)} f564"):
            break
        if amt < 10 * A:
            break
        do(c.deposit(FETH, swap_ideal(who, amt)), who, f"    sell for {f(amt * A // price())} fETH, deposit it")
        a, b, e, d = sides(who)
        p(f"    debt {f(d)} | exposure {f(a)} f564 | equity {f(a + b - d)} | leverage {float(a) / float(a + b - d):.2f}x | {pos(who).split(', ', 2)[-1]}")


try:
    section("constants and oracle")
    p("MAX_LTV", c.MAX_LTV_BPS().call(), "LIQ_THRESHOLD", c.LIQUIDATION_THRESHOLD_BPS().call(),
      "bonus", c.liquidationBonusBps().call(), "| YEAR", c.YEAR().call(), "paused", c.paused().call())
    pr, window = c.oraclePrice().call()
    p("oraclePrice", f(pr, 8), "f564 per fETH, window", window, "s (on-chain 1800) | 1.0001^69082 =", 1.0001 ** 69082)
    p("interestRate(u):", " ".join(f"{u}%->{f(c.interestRate(u * 10**16).call(), 6)}" for u in (0, 10, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 99, 100)))
    p("healthOf(no position)", health(alice), "| isAdmin(alice)", c.isAdmin(alice).call())

    section("A. deposits move tokens; borrows MINT new tokens; max borrow = 60% of collateral value")
    p("deposit 0:", why(c.deposit(FETH, 0), **{"from": alice}), "| unknown token:", why(c.deposit(dave, 1), **{"from": alice}))
    do(c.deposit(FETH, 10 * A), alice, "alice deposit 10 fETH")
    do(c.deposit(F564, 10_000 * A), alice, "alice deposit 10,000 f564")
    p("alice:", pos(alice), "|", supplies())
    col = c.collateralOf(alice, FETH).call() * pr // A + c.collateralOf(alice, F564).call()
    cap = max_amount(alice, lambda x: c.borrow(F564, x))
    p("max f564 borrow", f(cap), "| 60% of collateral value", f(col * 6000 // 10000), "| equal:", cap == col * 6000 // 10000)
    p("borrow cap+1:", why(c.borrow(F564, cap + 1), **{"from": alice}), "| no collateral:", why(c.borrow(F564, 1), **{"from": bob}))
    s = lab.f564.functions.totalSupply().call()
    do(c.borrow(F564, cap // 2), alice, f"alice borrows half ({f(cap // 2)})")
    p("alice:", pos(alice), "| f564 supply change", f(lab.f564.functions.totalSupply().call() - s), "|", supplies())
    p("market f564:", market(F564))
    do(c.deposit(F564, 5_000 * A), dave, "dave deposit 5,000 f564")
    same = max_amount(dave, lambda x: c.borrow(F564, x))
    do(c.borrow(F564, same), dave, f"dave borrows {f(same)} of the SAME token (f564 against f564)")
    p("dave:", pos(dave))

    section("B. withdraw with debt must keep debt <= 60% of collateral value")
    wmax = max_amount(alice, lambda x: c.withdraw(F564, x))
    do(c.withdraw(F564, wmax), alice, f"max f564 withdraw ({f(wmax)})")
    p("alice:", pos(alice))
    wmax = max_amount(alice, lambda x: c.withdraw(FETH, x))
    do(c.withdraw(FETH, wmax), alice, f"exact max fETH withdraw by eth_call ({f(wmax, 8)}) sent a block later")
    do(c.withdraw(FETH, wmax * 999 // 1000), alice, "99.9% of it")
    p("alice:", pos(alice), "-> the withdraw limit is the same 60% LTV rule as borrowing")
    do(c.repay(F564, c.debtOf(alice, F564).call()), alice, "alice repays all (amount = a stale debtOf() read)")
    do(c.withdraw(FETH, c.collateralOf(alice, FETH).call()), alice, "alice withdraws all fETH")
    p("alice:", pos(alice), "| debt left", c.debtOf(alice, F564).call(), "wei (interest accrued between the read and the mined block)")
    p("withdraw(type(uint).max) with dust debt outstanding:", why(c.withdraw(FETH, 2**256 - 1), **{"from": alice}))
    p("repay(type(uint256).max):", why(c.repay(F564, 2**256 - 1), **{"from": alice}), "<- repaid")
    do(c.repay(F564, 2**256 - 1), alice, "send repay max")
    p("alice debt left", c.debtOf(alice, F564).call(), "wei | withdraw(type(uint).max):", why(c.withdraw(FETH, 2**256 - 1), **{"from": alice}))
    do(c.withdraw(FETH, 2**256 - 1), alice, "send withdraw max")
    p("alice:", pos(alice))

    section("C. leveraged positions; borrow -> sell -> deposit loop (sale idealised at the TWAP, no fee, no impact)")
    do(c.deposit(FETH, 20 * A), bob, "bob deposit 20 fETH")
    capb = max_amount(bob, lambda x: c.borrow(F564, x))
    do(c.borrow(F564, capb * 999 // 1000), bob, f"bob borrows max {f(capb)} f564 and keeps the borrowed f564 (2.5x long)")
    p("bob:", pos(bob))
    do(c.deposit(F564, 60_000 * A), carol, "carol deposit 60,000 f564")
    capc = max_amount(carol, lambda x: c.borrow(FETH, x))
    do(c.borrow(FETH, capc * 999 // 1000), carol, f"carol borrows max {f(capc)} fETH and sells it (1.5x short)")
    p("carol:", pos(carol))
    do(c.deposit(F564, 20_000 * A), frank, "frank deposit 20,000 f564; loop keeps his f564 as collateral (mixed collateral)")
    loop(frank)
    p("frank (mixed):", pos(frank))
    do(c.deposit(FETH, swap_ideal(gina, 20_000 * A)), gina, "gina sells 20,000 f564 for fETH, deposits the fETH; loop (all-fETH collateral)")
    loop(gina)
    p("gina (pure):", pos(gina))
    p("market f564:", market(F564))

    section("D. debt ceiling is market-wide (first come, first served)")
    do(c.deposit(F564, 200_000 * A), dave, "dave deposits 200,000 f564 more")
    room = c.market(FETH).call()[1] - c.market(FETH).call()[0]
    capd = max_amount(dave, lambda x: c.borrow(FETH, x))
    p("dave max fETH borrow", f(capd), "| ceiling - outstanding fETH debt =", f(room), "| 60% of dave's own collateral would be",
      f(c.collateralOf(dave, F564).call() * A // price() * 6000 // 10000))
    p("borrow room+1:", why(c.borrow(FETH, room + 1), **{"from": dave}))
    do(c.borrow(FETH, capd * 999 // 1000), dave, "dave borrows it")
    p("market fETH:", market(FETH), "| dave borrows 1 more fETH (collateral is plenty):", why(c.borrow(FETH, A), **{"from": dave}))
    do(c.repay(FETH, capd * 999 // 1000), dave, "dave repays -> the capacity is free again")
    p("market fETH:", market(FETH))

    section("E. health vs TWAP price: health = 0.8 x (fETH collateral x TWAP + f564 collateral) / (f564 debt + fETH debt x TWAP)")
    p("bob: 2.5x long (fETH collateral).  carol: 1.5x short (f564 collateral).  frank: 2.44x long, mixed collateral")
    p("price     xP0      bob       carol      frank    liquidate(bob)    liquidate(carol)   liquidate(frank)")
    for target in (1000.0, 800.0, 751.0, 750.0, 700.0, 684.0, 600.0, 500.0, 1100.0, 1300.0, 1333.0, 1334.0, 1335.0, 1500.0, 2000.0):
        lab.set_price(target)
        pr = price()
        tags = []
        for who, dt, ct in ((bob, F564, FETH), (carol, FETH, F564), (frank, F564, FETH)):
            st, res = lab.try_call(c.liquidate(who, dt, ct, c.debtOf(who, dt).call()), **{"from": liq})
            tags.append("ok" if st == "ok" else res)
        p(f"{pr / A:8.2f}  {pr / (1000.0993 * A):.4f}  {health(bob):>9}  {health(carol):>9}  {health(frank):>9}   {tags[0]:>20}  {tags[1]:>20}  {tags[2]:>20}")
    lab.set_price(1000.0)
    p("at P0, all healthy: bob", why(c.liquidate(bob, F564, FETH, 1), **{"from": liq}), "| dave (collateral == debt token):", why(c.liquidate(dave, F564, F564, 1), **{"from": liq}))
    lab.set_price(700.0)
    p("at 700 (bob liquidatable): maxRepay 0:", why(c.liquidate(bob, F564, FETH, 0), **{"from": liq}),
      "| maxRepay 1 wei:", why(c.liquidate(bob, F564, FETH, 1), **{"from": liq}), "| collateral token without a balance:", why(c.liquidate(bob, F564, F564, 1), **{"from": liq}))
    lab.set_price(1000.0)

    section("F. liquidation economics, long side (bob: 20 fETH collateral, f564 debt) at price 700")
    lab.set_price(700.0)
    pr = price()
    debt = c.debtOf(bob, F564).call()
    p("bob:", pos(bob))
    p("maxRepay = whole debt:", why(c.liquidate(bob, F564, FETH, debt), **{"from": liq}), "<- repaid / seized")
    p("maxRepay = 50% + 1:", why(c.liquidate(bob, F564, FETH, debt // 2 + 1), **{"from": liq}))
    p("maxRepay = 25%:", why(c.liquidate(bob, F564, FETH, debt // 4), **{"from": liq}))
    spent0, eth0 = START564 - bal(liq, F564), bal(liq, FETH)
    b564, beth, s564 = bal(liq, F564), bal(liq, FETH), lab.f564.functions.totalSupply().call()
    do(c.liquidate(bob, F564, FETH, debt), liq, "send liquidate(maxRepay = whole debt)")
    paid, got = b564 - bal(liq, F564), bal(liq, FETH) - beth
    p(f"liquidator paid {f(paid)} f564, received {f(got)} fETH = {f(got * pr // A)} f564 at TWAP -> premium {f(got * pr // A - paid)}"
      f" = {(got * pr // A) * 10000 // paid - 10000} bps | f564 supply change {f(lab.f564.functions.totalSupply().call() - s564)} (the repaid debt is burned)")
    p("bob after:", pos(bob))
    p("again at 700:", why(c.liquidate(bob, F564, FETH, c.debtOf(bob, F564).call()), **{"from": liq}))
    lab.set_price(680.0)
    do(c.liquidate(bob, F564, FETH, c.debtOf(bob, F564).call()), liq, "at 680: liquidate again (maxRepay = whole remaining debt)")
    p("bob after:", pos(bob))
    lab.set_price(660.0)
    do(c.liquidate(bob, F564, FETH, c.debtOf(bob, F564).call()), liq, "at 660: once more")
    p("bob after:", pos(bob), f"| liquidator: f564 spent {f(START564 - bal(liq, F564) - spent0)}, fETH gained {f(bal(liq, FETH) - eth0)}"
      f" -> profit at the TWAPs of each call {f((bal(liq, FETH) - eth0) * 0 + (START564 - bal(liq, F564) - spent0) // 20)} f564 (5%)")
    do(c.repay(F564, 2**256 - 1), bob, "bob repays the rest (max)")
    do(c.withdraw(FETH, 2**256 - 1), bob, "bob withdraws everything (max)")
    p("bob:", pos(bob))
    lab.set_price(1000.0)

    section("G. liquidation economics, short side (carol: f564 collateral, fETH debt) at price 1400")
    lab.set_price(1400.0)
    pr = price()
    p("carol:", pos(carol))
    p("maxRepay = whole debt:", why(c.liquidate(carol, FETH, F564, c.debtOf(carol, FETH).call()), **{"from": liq}), "<- repaid / seized")
    b564, beth, s_eth = bal(liq, F564), bal(liq, FETH), lab.feth.functions.totalSupply().call()
    do(c.liquidate(carol, FETH, F564, c.debtOf(carol, FETH).call()), liq, "send liquidate(maxRepay = whole debt)")
    paid, got = beth - bal(liq, FETH), bal(liq, F564) - b564
    p(f"liquidator paid {f(paid)} fETH ({f(paid * pr // A)} f564), received {f(got)} f564 -> premium {f(got - paid * pr // A)}"
      f" = {got * 10000 // (paid * pr // A) - 10000} bps | fETH supply change {f(lab.feth.functions.totalSupply().call() - s_eth)}")
    p("carol after:", pos(carol))
    lab.set_price(1000.0)

    section("H. bad debt (gina: 2.5x long with fETH collateral, price -> 1.0)")
    lab.set_price(1.0)
    debt = c.debtOf(gina, F564).call()
    p("gina:", pos(gina), "| badDebtOf before:", f(c.badDebtOf(gina, F564).call()), f(c.badDebtOf(gina, FETH).call()), "| borrowers", c.borrowerCount().call())
    p("liquidate(maxRepay = whole debt):", why(c.liquidate(gina, F564, FETH, debt), **{"from": liq}), "<- repaid / seized")
    b564, beth = bal(liq, F564), bal(liq, FETH)
    do(c.liquidate(gina, F564, FETH, debt), liq, "send")
    p(f"liquidator paid {f(b564 - bal(liq, F564))} f564, received {f(bal(liq, FETH) - beth)} fETH (all of it: the repayment is scaled down to the collateral)")
    p("gina after:", pos(gina), "| borrowers", c.borrowerCount().call())
    p("liquidate again:", why(c.liquidate(gina, F564, FETH, c.debtOf(gina, F564).call()), **{"from": liq}))
    p("gina repays (no debt left):", why(c.repay(F564, 1), **{"from": gina}))
    p("another account borrows while the bad debt is unsettled:", why(c.borrow(F564, 1), **{"from": dave}))
    p("settleBadDebt(gina) by a stranger:", lab.try_send(c.settleBadDebt(gina), **{"from": liq}), "| badDebtOf now",
      f(c.badDebtOf(gina, F564).call()), "| borrowers", c.borrowerCount().call())
    p("settleBadDebt(gina) again:", lab.try_send(c.settleBadDebt(gina), **{"from": liq}), "| market f564:", market(F564))
    p("mixed-collateral long (frank) at price 1.0:", pos(frank))
    p("  liquidate f564 debt against the fETH collateral:", why(c.liquidate(frank, F564, FETH, 2**256 - 1), **{"from": liq}), "<- repaid / seized")
    do(c.liquidate(frank, F564, FETH, 2**256 - 1), liq, "  send")
    p("  frank:", pos(frank))
    p("  liquidate against the f564 collateral (same token as the debt):", why(c.liquidate(frank, F564, F564, 2**256 - 1), **{"from": liq}))
    do(c.liquidate(frank, F564, F564, 2**256 - 1), liq, "  send")
    p("  frank:", pos(frank), "| badDebtOf", f(c.badDebtOf(frank, F564).call()))
    p("market f564:", market(F564))
    lab.set_price(1000.0)

    section("I. interest accrues on debt only (rate = f(market utilisation = debt / ceiling))")
    p("market f564:", market(F564), "| fETH:", market(FETH))
    c0 = c.debtOf(carol, FETH).call()
    d0 = c.debtOf(dave, F564).call()
    skip(86400)
    c1, d1 = c.debtOf(carol, FETH).call(), c.debtOf(dave, F564).call()
    p(f"after 1 day: carol fETH debt {f(c0, 8)} -> {f(c1, 8)} = +{(c1 - c0) / c0 * 365 * 100:.4f}%/yr (rate {f(c.market(FETH).call()[3], 6)}); "
      f"dave f564 debt {f(d0, 8)} -> {f(d1, 8)} = +{(d1 - d0) / d0 * 365 * 100:.4f}%/yr (rate {f(c.market(F564).call()[3], 6)})")
    p("carol after a day of interest (price unchanged):", pos(carol))
    skip(6 * 86400)
    p("after 7 days: carol fETH debt", f(c.debtOf(carol, FETH).call()), "health", health(carol))

    section("J. repay burns tokens")
    s = lab.f564.functions.totalSupply().call()
    p("repay 100:", why(c.repay(F564, 100 * A), **{"from": dave}))
    do(c.repay(F564, 100 * A), dave, "dave repays 100 f564")
    p("dave debt", f(c.debtOf(dave, F564).call()), "| f564 supply change", f(lab.f564.functions.totalSupply().call() - s))
    p("repay more than the debt:", why(c.repay(F564, 10_000 * A), **{"from": dave}))
    do(c.repay(F564, 10_000 * A), dave, "dave repays 10,000 (> debt)")
    p("dave:", pos(dave))
    p("repay 0:", why(c.repay(F564, 0), **{"from": dave}))

    section("K. guards")
    p("setDebtCeiling by stranger:", lab.try_send(c.setDebtCeiling(F564, 1), **{"from": liq}),
      "| setLiquidationBonus:", lab.try_send(c.setLiquidationBonus(1000), **{"from": liq}),
      "| pause:", lab.try_send(c.pause(), **{"from": liq}))
    lab.pool.functions.setLiquidity(0).transact()
    p("zero pool liquidity -> oraclePrice:", why(c.oraclePrice()), "| healthOf(carol):", why(c.healthOf(carol)))
    p("  deposit f564:", lab.try_send(c.deposit(F564, 100 * A), **{"from": dave}), "| repay:", lab.try_send(c.repay(F564, 1), **{"from": carol}),
      "| liquidate carol:", lab.try_send(c.liquidate(carol, FETH, F564, 1), **{"from": liq}))
    lab.pool.functions.setLiquidity(CORE_L).transact()
    lab.f564.functions.setPaused(True).transact()
    p("f564 TOKEN paused -> deposit f564:", lab.try_send(c.deposit(F564, 100 * A), **{"from": dave}),
      "| repay f564:", lab.try_send(c.repay(F564, 1), **{"from": dave}),
      "| withdraw f564:", lab.try_send(c.withdraw(F564, 1), **{"from": carol}))
    p("  borrow f564:", lab.try_send(c.borrow(F564, 1), **{"from": carol}))
    lab.f564.functions.setPaused(False).transact()
except Exception as exc:  # noqa: BLE001 — lab: show what broke and keep the output
    raise SystemExit(f"Lab failed: {type(exc).__name__}") from exc
