"""Shared offline lab kit for the Phase 3 credit facility.

Deploys the facility's PUBLIC creation bytecode (Blockscout) on the
in-memory eth-tester chain with mock tokens and a mock pricing pool, and
exposes its participant and admin methods to the local scenarios.
"""

import ast
import json
import math
import re
from pathlib import Path

import solcx
from eth_abi import encode as abi_encode
from web3 import EthereumTesterProvider, Web3

HERE = Path(__file__).resolve().parent
A = 10**18
CREDIT_ABI = json.loads((HERE / "facility-abi.json").read_text())
CODE = (HERE / "creation.hex").read_text().strip()

MOCKS = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

contract Token {
    string public name; string public symbol; uint8 public decimals = 18;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bool public paused;
    uint256 public totalSupply;
    constructor(string memory n, string memory s) { name = n; symbol = s; }
    function setPaused(bool v) external { paused = v; }
    function mint(address to, uint256 amount) external { balanceOf[to] += amount; totalSupply += amount; }
    function burn(address from, uint256 amount) external { balanceOf[from] -= amount; totalSupply -= amount; }
    // The platform token's burn path: burns from `from` against the CALLER's allowance.
    function burnFrom(address from, uint256 amount) external {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
        balanceOf[from] -= amount; totalSupply -= amount;
    }
    function approve(address s, uint256 a) external returns (bool) { allowance[msg.sender][s] = a; return true; }
    function transfer(address to, uint256 a) external returns (bool) { return _move(msg.sender, to, a); }
    function transferFrom(address f, address to, uint256 a) external returns (bool) {
        uint256 allowed = allowance[f][msg.sender];
        require(allowed >= a, "allowance");
        if (allowed != type(uint256).max) allowance[f][msg.sender] = allowed - a;
        return _move(f, to, a);
    }
    function _move(address f, address to, uint256 a) internal returns (bool) {
        require(balanceOf[f] >= a, "balance");
        balanceOf[f] -= a; balanceOf[to] += a; return true;
    }
}

