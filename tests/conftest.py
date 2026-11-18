import os

import pytest
import solcx
from web3 import EthereumTesterProvider, Web3

import bot_contract
import phase2_contract

A = 10**18
FEES = [3000, 500]


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    # CLI tests must never load an operator's real environment file.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


def _compile_mocks():
    out = solcx.compile_files(
        [str(bot_contract.ROOT / "contracts/mocks/Mocks.sol")],
        output_values=["abi", "bin"],
        solc_version=bot_contract.SOLC_VERSION,
        evm_version=bot_contract.EVM_VERSION,
    )
    return {k.split(":")[1]: v for k, v in out.items()}


def _deploy(w3, abi, bytecode, *args):
    tx = w3.eth.contract(abi=abi, bytecode=bytecode).constructor(*args).transact()
    addr = w3.eth.wait_for_transaction_receipt(tx)["contractAddress"]
    return w3.eth.contract(address=addr, abi=abi)


@pytest.fixture(scope="module")
def compiled():
    abi, bytecode = bot_contract.compile_bot()
    mocks = _compile_mocks()
    mocks["TradingBot"] = {"abi": abi, "bin": bytecode}
    return mocks


class Env:
    """In-memory chain with a vault holding 5 tokenA; A/B are allowed (cap 2 each)."""

    def __init__(self, compiled):
        self.compiled = compiled
        self.w3 = w3 = Web3(EthereumTesterProvider())
        self.owner, self.operator, self.stranger = w3.eth.accounts[:3]
        w3.eth.default_account = self.owner

        self.a = self.deploy("MockToken")
        self.b = self.deploy("MockToken")
        self.junk = self.deploy("MockToken")  # a token the owner never allowed
        self.router = self.deploy("MockRouter")
        self.bot = self.deploy_bot(self.router)
        self.a.functions.mint(self.bot.address, 5 * A).transact()

    def deploy(self, name, *args):
        c = self.compiled[name]
        return _deploy(self.w3, c["abi"], c["bin"], *args)

    def deploy_bot(self, router, tokens=None, caps=None, fees=FEES):
        tokens = tokens or [self.a.address, self.b.address]
        caps = caps or [2 * A, 2 * A]
        return self.deploy(
            "TradingBot", self.owner, router.address, self.operator, tokens, caps, fees
        )

    def swap(self, tin, tout, amount=A, min_out=A // 2, fee=3000, frm=None, bot=None):
        bot = bot or self.bot
        return bot.functions.swap(
            tin.address, tout.address, fee, amount, min_out
        ).transact({"from": frm or self.operator})

    def receipt(self, tx_hash):
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)


@pytest.fixture
def env(compiled):
    return Env(compiled)


@pytest.fixture(scope="module")
def combined_artifacts():
    build = phase2_contract.standard_json_input()
    mutation = os.environ.get("PHASE2_TEST_MUTATION")
    if mutation:
        path = (
            phase2_contract.SOURCE
            if mutation == "single_operator"
            else "contracts/ArbitrageVault.sol"
        )
        if mutation not in ("single_operator", "cycle_operator"):
            raise ValueError("unknown TEST-ONLY mutation")
        build["sources"][path]["content"] = build["sources"][path]["content"].replace(
            'require(msg.sender == operator || msg.sender == owner, "not operator");',
            "",
        )
    for path in ("contracts/mocks/Mocks.sol", "contracts/mocks/ArbitrageMocks.sol"):
        build["sources"][path] = {"content": (bot_contract.ROOT / path).read_text()}
    out = solcx.compile_standard(build, solc_version=bot_contract.SOLC_VERSION)
    return {
        name: {"abi": item["abi"], "bin": item["evm"]["bytecode"]["object"]}
        for source in out["contracts"].values()
        for name, item in source.items()
    }


@pytest.fixture
def combined(combined_artifacts):
    from types import SimpleNamespace

    w3 = Web3(EthereumTesterProvider())
    owner, operator, stranger = w3.eth.accounts[:3]
    w3.eth.default_account = owner

    def deploy(name, *args):
        item = combined_artifacts[name]
        return _deploy(w3, item["abi"], item["bin"], *args)

    a, b = deploy("MockToken"), deploy("MockToken")
    factory = deploy("MockArbFactory")
    router = deploy("MockArbRouter", factory.address)
    pools = [
        deploy("MockArbPool", a.address, b.address, fee, factory.address)
        for fee in (500, 10000)
    ]
    for fee, pool in zip((500, 10000), pools, strict=True):
        factory.functions.setPool(a.address, b.address, fee, pool.address).transact()
        for token in (a, b):
            router.functions.setRoute(
                token.address, fee, (1, 1, 10**30, 0, False)
            ).transact()
    args = [
        owner,
        operator,
        router.address,
        a.address,
        b.address,
        [p.address for p in pools],
        [1000 * A, 10 * A, A // 10],
    ]
    bot = deploy("Phase2TradingBot", *args)
    a.functions.mint(bot.address, 5000 * A).transact()
    b.functions.mint(bot.address, 3 * A).transact()
    bot.functions.setPaused(False).transact()
    return SimpleNamespace(**locals())
