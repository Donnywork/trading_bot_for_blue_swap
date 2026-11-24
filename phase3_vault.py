"""Build, verify and operate the single-pool Phase 3 vault on Sepolia."""

import argparse
import json
import os
import re
from pathlib import Path

from web3 import Web3

import bot_contract
import config
import event_market
import ledger
import operator_lock
from phase3_chain import Chain
from phase_markets import PHASE3
from runtime import ConfigurationError, error_kind, load_environment

SOURCE = "contracts/Phase3Vault.sol"
RELEASE = config.ROOT / "abis/phase3_vault.artifact.json"
DIRECTORY = ledger.LOG_DIR / "phase3"
OWNER = Web3.to_checksum_address("0xb6bf0ba3f43d4386216df037dca1a1203038e135")
OPERATOR = Web3.to_checksum_address("0x9cb82edacbda4bdb053a3e7d5eccd6798addc731")
CHAIN_ID = 11155111
OPS = {
    "deposit",
    "repay",
    "borrow",
    "withdrawCollateral",
    "liquidate",
    "liquidateAndClose",
}
ADMIN = {
    "setLimits",
    "setBorrowingEnabled",
    "setOperator",
    "withdrawToOwner",
    "transferOwnership",
    "acceptOwnership",
}


def compile_vault(build=None):
    import solcx

    build = build or build_input()
    contracts = solcx.compile_standard(build, solc_version=bot_contract.SOLC_VERSION)[
        "contracts"
    ]
    return contracts[SOURCE]["Phase3Vault"]


def build_input():
    build = bot_contract.standard_json_input()
    build["sources"][SOURCE] = {"content": (config.ROOT / SOURCE).read_text()}
    return build


def plan(artifact):
    args = [
        OWNER,
        OPERATOR,
        PHASE3.credit,
        Web3.to_checksum_address(event_market.ROUTER),
        PHASE3.quote,
        PHASE3.base,
        PHASE3.pools[0][1],
    ]
    factory = Web3().eth.contract(
        abi=artifact["abi"], bytecode=artifact["evm"]["bytecode"]["object"]
    )
    return factory.constructor(*args).data_in_transaction


def verify(w3, artifact, address, block, *, initial=False):
    if w3.eth.chain_id != CHAIN_ID:
        raise ConfigurationError("Phase 3 vault requires Sepolia")
    runtime = artifact["evm"]["deployedBytecode"]
    expected = bytearray.fromhex(runtime["object"])
    actual = bytearray(w3.eth.get_code(address, block_identifier=block))
    if len(actual) != len(expected):
        raise ConfigurationError("vault code length mismatch")
    for refs in runtime["immutableReferences"].values():
        for ref in refs:
            span = slice(ref["start"], ref["start"] + ref["length"])
            actual[span] = expected[span]
    if actual != expected:
        raise ConfigurationError("vault bytecode mismatch")
    vault = w3.eth.contract(address=address, abi=artifact["abi"])
    bindings = {
        "owner": OWNER,
        "operator": OPERATOR,
        "quote": PHASE3.quote,
        "base": PHASE3.base,
        "pool": PHASE3.pools[0][1],
        "credit": PHASE3.credit,
        "router": Web3.to_checksum_address(event_market.ROUTER),
        "maxLtvBps": 4000,
    }
    if initial:
        bindings["borrowingEnabled"] = False
    for key, value in bindings.items():
        if getattr(vault.functions, key)().call(block_identifier=block) != value:
            raise ConfigurationError(f"vault {key} mismatch")
    if initial:
        for token, reserve in ((PHASE3.quote, 1000 * 10**18), (PHASE3.base, 10**18)):
            if tuple(vault.functions.limits(token).call(block_identifier=block)) != (
                0,
                reserve,
                0,
            ):
                raise ConfigurationError("unexpected initial vault limits")
    return vault


def requested(path, vault, choices):
    request = json.loads(path.read_text())
    if set(request) != {"id", "function", "args"} or not isinstance(
        request["args"], list
    ):
        raise ConfigurationError("expected id, function and args")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request["id"])
        or request["function"] not in choices
    ):
        raise ConfigurationError("invalid request ID or function")
    return request["id"], vault.encode_abi(request["function"], args=request["args"])


def binding(directory):
    record = json.loads((directory / "deployment.json").read_text())
    if record["status"] != "confirmed" or record["chain_id"] != CHAIN_ID:
        raise ConfigurationError("verified Phase 3 deployment required")
    return Web3.to_checksum_address(record["address"])