/// Mock pricing pool: one observation whose mean tick is whatever `meanTick` says.
contract Pool {
    address public immutable token0;
    address public immutable token1;
    uint24 public immutable fee;
    int24 public meanTick;
    int24 public tick;
    uint160 public sqrtPriceX96;
    uint128 public liquidity;
    uint32 public immutable ts0;
    struct Observation { uint32 blockTimestamp; int56 tickCumulative; uint160 secondsPerLiquidityCumulativeX128; bool initialized; }
    Observation[65535] public observations;
    uint16 public observationCardinality = 1;
    uint16 public observationCardinalityNext = 1;
    uint16 internal constant cardinalityNextOld = 1;
    constructor(address a, address b, uint24 f, uint160 sqrt0, int24 t) {
        token0 = a; token1 = b; fee = f; sqrtPriceX96 = sqrt0; tick = t; meanTick = t;
        ts0 = uint32(block.timestamp);
        observations[0] = Observation(uint32(block.timestamp), 0, 0, true);
    }
    function setOracle(int24 meanTick_, int24 tick_, uint160 sqrt_) external { meanTick = meanTick_; tick = tick_; sqrtPriceX96 = sqrt_; }
    function setLiquidity(uint128 l) external { liquidity = l; }
    function setCardinality(uint16 n) external { observationCardinality = n; observationCardinalityNext = n; }
    function observe(uint32[] calldata secondsAgos) external view returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidityCumulatives) {
        tickCumulatives = new int56[](secondsAgos.length);
        secondsPerLiquidityCumulatives = new uint160[](secondsAgos.length);
        for (uint256 i = 0; i < secondsAgos.length; i++) {
            uint32 target = uint32(block.timestamp) - secondsAgos[i];
            unchecked {
                // signed (t - ts0) so a window older than the observation still spans the full range
                int256 dt = int256(uint256(target)) - int256(uint256(ts0));
                tickCumulatives[i] = int56(dt * int256(meanTick));
            }
        }
    }
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint32, bool) {
        // The platform pool's slot0 has SEVEN words (feeProtocol is its own word), like the real one.
        return (sqrtPriceX96, tick, 0, observationCardinality, observationCardinalityNext, 0, true);
    }
    function tickSpacing() external view returns (int24) { return 60; }
    function factory() external view returns (address) { return address(0x0000000000000000000000000000000000000001); }
}
"""


def compile_mocks():
    out = solcx.compile_standard(
        {
            "language": "Solidity",
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "evmVersion": "paris",
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
            },
            "sources": {"Mocks.sol": {"content": MOCKS}},
        },
        solc_version="0.8.27",
    )
    return {
        name: (item["abi"], item["evm"]["bytecode"]["object"])
        for src in out["contracts"].values()
        for name, item in src.items()
    }


def deploy(w3, abi, bytecode, *args):
    tx = w3.eth.contract(abi=abi, bytecode=bytecode).constructor(*args).transact()
    addr = w3.eth.wait_for_transaction_receipt(tx)["contractAddress"]
    return w3.eth.contract(address=addr, abi=abi)


# The two hard-coded organizers (PUSH20 constants, twice each in the creation code).
ORGANIZERS = {
    "92a3b01d3af8aa7fc0d6695e2153f23d9146ff17": "CHAO",
    "b6ac0341fcf3fb507a8208d34a97f13779e1393d": "JOHN",
}
ERRORS = {
    "6593fd52": "HealthyPosition",
    "3a23d825": "InsufficientCollateral",
    "2c5211c6": "InvalidAmount",
    "2083cd40": "InvalidPool",
    "c1ab6dc1": "InvalidToken",
    "3261c792": "LimitExceeded",
    "429eabc6": "OracleUnavailable",
    "9e87fac8": "Paused",
    "ab143c06": "Reentrancy",
    "82b42900": "Unauthorized",
    "9ba8d24d": "UnsettledBadDebt",
}
_BYTES = re.compile(r"""b(['"])((?:\\.|(?!\1).)*)\1""")


def reason(exc):
    """Custom-error name of a reverted facility call (lab helper).

    eth-tester reports revert data as a Python bytes repr inside the message
    (``execution reverted: b',R\\x11\\xc6'``), printable bytes unescaped.
    """
    found = _BYTES.search(" ".join(str(exc).split()))
    if not found:
        return "empty"
    raw = ast.literal_eval("b" + found.group(0)[1:])
    return ERRORS.get(raw.hex(), raw.hex() or "empty")


def deploy_credit(w3, *args, admins=None):
    # Blockscout's creation bytecode carries the REAL constructor arguments as its
    # last 96 bytes (constructors CODECOPY their arguments from the code): swap them.
    # `admins` maps an organizer address to a lab account (PUSH20 patch) so admin
    # functions can be exercised without any real key.
    body = bytes.fromhex(CODE[2:-192])
    for real, stand_in in (admins or {}).items():
        old, new = bytes.fromhex(real.removeprefix("0x")), bytes.fromhex(stand_in.removeprefix("0x"))
        assert body.count(b"\x73" + old) == 2, real
        body = body.replace(b"\x73" + old, b"\x73" + new)
    no_ctor = [item for item in CREDIT_ABI if item.get("type") != "constructor"]
    init = body + abi_encode(["address"] * len(args), list(args))
    tx = w3.eth.contract(abi=no_ctor, bytecode="0x" + init.hex()).constructor().transact()
    address = w3.eth.wait_for_transaction_receipt(tx)["contractAddress"]
    return w3.eth.contract(address=address, abi=CREDIT_ABI)


class Lab:
    def __init__(self, admins=None):
        """`admins=True` stands lab accounts in for the hard-coded organizers (throwaway keys)."""
        self.art = compile_mocks()
        self.w3 = Web3(EthereumTesterProvider())
        self.w3.eth.default_account = self.w3.eth.accounts[0]
        w3 = self.w3
        mapping = None
        if admins is True:
            mapping = dict(zip(ORGANIZERS, (w3.eth.accounts[5], w3.eth.accounts[9]), strict=True))
        self.admins = mapping
        self.admin, self.alice, self.bob, self.carol, self.liquidator = w3.eth.accounts[:5]
        self.f564 = deploy(w3, *self.art["Token"], "FIN564 Phase 3 f564", "f564")  # token0 of the pool
        self.feth = deploy(w3, *self.art["Token"], "FIN564 Phase 3 fETH", "fETH")
        self.tick0 = -69082  # price ~1000.1 f564 per fETH with token0 = f564
        self.sqrt0 = 2505414483750479311864138015
        self.pool = deploy(w3, *self.art["Pool"], self.f564.address, self.feth.address, 3000, self.sqrt0, self.tick0)
        self.credit = deploy_credit(w3, self.feth.address, self.f564.address, self.pool.address, admins=mapping)
        self.c = self.credit.functions
        for who in (self.alice, self.bob, self.carol, self.liquidator):
            self.f564.functions.mint(who, 1_000_000 * A).transact()
            self.feth.functions.mint(who, 1_000 * A).transact()
            self.f564.functions.approve(self.credit.address, 2**256 - 1).transact({"from": who})
            self.feth.functions.approve(self.credit.address, 2**256 - 1).transact({"from": who})

    def set_price(self, price):
        """Point the oracle at a f564-per-fETH price (tick negative: token0 = f564)."""
        tick = round(math.log(price) / math.log(1.0001))
        self.pool.functions.setOracle(-tick, -tick, self.sqrt0).transact()

    def try_call(self, fn, **kw):
        """eth_call: ('ok', result) or ('revert', custom-error name)."""
        try:
            return ("ok", fn.call(kw))
        except Exception as exc:  # noqa: BLE001 — lab: show the revert reason
            return ("revert", reason(exc))

    def try_send(self, fn, **kw):
        """Mined transaction: 'ok' or the custom-error name."""
        try:
            self.w3.eth.wait_for_transaction_receipt(fn.transact(kw) if kw else fn.transact())
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return reason(exc)
