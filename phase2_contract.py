"""Build/prepare one Phase 2 trading contract; only the explicit deploy command sends."""

import argparse
import json
import os
from pathlib import Path

import solcx
from web3 import Web3

import bot_contract
import config
import event_market as market
import ledger
import operator_lock
from event_settings import token_amount
from runtime import ConfigurationError, error_summary
from runtime import load_environment as load_dotenv

SOURCE = "contracts/Phase2TradingBot.sol"
ARTIFACT = config.ROOT / "build/Phase2TradingBot.artifact.json"


def standard_json_input():
    build = bot_contract.standard_json_input()
    # Preserve source paths/content for existing deployments' Etherscan verification.
    for path in ("contracts/ArbitrageVault.sol", SOURCE):
        build["sources"][path] = {"content": (config.ROOT / path).read_text()}
    return build


def compile_contract(build=None):
    result = solcx.compile_standard(
        build or standard_json_input(), solc_version=bot_contract.SOLC_VERSION
    )
    return result["contracts"][SOURCE]["Phase2TradingBot"]


def build_files():
    build = standard_json_input()
    artifact = compile_contract(build)
    ARTIFACT.parent.mkdir(exist_ok=True)
    ledger.atomic_write(ARTIFACT, json.dumps(artifact, indent=2))
    ledger.atomic_write(
        ARTIFACT.parent / "Phase2TradingBot.standard-input.json",
        json.dumps(build, indent=2),
    )
    return artifact


def deployment_plan(owner, operator, limits, artifact):
    owner, operator = (
        Web3.to_checksum_address(market.hex_value(a.lower(), 20))
        for a in (owner, operator)
    )
    if int(owner, 16) == 0 or int(operator, 16) == 0 or owner == operator:
        raise ValueError("distinct nonzero owner/operator addresses required")
    if len(limits) != 3 or any(
        type(n) is not int or not 0 < n < 2**256 for n in limits
    ):
        raise ValueError("three positive raw limits required")
    args = [
        owner,
        operator,
        Web3.to_checksum_address(market.ROUTER),
        Web3.to_checksum_address(market.QUOTE),
        Web3.to_checksum_address(market.BASE),
        [Web3.to_checksum_address(market.POOLS[f]) for f in (500, 10000)],
        list(limits),
    ]
    factory = Web3().eth.contract(
        abi=artifact["abi"], bytecode=artifact["evm"]["bytecode"]["object"]
    )
    data = factory.constructor(*args).data_in_transaction
    return {
        "contract": "Phase2TradingBot",
        "chain_id": 11155111,
        "owner": owner,
        "operator": operator,
        "limits_raw": list(limits),
        "constructor": args,
        "data": data,
        "constructor_arguments": data[2 + len(artifact["evm"]["bytecode"]["object"]) :],
        "value": 0,
    }


