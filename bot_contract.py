import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import solcx
from dotenv import load_dotenv
from web3 import Web3

import config

ROOT = Path(__file__).parent
SOLC_VERSION = "0.8.27"
EVM_VERSION = "paris"
CONTRACT_PATH = ROOT / "contracts/TradingBot.sol"
ABI_PATH = ROOT / "abis/trading_bot.json"
BUILD_DIR = ROOT / "build"
SEPOLIA_CHAIN_ID = 11155111
GAS_BUFFER = 1.2
FEE_TIERS = [3000]  # the only pool fee tier the bot trades


def standard_json_input():
    # ------------------------------------
    # The single build input: compile_bot() deploys exactly this, and Etherscan's
    # "Standard-Json-Input" verification takes the same file, so bytecode matches.
    # ------------------------------------

    return {
        "language": "Solidity",
        "sources": {"contracts/TradingBot.sol": {"content": CONTRACT_PATH.read_text()}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "evmVersion": EVM_VERSION,  # no PUSH0/MCOPY surprises on older chains
            "outputSelection": {
                "*": {
                    "*": [
                        "abi",
                        "evm.bytecode.object",
                        "evm.deployedBytecode.object",
                        "evm.deployedBytecode.immutableReferences",
                    ]
                }
            },
        },
    }


def compile_contract():
    solcx.install_solc(SOLC_VERSION, show_progress=False)
    out = solcx.compile_standard(standard_json_input(), solc_version=SOLC_VERSION)
    return out["contracts"]["contracts/TradingBot.sol"]["TradingBot"]


def compile_bot():
    # ------------------------------------
    # Compile contracts/TradingBot.sol -> (abi, bytecode)
    # ------------------------------------

    contract = compile_contract()
    return contract["abi"], contract["evm"]["bytecode"]["object"]


def deployed_code_matches(w3, address):
    # ------------------------------------
    # Is the code at `address` exactly this repo's contract? The router address is an immutable
    # baked into the bytecode, so blank those bytes on both sides before comparing.
    # ------------------------------------

    deployed = compile_contract()["evm"]["deployedBytecode"]
    expected = bytearray(bytes.fromhex(deployed["object"]))
    actual = bytearray(bytes(w3.eth.get_code(Web3.to_checksum_address(address))))
    if len(expected) != len(actual):
        return False
    for refs in deployed["immutableReferences"].values():
        for ref in refs:
            start, length = ref["start"], ref["length"]
            expected[start : start + length] = bytes(length)
            actual[start : start + length] = bytes(length)
    return expected == actual


def validate_owner(owner, bot_wallet):
    # ------------------------------------
    # A wrong owner is permanent-ish: only the owner can change it. Refuse anything that is
    # not an exact checksummed address (to_checksum_address would silently "fix" a typo).
    # ------------------------------------

    if not owner:
        raise ValueError("OWNER_ADDRESS is empty: set it to your enrolled wallet")
    if not Web3.is_checksum_address(owner):
        raise ValueError(
            f"OWNER_ADDRESS {owner!r} is not a valid checksummed address "
            "(copy it from your wallet or Etherscan, do not retype it)"
        )
    if int(owner, 16) == 0:
        raise ValueError("OWNER_ADDRESS is the zero address")
    if owner == bot_wallet:
        raise ValueError(
            "OWNER_ADDRESS is the bot wallet: the owner must be your enrolled wallet, "
            "whose key never goes in .env"
        )
    return owner


def send_tx(w3, account, tx):
    # ------------------------------------
    # Fill nonce/gas/fees, sign, send, wait for the receipt
    # ------------------------------------

    tx = {
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
        **tx,
    }
    # build_transaction already estimates gas; add headroom in case state moves before mining
    gas = tx.get("gas") or w3.eth.estimate_gas(tx)
    tx["gas"] = int(gas * GAS_BUFFER)
    if "maxFeePerGas" not in tx:
        priority = w3.eth.max_priority_fee
        base = w3.eth.get_block("latest")["baseFeePerGas"]
        tx["maxPriorityFeePerGas"] = priority
        tx["maxFeePerGas"] = base * 2 + priority

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print("Tx sent:", tx_hash.to_0x_hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash.to_0x_hex()}")
    return receipt


