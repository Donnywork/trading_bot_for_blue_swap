"""Does settleBadDebt() lift the borrower's UnsettledBadDebt lock?  (offline lab)

    uv run python research/2026-11-24-phase3/lab/phase3_settle_probe.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase3_credit_labkit import A, Lab  # noqa: E402

lab = Lab()
c, w3 = lab.c, lab.w3
alice, bob, carol = lab.alice, lab.bob, lab.carol
F564, FETH = lab.f564.address, lab.feth.address
lab.pool.functions.setLiquidity(57_341_279_504_686_431_094_117).transact()
lab.pool.functions.setCardinality(180).transact()
out = []


def p(*x):
    line = " ".join(str(v) for v in x)
    out.append(line)
    print(line)


def why(fn, **kw):
    status, res = lab.try_call(fn, **kw)
    return res if status == "revert" else "ok"


def state(who):
    return (f"collateral {c.collateralOf(who, FETH).call() / A:.4f} fETH / {c.collateralOf(who, F564).call() / A:.4f} f564, "
            f"debt {c.debtOf(who, F564).call() / A:.4f}, badDebt {c.badDebtOf(who, F564).call() / A:.4f}, borrowers {c.borrowerCount().call()}")


c.deposit(FETH, 20 * A).transact({"from": bob})
c.borrow(F564, 11_900 * A).transact({"from": bob})
lab.set_price(1.0)
c.liquidate(bob, F564, FETH, 2**256 - 1).transact({"from": alice})
lab.set_price(1000.0)
p("after the bad-debt liquidation:", state(bob))
p("borrow:", why(c.borrow(F564, 1), **{"from": bob}))
p("settleBadDebt with no collateral:", lab.try_send(c.settleBadDebt(bob), **{"from": carol}), "->", state(bob))
p("borrow (no collateral):", why(c.borrow(F564, 1), **{"from": bob}))
c.deposit(F564, 1_000 * A).transact({"from": bob})
p("after depositing 1,000 f564:", state(bob))
p("borrow:", why(c.borrow(F564, 1), **{"from": bob}), "| withdraw:", why(c.withdraw(F564, 1), **{"from": bob}),
  "| repay:", why(c.repay(F564, 1), **{"from": bob}), "| liquidate:", why(c.liquidate(bob, F564, F564, 1), **{"from": carol}))
p("settleBadDebt with collateral:", lab.try_send(c.settleBadDebt(bob), **{"from": carol}))
p("market f564 debt", c.market(F564).call()[0] / A, "| carol borrows:", why(c.borrow(F564, 1), **{"from": carol}))
