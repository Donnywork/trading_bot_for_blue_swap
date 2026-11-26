"""Single-pool Phase 3 spot trades after confirmed sales, using the registered TradingBot."""

import json
import time
from datetime import UTC, datetime

import requests
from web3 import Web3

import config
import operator_lock
import phase3_vault as vault
from phase3_chain import FEE, POOL, WAD
from phase3_monitor import CONTROL, alert
from phase_markets import PHASE3
from runtime import ConfigurationError

ADDRESS = Web3.to_checksum_address("0x418f86d4f99ce7fb7d045db23c933558072a02df")
RUNTIME_HASH = "0x75ba42696e75209c695a55a45d01787baef785ee6c0545b1c4554f083e48e042"
QUOTE, BASE = PHASE3.quote, PHASE3.base
# These amounts are suggestions for the unsigned owner calls. Live sizing reads the vault.
PROPOSED_SIZE = 1000 * WAD
MIN_SIZE = 125 * WAD
PROPOSED_EXIT_CAP = 3 * WAD // 2
LEGACY_ENTRY_CEILING = 250 * WAD
HOLD = 1800
ENTRY_DISCOUNT_BPS = 125
TRANSFER = Web3.keccak(text="Transfer(address,address,uint256)")


def inventory(directory):
    """Recover actual inventory from the same atomic receipt record that settled each send."""
    records = [
        json.loads(path.read_text())
        for path in (directory / "actions").glob("auto-*-spot-*.json")
    ]
    position = latest = None
    for row in sorted(records, key=lambda r: (r["block"], r["nonce"])):
        if (
            row["chain_id"] != vault.CHAIN_ID
            or row["to"] != ADDRESS
            or row["operator"] != vault.OPERATOR
        ):
            raise ConfigurationError("spot ledger identity mismatch")
        if row["status"] == "reverted":
            continue
        if row["status"] != "confirmed":
            raise ConfigurationError("unsettled spot receipt")
        fill = row["spot_fill"]
        if min(fill["spent"], fill["received"], fill["time"]) <= 0:
            raise ConfigurationError("invalid spot receipt amounts")
        if (
            fill["side"] == "buy"
            and position is None
            and MIN_SIZE
            <= fill["spent"]
            <= fill.get("quote_cap_raw", LEGACY_ENTRY_CEILING)
        ):
            position = fill
        elif (
            fill["side"] == "sell"
            and position
            and fill["spent"] == position["received"]
        ):
            position = None
        else:
            raise ConfigurationError("inconsistent spot position history")
        latest = row
    return position, latest


def confirmed_fill(receipt, side, amount, minimum):
    # Require actual full input and output from token Transfer logs. The legacy
    # vault event alone reports requested input even for a partial router fill.
    delta = {QUOTE.lower(): 0, BASE.lower(): 0}
    for log in receipt["logs"]:
        token, topics = log["address"].lower(), log["topics"]
        if token not in delta or len(topics) != 3 or topics[0] != TRANSFER:
            continue
        value = int.from_bytes(log["data"], "big")
        for sign, topic in [(-1, topics[1]), (1, topics[2])]:
            if bytes(topic)[-20:] == bytes.fromhex(ADDRESS[2:]):
                delta[token] += sign * value
    src, dst = (QUOTE, BASE) if side == "buy" else (BASE, QUOTE)
    received = delta[dst.lower()]
    if -delta[src.lower()] != amount or received < minimum:
        raise ConfigurationError(
            "spot receipt differs from trade; reconcile pending intent"
        )
    return {"side": side, "spent": amount, "received": received}


def discounted(sqrt_price, oracle):
    price, window = oracle
    # token0 is f564. A larger pool sqrt price means a cheaper fETH.
    return (
        window >= 1800
        and price > 0
        and sqrt_price > 0
        and (1 << 192) * WAD * 10000
        <= sqrt_price**2 * price * (10000 - ENTRY_DISCOUNT_BPS)
    )


def exit_reason(position, proceeds, timestamp):
    if proceeds * 10000 >= position["spent"] * 10050:
        return "take_profit"
    if proceeds * 10000 <= position["spent"] * 9850:
        return "stop_loss"
    if timestamp - position["time"] >= HOLD:
        return "time_stop"
    return None