def deploy_bot(w3, account, owner, router, operator, caps, fees):
    # ------------------------------------
    # Deploy. The deployer only pays gas; `owner` is who controls the funds.
    # caps: {token_address: max_amount_in_per_swap}; fees: allowed fee tiers.
    # ------------------------------------

    abi, bytecode = compile_bot()
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    constructor = factory.constructor(
        Web3.to_checksum_address(owner),
        Web3.to_checksum_address(router),
        Web3.to_checksum_address(operator),
        [Web3.to_checksum_address(t) for t in caps],
        list(caps.values()),
        list(fees),
    )
    tx = constructor.build_transaction({"from": account.address})
    receipt = send_tx(w3, account, tx)
    # ABI-encoded constructor args (for Etherscan verification) = deploy data minus bytecode
    ctor_args = tx["data"][len("0x" + bytecode) :]
    return receipt["contractAddress"], abi, ctor_args


def load_bot(w3, address):
    address = Web3.to_checksum_address(address)
    if not w3.eth.get_code(address):
        raise SystemExit(f"No contract at {address}: check BOT_CONTRACT_ADDRESS")
    # the ABI saved at deploy time matches the deployed bytecode even if the source changed
    abi = json.loads(ABI_PATH.read_text()) if ABI_PATH.exists() else compile_bot()[0]
    return w3.eth.contract(address=address, abi=abi)


def swap_via_contract(w3, account, bot, quote, fee, dry_run=True):
    # ------------------------------------
    # Send a swap through the contract using a get_quote() result.
    # dry_run simulates with eth_call: nothing is sent, no gas is spent.
    # ------------------------------------

    if quote["amount_out_min"] <= 0:
        raise ValueError("Amount too small: the minimum output rounds to 0")

    fn = bot.functions.swap(
        Web3.to_checksum_address(quote["token_in"]),
        Web3.to_checksum_address(quote["token_out"]),
        fee,
        quote["amount_in"],
        quote["amount_out_min"],
    )

    simulated = fn.call({"from": account.address})
    print(f"Simulated amountOut: {simulated}")

    if dry_run:
        print("DRY_RUN=true: not sending")
        return None

    return send_tx(w3, account, fn.build_transaction({"from": account.address}))


def connect():
    load_dotenv()
    w3 = Web3(Web3.HTTPProvider(os.environ["RPC_URL"]))
    if not w3.is_connected():
        raise ConnectionError("Failed to connect to Ethereum RPC")
    if w3.eth.chain_id != SEPOLIA_CHAIN_ID:
        raise ValueError(f"Wrong network! Expected Sepolia, got {w3.eth.chain_id}")
    account = w3.eth.account.from_key(os.environ["PRIVATE_KEY"])
    return w3, account


def token_contract(w3, address):
    return w3.eth.contract(
        address=Web3.to_checksum_address(address), abi=config.erc20_abi
    )


def vault_tokens(w3, bot):
    # (symbol, decimals, vault balance in human units, per-swap cap in human units) per token
    rows = []
    for name in ("F564_ADDRESS", "FETH_ADDRESS"):
        token = token_contract(w3, os.environ[name])
        sym = token.functions.symbol().call()
        dec = token.functions.decimals().call()
        bal = Decimal(token.functions.balanceOf(bot.address).call()) / 10**dec
        cap = Decimal(bot.functions.tokenCap(token.address).call()) / 10**dec
        rows.append((sym, dec, bal, cap))
    return rows