def deploy(plan, record_path):
    # Runtime-only key access. The agent's offline build/tests never call this against a real RPC.
    if record_path.exists():
        raise ConfigurationError(
            "deployment record already exists; reconcile before deploying"
        )
    w3 = Web3(config.rpc_provider(os.environ["RPC_URL"]))
    if w3.eth.chain_id != 11155111:
        raise ValueError("deployment requires Sepolia")
    account = w3.eth.account.from_key(os.environ["PRIVATE_KEY"])
    if account.address != plan["operator"] or account.address == plan["owner"]:
        raise ValueError("deployment key must be the planned operator, never the owner")
    from event_snapshot import markets

    markets(
        w3
    )  # Verify the fixed Phase 2 tokens, canonical pools, router and quoter first.
    with operator_lock.sender(ledger.LOG_DIR, "manual"):
        tx = {
            "from": account.address,
            "chainId": 11155111,
            "data": plan["data"],
            "value": 0,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        }
        tx["gas"] = w3.eth.estimate_gas(tx)
        print("Deploy ONE Phase2TradingBot on Sepolia")
        print("Owner:", plan["owner"], "Operator:", plan["operator"])
        print("Raw quote/base/profit limits:", plan["limits_raw"])
        if input("Type DEPLOY to sign this deployment: ") != "DEPLOY":
            raise ValueError("deployment cancelled")
        if w3.eth.get_transaction_count(account.address, "pending") != tx["nonce"]:
            raise ValueError("operator nonce changed during review; prepare again")
        record = {
            "status": "intent",
            "operator": account.address,
            "nonce": tx["nonce"],
            "plan": plan,
        }
        record_path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse reuse of a record even after an uncertain outcome: reconcile first.
        with record_path.open("x") as stream:
            stream.write(json.dumps(record))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            receipt = bot_contract.send_tx(w3, account, tx)
            if (
                receipt["status"] != 1
                or not receipt["contractAddress"]
                or w3.eth.get_block(receipt["blockNumber"])["hash"]
                != receipt["blockHash"]
                or not w3.eth.get_code(receipt["contractAddress"])
            ):
                raise ValueError("deployment receipt or code could not be verified")
        except Exception:  # noqa: BLE001 — never expose provider errors or retry a deployment
            print(
                "Deployment outcome requires reconciliation. Do not deploy again; inspect the recorded operator nonce."
            )
            raise SystemExit(1) from None
        record.update(
            status="confirmed",
            address=receipt["contractAddress"],
            tx_hash=Web3.to_hex(receipt["transactionHash"]),
        )
        ledger.atomic_write(record_path, json.dumps(record, indent=2))
        print("BOT_CONTRACT_ADDRESS=" + receipt["contractAddress"])
        print(
            "Contract starts paused. Register/fund it, then the owner enables trading."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    prepare = sub.add_parser(
        "prepare", help="offline unsigned deployment plan; refuses overwrite"
    )
    prepare.add_argument("--owner", required=True)
    prepare.add_argument("--operator", required=True)
    for key in ("quote-cap", "base-cap", "min-profit"):
        prepare.add_argument("--" + key, type=token_amount, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    send = sub.add_parser("deploy", help="sign and deploy the reviewed plan on Sepolia")
    send.add_argument("--plan", type=Path, required=True)
    send.add_argument("--record", type=Path, required=True)
    send.add_argument("--load-env", action="store_true", help=argparse.SUPPRESS)
    send.add_argument(
        "--no-env", action="store_true", help="use exported environment only"
    )
    calldata = sub.add_parser("calldata", help="offline owner calldata; never signs")
    calldata.add_argument("--address", required=True)
    calldata.add_argument(
        "function",
        choices=(
            "setPaused",
            "setLimits",
            "setOperator",
            "withdraw",
            "withdrawETH",
            "transferOwnership",
            "acceptOwnership",
        ),
    )
    calldata.add_argument("args", nargs="*")
    args = parser.parse_args()
    if args.command == "calldata":
        abi = json.loads(ARTIFACT.read_text())["abi"]
        bot = Web3().eth.contract(
            address=Web3.to_checksum_address(args.address), abi=abi
        )
        spec = next(row for row in abi if row.get("name") == args.function)
        if len(args.args) != len(spec["inputs"]):
            parser.error("wrong number of owner arguments")
        values = []
        for value, field in zip(args.args, spec["inputs"], strict=True):
            if field["type"] == "bool":
                if value not in ("true", "false"):
                    parser.error("boolean must be true or false")
                values.append(value == "true")
            elif field["type"] == "address":
                values.append(Web3.to_checksum_address(value))
            else:
                values.append(int(value))
        bot_contract.print_calldata(bot, args.function, values)
        return
    if args.command == "build":
        build_files()
        print("Built Phase2TradingBot in build/; offline, not deployed.")
    elif args.command == "prepare":
        if args.output.exists():
            raise ConfigurationError(
                "deployment plan already exists; choose a new output path"
            )
        artifact = compile_contract()
        plan = deployment_plan(
            args.owner,
            args.operator,
            [args.quote_cap, args.base_cap, args.min_profit],
            artifact,
        )
        with args.output.open("x") as stream:
            stream.write(json.dumps(plan, indent=2) + "\n")
        print("Unsigned deployment plan saved; no network, key loading, or signing.")
    else:
        if args.load_env and args.no_env:
            parser.error("--load-env and --no-env cannot be combined")
        if args.record.exists():
            raise ConfigurationError(
                "deployment record already exists; reconcile it before any further deployment"
            )
        plan = json.loads(args.plan.read_text())
        artifact = compile_contract()
        expected = deployment_plan(
            plan["owner"], plan["operator"], plan["limits_raw"], artifact
        )
        if plan != expected:
            raise ConfigurationError(
                "deployment plan differs from current source/Phase 2 configuration; prepare and review again"
            )
        if not args.no_env:
            load_dotenv()
        deploy(plan, args.record)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — CLI boundary must not print provider bodies
        print(f"Phase 2 contract command failed: {error_summary(exc)}")
        raise SystemExit(1) from None