def entry_open(directory, now):
    # The unscoped monitor supplies phase and market identity. Wallet permissions
    # are address-specific and must come from the address-scoped site response.
    s = json.loads((directory / "state.json").read_text())
    site = s.get("site", {})
    return (
        s.get("health") == "ready"
        and 0 <= now - s["updated"] <= 90
        and "site_error" not in s
        and 0 <= now - s["site_checked_at"] <= 360
        and site.get("phase") == "phase-3"
        and site.get("pricingPool", "").lower() == POOL.lower()
        and site.get("status") == "running"
    )


def eligible_vault():
    reply = requests.get(CONTROL, params={"address": ADDRESS}, timeout=4)
    reply.raise_for_status()
    site = reply.json()
    phase = site.get("phase") or {}
    return (
        site.get("participantEnabled") is True
        and site.get("tradingEnabled") is True
        and site.get("status") == "running"
        and phase.get("id") == "phase-3"
        and phase.get("pricingPool", "").lower() == POOL.lower()
    )


def owner_transactions():
    """Unsigned calls in signing order, with funds sourced from the enrolled owner's vault."""
    trading = Web3().eth.contract(abi=spot_abi())
    credit = Web3().eth.contract(abi=json.loads(vault.RELEASE.read_text())["abi"])
    token = Web3().eth.contract(
        abi=[
            {
                "type": "function",
                "name": "transfer",
                "inputs": [
                    {"name": "to", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "outputs": [{"name": "", "type": "bool"}],
            }
        ]
    )
    for dest, contract, method, args in [
        (ADDRESS, trading, "setTokenCap", [QUOTE, PROPOSED_SIZE]),
        (ADDRESS, trading, "setTokenCap", [BASE, PROPOSED_EXIT_CAP]),
        (
            "0x346B0c324963B91afb6f722d126E90506623bB8d",
            credit,
            "withdrawToOwner",
            [QUOTE, PROPOSED_SIZE],
        ),
        (QUOTE, token, "transfer", [ADDRESS, PROPOSED_SIZE]),
    ]:
        yield {
            "chainId": vault.CHAIN_ID,
            "to": dest,
            "value": 0,
            "data": contract.encode_abi(method, args=args),
        }


def spot_abi():
    return json.loads((config.ROOT / "abis/phase3_spot.json").read_text())


class Spot:
    def __init__(self, trader):
        self.trader = trader
        self.chain = trader.chain
        self.directory = trader.directory
        self.chain.contracts["spot"] = self.chain.w3.eth.contract(
            address=ADDRESS, abi=spot_abi()
        )
        self.checked = False

    def step(self, block):
        from phase3_auto import END

        block, block_hash, timestamp = self.chain.header(block)
        if not self.checked:
            code = self.chain.w3.eth.get_code(ADDRESS, block_identifier=block)
            if Web3.keccak(code).to_0x_hex() != RUNTIME_HASH:
                raise ConfigurationError("spot vault bytecode mismatch")
            # The fixed runtime hash includes the official router immutable.
            self.checked = True
        position, last = inventory(self.directory)
        if last and (
            block < last["block"]
            or self.chain.header(last["block"])[1] != last["block_hash"]
        ):
            raise ConfigurationError("spot fill not canonical; reconcile spot ledger")
        owner, operator, fee, qcap, bcap, cash, base, slot, oracle = self.chain.views(
            block,
            [
                ("spot", "owner"),
                ("spot", "operator"),
                ("spot", "feeAllowed", FEE),
                ("spot", "tokenCap", QUOTE),
                ("spot", "tokenCap", BASE),
                ("f564", "balanceOf", ADDRESS),
                ("fETH", "balanceOf", ADDRESS),
                ("pool", "slot0"),
                ("credit", "oraclePrice"),
            ],
        )
        if (
            owner.lower() != vault.OWNER.lower()
            or operator.lower() != vault.OPERATOR.lower()
        ):
            raise ConfigurationError("spot vault role mismatch")
        state = {
            "vault": ADDRESS,
            "position": position,
            "quote_balance_raw": cash,
            "quote_cap_raw": qcap,
            "base_cap_raw": bcap,
        }
        if position:
            if not fee or qcap == 0 or min(base, bcap) < position["received"]:
                return state | {"status": "exit_needs_balance_or_owner_cap"}
            side, amount = "sell", position["received"]
        else:
            if not fee or min(qcap, cash) < MIN_SIZE or bcap == 0:
                return state | {"status": "awaiting_funding_or_caps"}
            if not entry_open(self.directory, time.time()):
                return state | {"status": "awaiting_competition_trading"}
            if timestamp + HOLD + 120 >= END or (
                last and timestamp - last["spot_fill"]["time"] < HOLD
            ):
                return state | {"status": "entry_cooldown"}
            if not discounted(slot[0], oracle):
                return state | {"status": "watching"}
            logs = (
                self.chain.contracts["pool"]
                .events.Swap()
                .get_logs(from_block=block - 6, to_block=block)
            )
            if (
                not logs
                or logs[-1]["args"]["amount1"] <= 0
                or logs[-1]["blockNumber"] >= block
            ):
                return state | {"status": "waiting_for_confirmed_sale"}
            if not eligible_vault():
                return state | {"status": "awaiting_competition_trading"}
            side, amount = "buy", min(qcap, cash)
        tip = self.chain.contracts["spot"]
        src, dst = (QUOTE, BASE) if side == "buy" else (BASE, QUOTE)
        args = [src, dst, FEE, amount]
        sender = {"from": vault.OPERATOR}
        quote = tip.functions.swap(*args, 1).call(sender, block_identifier=block)
        minimum = max(1, quote * 9990 // 10000)
        if side == "sell":
            reason = exit_reason(position, quote, timestamp)
            if not reason:
                return state | {"status": "holding", "quoted_exit_raw": quote}
            if reason == "take_profit":
                minimum = max(minimum, (position["spent"] * 10050 + 9999) // 10000)
        else:
            reason = "confirmed_dip"
            # Even the minimum fill must cover the future 30 bp exit fee
            # and target profit at TWAP. Recovery and exit depth are uncertain.
            if (
                quote * 110 > bcap * 100
                or minimum * oracle[0] * 9970 < amount * WAD * 10050
            ):
                return state | {"status": "insufficient_recovery_margin"}
        data = tip.encode_abi("swap", args=args + [minimum])
        key = logs[-1]["transactionHash"].hex() if side == "buy" else str(block)
        detail = {
            "side": side,
            "reason": reason,
            "input_raw": amount,
            "minimum_raw": minimum,
        }
        if side == "buy":
            detail["quote_cap_raw"] = qcap
        if not self.trader.live:
            return state | detail | {"status": "simulated"}
        status = self.execute(block, block_hash, timestamp, key, data, detail)
        return state | detail | status

    def execute(self, block, block_hash, timestamp, key, data, detail):
        from phase3_auto import END, fee_budget, fresh

        w3 = self.trader.sender
        side = detail["side"]
        amount = detail["input_raw"]
        minimum = detail["minimum_raw"]
        tx = {
            "from": vault.OPERATOR,
            "to": ADDRESS,
            "data": data,
            "value": 0,
            "chainId": vault.CHAIN_ID,
        }
        with operator_lock.sender(self.directory.parent, "manual"):
            day = datetime.now(UTC).strftime("%Y%m%d")
            dest = self.directory / "actions" / f"auto-{day}-spot-{side}-{key}.json"
            if dest.exists():
                return {"status": "already_attempted"}
            if (
                w3.eth.chain_id != vault.CHAIN_ID
                or Web3.to_hex(w3.eth.get_block("latest").hash) != block_hash
            ):
                return {"status": "quote_expired"}
            (actual,) = w3.codec.decode(
                ["uint256"], w3.eth.call(tx, block_identifier=block)
            )
            if actual < minimum:
                return {"status": "quote_moved"}

            def final_check(cost):
                fee_budget(self.directory, day, cost)
                now = time.time()
                if (
                    not fresh(timestamp, now)
                    or now + 13 >= END
                    or Web3.to_hex(w3.eth.get_block("latest").hash) != block_hash
                    or (side == "buy" and not entry_open(self.directory, now))
                ):
                    raise ConfigurationError("spot submission window closed")

            record = vault.broadcast(
                w3,
                self.trader.signer,
                tx,
                dest,
                self.directory / "pending.json",
                None,
                before_intent=final_check,
                account_fill=lambda r: (
                    confirmed_fill(r, side, amount, minimum)
                    | (
                        {"quote_cap_raw": detail["quote_cap_raw"]}
                        if side == "buy"
                        else {}
                    )
                ),
            )
            alert(
                f"Phase 3 spot {side} confirmed ({detail['reason']}) in block {record['block']}, tx {record['tx_hash']}"
            )
            return {"status": "confirmed", "tx_hash": record["tx_hash"]}


if __name__ == "__main__":
    print(json.dumps(list(owner_transactions()), indent=2))