def broadcast(w3, signer, tx, destination, pending, artifact):
    if destination.exists() or pending.exists():
        raise ConfigurationError(
            "deployment or request already recorded; reconcile before sending"
        )
    estimate = w3.eth.estimate_gas(tx)
    tx = dict(
        tx, nonce=w3.eth.get_transaction_count(signer.address, "pending"), gas=estimate
    )
    priority = min(w3.eth.max_priority_fee, 2 * 10**9)
    fee = w3.eth.get_block("latest")["baseFeePerGas"] * 2 + priority
    if (
        fee > 100 * 10**9
        or estimate * 2 > 10_000_000
        or w3.eth.get_balance(signer.address) < estimate * 2 * fee
    ):
        raise ConfigurationError("gas budget insufficient or fee ceiling exceeded")
    tx.update(maxPriorityFeePerGas=priority, maxFeePerGas=fee)
    intent = {
        "status": "intent",
        "chain_id": CHAIN_ID,
        "operator": signer.address,
        "nonce": tx["nonce"],
        "to": tx.get("to"),
        "data_hash": Web3.keccak(hexstr=tx["data"]).to_0x_hex(),
        "record": destination.name,
    }
    pending.parent.mkdir(parents=True, exist_ok=True)
    with pending.open("x") as stream:
        json.dump(intent, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        receipt = bot_contract.send_tx(w3, signer, tx, gas_buffer=2)
    except bot_contract.NotSent:
        pending.unlink()
        raise
    except bot_contract.Reverted as exc:
        receipt = exc.receipt
    if w3.eth.get_block(receipt["blockNumber"])["hash"] != receipt["blockHash"]:
        raise ConfigurationError("receipt reorganized; reconcile the pending record")
    success = receipt["status"] == 1
    record = dict(
        intent,
        status="confirmed" if success else "reverted",
        block=receipt["blockNumber"],
        block_hash=Web3.to_hex(receipt["blockHash"]),
        tx_hash=Web3.to_hex(receipt["transactionHash"]),
    )
    if success and "to" not in tx:
        address = receipt["contractAddress"]
        verify(w3, artifact, address, record["block"], initial=True)
        record["address"] = address
    destination.parent.mkdir(parents=True, exist_ok=True)
    ledger.atomic_write(destination, json.dumps(record, indent=2) + "\n")
    pending.unlink()
    print(json.dumps(record, indent=2))
    if not success:
        raise ConfigurationError("transaction reverted; see receipt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "build",
        "estimate-deploy",
        "deploy",
        "verify",
        "calldata",
        "simulate",
        "submit",
    ):
        cmd = commands.add_parser(name)
        if name in ("calldata", "simulate", "submit"):
            cmd.add_argument(
                "request", type=Path, help="JSON with id, function, args (raw units)"
            )
    args = parser.parse_args()
    if args.command == "build":
        build = build_input()
        artifact = compile_vault(build)
        ledger.atomic_write(RELEASE, json.dumps(artifact, separators=(",", ":")) + "\n")
        ledger.atomic_write(
            config.ROOT / "build/Phase3Vault.standard-input.json", json.dumps(build)
        )
        return
    artifact = json.loads(RELEASE.read_text())
    if args.command == "calldata":
        address = binding(DIRECTORY)
        vault = Web3().eth.contract(address=address, abi=artifact["abi"])
        _, data = requested(args.request, vault, ADMIN | OPS)
        print(
            json.dumps({"chainId": CHAIN_ID, "to": address, "value": 0, "data": data})
        )
        return
    live = args.command in ("deploy", "submit")
    if live:
        load_environment()
        if (
            args.command == "submit"
            and os.environ.get("DRY_RUN", "true").lower() != "false"
        ):
            raise ConfigurationError("submit requires DRY_RUN=false")
        w3 = Web3(config.rpc_provider(os.environ["RPC_URL"]))
    else:
        chain = (
            Chain(os.environ["PHASE3_RPC_URL"])
            if os.environ.get("PHASE3_RPC_URL")
            else Chain()
        )
        w3 = chain.w3
    with operator_lock.sender(ledger.LOG_DIR, "manual", live=live):
        if w3.eth.chain_id != CHAIN_ID:
            raise ConfigurationError("Phase 3 requires Sepolia")
        tx = {"from": OPERATOR, "chainId": CHAIN_ID, "value": 0}
        if args.command in ("estimate-deploy", "deploy"):
            tx["data"] = plan(artifact)
            destination = DIRECTORY / "deployment.json"
            if destination.exists():
                raise ConfigurationError("Phase 3 deployment already recorded")
            if not live:
                print(
                    f"Owner {OWNER}\nOperator {OPERATOR}\nDeployment estimate {w3.eth.estimate_gas(tx)} gas"
                )
                return
        else:
            address = binding(DIRECTORY)
            block = w3.eth.block_number
            vault = verify(w3, artifact, address, block)
            if args.command == "verify":
                print(
                    f"Verified Sepolia Phase3Vault {address} at block {block}; owner {OWNER}; operator {OPERATOR}"
                )
                return
            key, data = requested(args.request, vault, OPS)
            destination = DIRECTORY / "actions" / (key + ".json")
            tx.update(to=address, data=data)
            result = w3.eth.call(tx, block_identifier=block)
            if not live:
                print(
                    f"Simulation passed at block {block}, return data {Web3.to_hex(result)}"
                )
                return
        account = w3.eth.account.from_key(os.environ["PRIVATE_KEY"])
        if account.address != OPERATOR:
            raise ConfigurationError("operator signer mismatch")
        broadcast(w3, account, tx, destination, DIRECTORY / "pending.json", artifact)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — suppress provider bodies and secret URLs at the CLI boundary
        detail = str(exc) if isinstance(exc, ConfigurationError) else error_kind(exc)
        raise SystemExit(f"Phase 3 vault command failed ({detail})") from None