def cmd_deploy():
    w3, account = connect()

    if os.environ.get("BOT_CONTRACT_ADDRESS"):
        raise SystemExit(
            "BOT_CONTRACT_ADDRESS is already set: clear it first if you want a new contract"
        )

    owner = validate_owner(os.environ.get("OWNER_ADDRESS", ""), account.address)
    router = os.environ["ROUTER_ADDRESS"]
    max_trade = Decimal(os.environ["MAX_TRADE_AMOUNT"])

    # per-swap on-chain cap = MAX_TRADE_AMOUNT in each token's own units
    caps = {}
    for name in ("F564_ADDRESS", "FETH_ADDRESS"):
        token = token_contract(w3, os.environ[name])
        caps[token.address] = int(max_trade * 10 ** token.functions.decimals().call())

    print("Deployer / operator:", account.address)
    print("Owner:              ", owner)
    print("Router:             ", router)
    print("Allowed fee tiers:  ", FEE_TIERS)
    for token_address, cap in caps.items():
        print(f"Allowed token:       {token_address} (cap per swap: {cap})")
    eth = Web3.from_wei(w3.eth.get_balance(account.address), "ether")
    print("Deployer ETH:       ", eth)

    answer = input(f"\nOnly {owner} can ever change the owner. Type YES to deploy: ")
    if answer != "YES":
        raise SystemExit("aborted")

    address, abi, ctor_args = deploy_bot(
        w3, account, owner, router, account.address, caps, FEE_TIERS
    )

    print("\nDeployed TradingBot at:", address)
    print("Add to .env:  BOT_CONTRACT_ADDRESS=" + address)
    print(f"https://sepolia.etherscan.io/address/{address}")

    ABI_PATH.write_text(json.dumps(abi, indent=2))
    print(f"\nABI saved to {ABI_PATH.relative_to(ROOT)}")
    print(f"\nTo verify on Etherscan (Standard-Json-Input, solc v{SOLC_VERSION}):")
    print("  uv run python bot_contract.py verify-input")
    print("  constructor arguments (ABI-encoded):", ctor_args)


def cmd_verify_input():
    BUILD_DIR.mkdir(exist_ok=True)
    path = BUILD_DIR / "TradingBot.standard-input.json"
    path.write_text(json.dumps(standard_json_input(), indent=2))
    print(f"Wrote {path.relative_to(ROOT)}")
    print(
        "Etherscan -> Verify & Publish -> Solidity (Standard-Json-Input), "
        f"compiler v{SOLC_VERSION}"
    )


def cmd_status():
    w3, account = connect()
    bot = load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])

    operator = bot.functions.operator().call()
    print("Contract:  ", bot.address)
    print("owner():   ", bot.functions.owner().call())
    print("pending:   ", bot.functions.pendingOwner().call())
    print("operator():", operator)
    print("router():  ", bot.functions.router().call())
    print("ETH:       ", Web3.from_wei(w3.eth.get_balance(bot.address), "ether"))
    for fee in FEE_TIERS:
        print(f"fee {fee} allowed:", bot.functions.feeAllowed(fee).call())
    for sym, _, bal, cap in vault_tokens(w3, bot):
        print(f"{sym}:".ljust(11), bal, f"(cap per swap: {cap})")

    eth = Web3.from_wei(w3.eth.get_balance(account.address), "ether")
    print("\nOperator ETH (pays gas):", eth)
    if account.address != operator:
        print("WARNING: PRIVATE_KEY is not the contract's operator: swaps will revert")


def print_calldata(bot, fn_name, args):
    data = bot.encode_abi(fn_name, args=args)
    print("Send from the OWNER wallet:")
    print("  to:  ", bot.address)
    print("  data:", data)
    print(
        "(MetaMask: Settings -> Advanced -> 'Show hex data', then paste into the send "
        "screen; or use Remix 'At Address' / Etherscan Write Contract)"
    )


