"""Watch fresh Sepolia heads and automatically close profitable Phase 3 liquidations."""

import argparse
import json
import os
import time
from datetime import UTC, datetime

from web3 import Web3
from web3.exceptions import ContractLogicError

import arbitrage
import config
import ledger
import operator_lock
import phase3_vault as vault_cli
from phase3_chain import PUBLIC_RPC, WAD, Chain
from phase3_monitor import alert
from phase3_spot import Spot
from phase_markets import PHASE3
from runtime import ConfigurationError, error_kind, load_environment

TOKENS = (PHASE3.quote, PHASE3.base)
SYMBOLS = ("f564", "fETH")
CAPS = (1000 * WAD, WAD)
RESERVES = (6000 * WAD, 6 * WAD)
END = 1796716945  # Scheduled end of Phase 3, UTC.
MAX_GAS_COST = 10**15
DAILY_GAS = 10 * MAX_GAS_COST


def capacity(balance, limits, index):
    _, reserve, cap = limits
    return min(CAPS[index], cap, max(0, balance - max(reserve, RESERVES[index])))


def plan_lot(
    vault,
    operator,
    block,
    borrower,
    debt,
    collateral,
    maximum,
    sqrt_limit,
    price,
    deadline,
    quote,
):
    # Preview the actual liquidation AND exit. This includes facility rounding,
    # close factor, available collateral, current liquidity, fees and price impact.
    minimum = 10 * WAD if debt == quote else (10 * WAD**2 + price - 1) // price
    for amount in dict.fromkeys([maximum, maximum // 2, maximum // 4]):
        if amount <= 0:
            continue
        lot = (borrower, debt, collateral, amount, 1, deadline)
        try:
            gain = vault.functions.liquidateAndClose(lot, minimum, sqrt_limit).call(
                {"from": operator}, block_identifier=block
            )
        except ContractLogicError:
            continue
        if gain < minimum:
            continue
        args = [lot, max(minimum, gain * 95 // 100), sqrt_limit]
        return {
            "args": args,
            "gain": gain,
            "data": vault.encode_abi("liquidateAndClose", args=args),
        }
    return None


def fresh(timestamp, now):
    return -5 <= now - timestamp <= 36


def fee_budget(directory, day, worst_case):
    if worst_case > MAX_GAS_COST:
        raise ConfigurationError("operator transaction gas budget exceeded")
    spent = sum(
        json.loads(p.read_text())["gas_cost_wei"]
        for p in (directory / "actions").glob(f"auto-{day}-*.json")
    )
    if spent + worst_case > DAILY_GAS:
        raise ConfigurationError("daily operator gas budget exceeded")


class Trader:
    def __init__(self, live, directory):
        self.directory = directory
        self.live = live
        self.chain = Chain(os.environ.get("PHASE3_RPC_URL") or PUBLIC_RPC)
        self.artifact = json.loads(vault_cli.RELEASE.read_text())
        self.address = vault_cli.binding(directory)
        self.validated = False
        self.last_hash = None
        self.signer = self.sender = None
        if live:
            if os.environ.get("DRY_RUN", "true").lower() != "false":
                raise ConfigurationError("live execution requires DRY_RUN=false")
            self.sender = Web3(config.rpc_provider(os.environ["RPC_URL"]))
            self.signer = self.sender.eth.account.from_key(os.environ["PRIVATE_KEY"])
            if self.signer.address != vault_cli.OPERATOR:
                raise ConfigurationError("operator signer mismatch")
        token_abi = json.loads((config.ROOT / "abis/erc20.json").read_text())
        for label, token in zip(SYMBOLS, TOKENS, strict=True):
            self.chain.contracts[label] = self.chain.w3.eth.contract(
                address=token, abi=token_abi
            )

    def step(self):
        block, block_hash, timestamp = self.chain.header("latest")
        if not fresh(timestamp, time.time()):
            raise ConfigurationError("waiting for fresh Sepolia head")
        if self.last_hash == block_hash:
            return None
        if not self.validated:
            self.chain.validate(block)
            self.vault = vault_cli.verify(
                self.chain.w3, self.artifact, self.address, block
            )
            self.chain.contracts["vault"] = self.vault
            self.validated = True
        paused, count, *limits = self.chain.views(
            block,
            [
                ("credit", "paused"),
                ("credit", "borrowerCount"),
                *(("vault", "limits", t) for t in TOKENS),
            ],
        )
        state = {
            "block": block,
            "borrower_count": count,
            "liquidation_caps_raw": {
                label: value[2] for label, value in zip(SYMBOLS, limits, strict=True)
            },
        }
        self.last_hash = block_hash
        if paused:
            return state | {"status": "facility_paused"}
        if not any(lim[2] for lim in limits):
            return state | {"status": "awaiting_owner_limits"}
        accounts = [
            Web3.to_checksum_address(a)
            for a in self.chain.views(
                block, (("credit", "borrowerAt", i) for i in range(count))
            )
        ]
        health = self.chain.views(block, (("credit", "healthOf", a) for a in accounts))
        eligible = [
            a
            for a, h in zip(accounts, health, strict=True)
            if h < WAD and a not in (self.address, vault_cli.OWNER)
        ]
        if not eligible:
            return state | {"status": "watching"}
        oracle, slot, *balances = self.chain.views(
            block,
            [
                ("credit", "oraclePrice"),
                ("pool", "slot0"),
                *((label, "balanceOf", self.address) for label in SYMBOLS),
            ],
        )
        price, window = oracle
        if price <= 0 or window < 1800:
            return state | {"status": "oracle_unavailable"}
        for borrower in eligible:
            debts = self.chain.views(
                block, [("credit", "debtOf", borrower, t) for t in TOKENS]
            )
            collaterals = self.chain.views(
                block, [("credit", "collateralOf", borrower, t) for t in TOKENS]
            )
            for i, debt in enumerate(TOKENS):
                maximum = min(capacity(balances[i], limits[i], i), debts[i] // 2)
                if not maximum:
                    continue
                for j in (i, 1 - i):
                    if not collaterals[j]:
                        continue
                    limit = 0 if i == j else arbitrage.price_limit(slot[0], j == 0, 100)
                    deadline = min(timestamp + 48, END)
                    plan = plan_lot(
                        self.vault,
                        vault_cli.OPERATOR,
                        block,
                        borrower,
                        debt,
                        TOKENS[j],
                        maximum,
                        limit,
                        price,
                        deadline,
                        TOKENS[0],
                    )
                    if plan:
                        state.update(
                            borrower=borrower,
                            debt_token=debt,
                            simulated_gain_raw=plan["gain"],
                        )
                        if not self.live:
                            return state | {"status": "simulated"}
                        return state | self.execute(plan, block, block_hash, deadline)
        return state | {"status": "no_profitable_close"}

    def execute(self, plan, block, block_hash, deadline):
        w3 = self.sender
        with operator_lock.sender(self.directory.parent, "manual"):
            head = w3.eth.get_block("latest")
            if (
                not 0 <= head.number - block <= 2
                or not fresh(head.timestamp, time.time())
                or Web3.to_hex(w3.eth.get_block(block).hash) != block_hash
            ):
                return {"status": "quote_expired"}
            vault_cli.verify(w3, self.artifact, self.address, head.number)
            day = datetime.now(UTC).strftime("%Y%m%d")
            dest = self.directory / "actions" / f"auto-{day}-{block_hash[2:]}.json"
            if dest.exists():
                return {"status": "already_processed"}
            tx = {
                "from": vault_cli.OPERATOR,
                "to": self.address,
                "data": plan["data"],
                "value": 0,
                "chainId": vault_cli.CHAIN_ID,
            }
            # Re-simulate the tightened calldata on the actual sending provider.
            try:
                (gain,) = w3.codec.decode(
                    ["uint256"], w3.eth.call(tx, block_identifier=head.number)
                )
            except ContractLogicError:
                return {"status": "opportunity_moved"}
            if gain < plan["args"][1]:
                return {"status": "opportunity_moved"}

            def final_check(cost):
                fee_budget(self.directory, day, cost)
                if (
                    w3.eth.chain_id != vault_cli.CHAIN_ID
                    or time.time() + 13 >= deadline
                ):
                    raise ConfigurationError("submission window expired")

            record = vault_cli.broadcast(
                w3,
                self.signer,
                tx,
                dest,
                self.directory / "pending.json",
                self.artifact,
                before_intent=final_check,
            )
            alert(
                f"Atomic liquidation confirmed in block {record['block']}, tx {record['tx_hash']}"
            )
            return {"status": "confirmed", "tx_hash": record["tx_hash"]}


def run(live, once=False, *, with_spot=False):
    directory = vault_cli.DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("auto.json" if live else "auto-simulation.json")
    previous = None
    trader = None
    spot = None
    while True:
        terminal = False
        try:
            if live and any(
                p.exists()
                for p in (
                    directory / "pending.json",
                    directory.parent / "HALTED",
                    directory.parent / "events/HALTED",
                )
            ):
                state, terminal = {"status": "halted_reconcile"}, True
            elif time.time() >= END:
                state, terminal = {"status": "phase_ended"}, True
            else:
                trader = trader or Trader(live, directory)
                state = trader.step()
                if with_spot and state is not None:
                    if state["status"] in ("confirmed", "simulated"):
                        state["spot"] = {"status": "liquidation_priority"}
                    else:
                        try:
                            spot = spot or Spot(trader)
                            state["spot"] = spot.step(state["block"])
                        except Exception as exc:
                            if (directory / "pending.json").exists():
                                raise
                            state["spot"] = {
                                "status": "degraded",
                                "reason": str(exc)
                                if isinstance(exc, ConfigurationError)
                                else error_kind(exc),
                            }
                            spot = None
        except Exception as exc:  # noqa: BLE001 — persist fixed diagnostics, never RPC bodies
            terminal = live and (directory / "pending.json").exists()
            state = {
                "status": "halted_reconcile" if terminal else "degraded",
                "reason": str(exc)
                if isinstance(exc, ConfigurationError)
                else error_kind(exc),
            }
            trader = None
            spot = None
        if state is not None:
            ledger.atomic_write(
                path,
                json.dumps(
                    state | {"live": live, "updated": int(time.time())}, indent=2
                ),
            )
            if state["status"] != previous:
                print(json.dumps(state), flush=True)
                if terminal and live:
                    alert(f"Phase 3 autotrader stopped ({state['status']})")
                previous = state["status"]
        if terminal or once:
            return
        time.sleep(4 if previous != "degraded" else 15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--with-spot",
        action="store_true",
        help="also run the registered single-pool spot strategy",
    )
    args = parser.parse_args()
    if args.live:
        load_environment()
    with operator_lock.acquire(
        vault_cli.DIRECTORY / ("auto.lock" if args.live else "auto-simulation.lock")
    ):
        run(args.live, args.once, with_spot=args.with_spot)