def cmd_withdraw_calldata(token, amount):
    # ------------------------------------
    # The owner key is never in .env, so print calldata for the owner wallet to send.
    # `token` is fETH, f564 or an address; `amount` is human units or "all".
    # ------------------------------------

    w3, _ = connect()
    bot = load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])
    known = {"feth": os.environ["FETH_ADDRESS"], "f564": os.environ["F564_ADDRESS"]}
    address = Web3.to_checksum_address(known.get(token.lower(), token))
    erc20 = token_contract(w3, address)
    decimals = erc20.functions.decimals().call()
    balance = erc20.functions.balanceOf(bot.address).call()

    raw = balance if amount == "all" else int(Decimal(amount) * 10**decimals)
    if raw <= 0 or raw > balance:
        raise SystemExit(
            f"Amount must be between 0 and the contract balance ({balance})"
        )
    print(f"Withdraw {Decimal(raw) / 10**decimals} of {address}")
    print_calldata(bot, "withdraw", [address, raw])


def cmd_calldata(fn_name, values):
    # generic owner-function calldata, e.g. `calldata setTokenCap 0xTOKEN 1000000000000000000`
    # (raw on-chain values, no unit conversion)
    w3, _ = connect()
    bot = load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])
    abi = next(
        (x for x in bot.abi if x["type"] == "function" and x["name"] == fn_name), None
    )
    if abi is None or len(abi["inputs"]) != len(values):
        raise SystemExit(f"Unknown function or wrong number of arguments for {fn_name}")

    args = []
    for spec, value in zip(abi["inputs"], values, strict=True):
        if spec["type"] == "address":
            args.append(Web3.to_checksum_address(value))
        elif spec["type"].startswith("uint"):
            args.append(int(value))
        elif spec["type"] == "bool":
            args.append(value.lower() == "true")
        else:
            raise SystemExit(f"Unsupported argument type {spec['type']}")
    print_calldata(bot, fn_name, args)


def cmd_swap(token_in, amount):
    # cheap checks first: no compile or RPC work for an amount we would refuse anyway
    max_trade = Decimal(os.environ["MAX_TRADE_AMOUNT"])
    if not Decimal(amount).is_finite() or Decimal(amount) > max_trade:
        raise SystemExit("Amount exceeds MAX_TRADE_AMOUNT")

    w3, account = connect()
    market = config.load_market()
    bot = load_bot(w3, os.environ["BOT_CONTRACT_ADDRESS"])

    quote = config.get_quote(market, token_in, amount)
    if quote["too_large"]:
        raise SystemExit("Price impact too large, aborting")

    dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
    before = vault_tokens(w3, bot)
    receipt = swap_via_contract(w3, account, bot, quote, market["fee"], dry_run)
    if receipt is None:
        return

    print(
        f"\nConfirmed in block {receipt['blockNumber']}, gas used {receipt['gasUsed']}"
    )
    print(f"https://sepolia.etherscan.io/tx/{receipt['transactionHash'].to_0x_hex()}")
    for (sym, _, was, _), (_, _, now, _) in zip(
        before, vault_tokens(w3, bot), strict=True
    ):
        print(f"Vault {sym}: {was} -> {now}")


USAGE = (
    "usage: bot_contract.py deploy | status | verify-input   (live checks: live_check.py)\n"
    "       bot_contract.py swap fETH|f564 AMOUNT\n"
    "       bot_contract.py withdraw-calldata fETH|f564|ADDRESS AMOUNT|all\n"
    "       bot_contract.py calldata FUNCTION ARGS..."
)


def main():
    load_dotenv()
    args = sys.argv[1:]
    if args == ["deploy"]:
        cmd_deploy()
    elif args == ["status"]:
        cmd_status()
    elif args == ["verify-input"]:
        cmd_verify_input()
    elif len(args) == 3 and args[0] == "swap":
        cmd_swap(args[1], args[2])
    elif len(args) == 3 and args[0] == "withdraw-calldata":
        cmd_withdraw_calldata(args[1], args[2])
    elif len(args) >= 2 and args[0] == "calldata":
        cmd_calldata(args[1], args[2:])
    else:
        raise SystemExit(USAGE)


if __name__ == "__main__":
    main()
