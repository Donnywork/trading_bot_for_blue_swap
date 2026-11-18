import math
from decimal import Decimal
from types import SimpleNamespace

import pytest

import config
import ledger
import legacy_main as main
import pool_monitor
import v3pool

FETH = "0x2238115a9B6469DEE6c004B00A0367fFba2Fd1BC"
F564 = "0x21abd410c4240CCC170D004fEfB6405943e0d7Ff"
NARROW = "0x68D8bC396df8e791EFd45A9b214CB07fe4300f48"
WIDE = "0xC67Fcb8459C792aE1E271f24CE212Ba90eCC2920"
IDENTITY = {
    "pool_address": NARROW.lower(),
    "base_address": FETH.lower(),
    "quote_address": F564.lower(),
    "fee": 500,
    "base_is_token0": False,
}


@pytest.fixture
def phase2(monkeypatch):
    for key, value in {
        "FETH_ADDRESS": FETH,
        "F564_ADDRESS": F564,
        "POOL_ADDRESS": NARROW,
        "POOL_FEE": "500",
        "SECONDARY_POOL_ADDRESS": WIDE,
        "SECONDARY_POOL_FEE": "10000",
        "RPC_URL": "https://example.invalid",
        "ROUTER_ADDRESS": FETH,
        "QUOTER_ADDRESS": F564,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)


class Call:
    def __init__(self, value):
        self.value = value

    def call(self, **kwargs):
        return self.value


class Pool:
    address = NARROW

    def __init__(self, fee=500, token0=F564, token1=FETH):
        self.functions = self
        self.fee_value, self.t0, self.t1 = fee, token0, token1

    def token0(self):
        return Call(self.t0)

    def token1(self):
        return Call(self.t1)

    def fee(self):
        return Call(self.fee_value)

    def factory(self):
        return Call(FETH)

    def tickSpacing(self):
        return Call(10)

    def liquidity(self):
        return Call(10**24)

    def slot0(self):
        return Call((int(math.sqrt(1 / 1000) * 2**96), 0))


def test_two_pools_and_explicit_single_pool(phase2, monkeypatch):
    assert config.configured_pools() == [(NARROW, 500), (WIDE, 10000)]
    monkeypatch.setenv("SECONDARY_POOL_ADDRESS", "")
    assert config.configured_pools() == [(NARROW, 500)]


@pytest.mark.parametrize(
    "key,value",
    [
        ("POOL_FEE", "300"),
        ("POOL_FEE", "0.05"),
        ("SECONDARY_POOL_FEE", "500"),
        ("SECONDARY_POOL_ADDRESS", NARROW),
        ("POOL_ADDRESS", "bad"),
    ],
)
def test_invalid_pool_configuration_is_refused(phase2, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        config.configured_pools()


def load_fake_market(monkeypatch, pool):
    w3 = SimpleNamespace(
        is_connected=lambda: True,
        eth=SimpleNamespace(
            chain_id=11155111, block_number=42, contract=lambda **kw: pool
        ),
    )
    # Identical symbols deliberately: token identity must be based on addresses.
    monkeypatch.setattr(config, "read_token", lambda *a: ("TOKEN", 18))
    monkeypatch.setattr(config, "check_factories", lambda *a: None)
    monkeypatch.setattr(config, "read_price", lambda *a: None)
    return config.load_market(w3=w3)


def test_market_uses_addresses_instead_of_symbols(phase2, monkeypatch):
    market = load_fake_market(monkeypatch, Pool())
    assert market["tokens"]["feth"][0] == FETH
    assert market["tokens"]["f564"][0] == F564
    assert market["fee"] == 500


@pytest.mark.parametrize(
    "pool,message",
    [
        (Pool(fee=3000), "fee"),
        (Pool(token0="0x" + "11" * 20), "tokens"),
    ],
)
def test_phase1_or_wrong_fee_is_refused(phase2, monkeypatch, pool, message):
    with pytest.raises(ValueError, match=message):
        load_fake_market(monkeypatch, pool)


def test_phase2_buy_spends_f564_and_sell_spends_feth(monkeypatch):
    market = {
        "pool": Pool(),
        "fee": 500,
        "tokens": {
            "feth": (FETH, 18, "fETH"),
            "f564": (F564, 18, "f564"),
        },
    }
    bot = SimpleNamespace(functions=SimpleNamespace(tokenCap=lambda a: Call(10**30)))
    io = main.ChainIO(None, None, bot, market, SimpleNamespace(dry_run=True))
    assert io.read_price() == pytest.approx(1000)
    assert io.market_identity() == IDENTITY
    inputs, fees = [], []

    def quote(m, symbol, amount):
        inputs.append((symbol, amount))
        return {"amount_out": 10**18, "too_large": False}

    monkeypatch.setattr(config, "get_quote", quote)
    monkeypatch.setattr(
        main.bot_contract, "swap_via_contract", lambda *args: fees.append(args[4])
    )
    assert io.trade("buy", Decimal(1000)) == 1
    assert io.trade("sell", Decimal(1)) == 1
    assert inputs == [("f564", Decimal(1000)), ("fETH", Decimal(1))]
    assert fees == [500, 500]


@pytest.mark.parametrize("fee", [500, 10000])
def test_reversed_layout_has_identical_f564_cashflows(fee):
    normal = {**v3pool.load_profile(), "fee": fee}
    reversed_profile = {
        **normal,
        "base_is_token0": False,
        "ticks": [{"tick": -t["tick"], "net": -t["net"]} for t in normal["ticks"]],
    }
    for price in (900, 1043, 1200):
        assert v3pool.round_trip(
            reversed_profile, price, price * 1.02, 1000
        ) == pytest.approx(v3pool.round_trip(normal, price, price * 1.02, 1000))
        assert v3pool.depth_scale(reversed_profile, price, 10**24) == pytest.approx(
            v3pool.depth_scale(normal, price, 10**24)
        )


def test_layout_fee_matches_constant_liquidity_formula():
    liquidity, price, amount = 1e6, 1000, 1000
    for fee in (500, 10000):
        profile = {
            "d0": 18,
            "d1": 18,
            "fee": fee,
            "ticks": [
                {"tick": -887270, "net": int(liquidity * 10**18)},
                {"tick": 887270, "net": -int(liquidity * 10**18)},
            ],
        }
        effective = amount * (1 - fee / 1e6)
        expected = liquidity * (
            1 / math.sqrt(price) - 1 / (math.sqrt(price) + effective / liquidity)
        )
        assert v3pool.V3Pool(profile, price).buy_with_quote(amount) == pytest.approx(
            expected
        )


def test_phase2_never_uses_phase1_depth(tmp_path):
    path = tmp_path / "pool.json"
    with pytest.raises(ValueError, match="matching"):
        v3pool.current_profile(path, expected=IDENTITY)
    v3pool.save_profile(path, v3pool.load_profile())
    with pytest.raises(ValueError):
        v3pool.current_profile(path, expected=IDENTITY)
    tagged = {**v3pool.load_profile(), **IDENTITY}
    v3pool.save_profile(path, tagged)
    assert v3pool.current_profile(path, expected=IDENTITY) == tagged
    with pytest.raises(ValueError):
        v3pool.current_profile(
            path, expected={**IDENTITY, "pool_address": WIDE.lower()}
        )


def test_market_change_cannot_reinterpret_existing_positions(tmp_path):
    ledger.bind_market(tmp_path, IDENTITY)
    ledger.save(
        tmp_path / "ledger.json",
        ledger.open_position(ledger.empty(), 1000, Decimal(1), Decimal(1000), 1),
    )
    ledger.bind_market(tmp_path, IDENTITY)
    with pytest.raises(ValueError, match="Market changed"):
        ledger.bind_market(tmp_path, {**IDENTITY, "pool_address": WIDE.lower()})
    assert ledger.load(tmp_path / "ledger.json")["open"]["base"] == "1"
    (tmp_path / "market.json").unlink()
    with pytest.raises(ValueError, match="Unidentified"):
        ledger.bind_market(tmp_path, IDENTITY)
    assert not (tmp_path / "market.json").exists()


def test_bad_market_marker_is_never_overwritten(tmp_path):
    path = tmp_path / "market.json"
    path.write_text("{")
    with pytest.raises(ValueError, match="unreadable"):
        ledger.bind_market(tmp_path, IDENTITY)
    assert path.read_text() == "{"


def test_monitor_reports_feth_buys_and_f564_price_in_phase2():
    logs = [
        {
            "blockNumber": 1,
            "args": {
                "recipient": FETH,
                "amount0": 1000 * 10**18,
                "amount1": -(10**18),
                "sqrtPriceX96": int(math.sqrt(0.001) * 2**96),
            },
        }
    ]
    swap = pool_monitor.to_swaps(logs, {1: 123}, 18, 18, False)[0]
    assert swap.side == "buy" and swap.base_amount == 1
    assert swap.price == pytest.approx(1000)


def test_quotes_use_the_requested_fee_and_fixed_block(monkeypatch):
    calls = []

    class QuoteCall:
        def call(self, **kwargs):
            calls.append(kwargs)
            return 123, 0, 2, 50000

    def quote_single(params):
        calls.append(params)
        return QuoteCall()

    quoter = SimpleNamespace(
        functions=SimpleNamespace(quoteExactInputSingle=quote_single)
    )
    for fee in (500, 10000):
        assert config.quote_swap(quoter, F564, FETH, fee, 10**18, 42) == (123, 2, 50000)
    assert calls == [
        (F564, FETH, 10**18, 500, 0),
        {"block_identifier": 42},
        (F564, FETH, 10**18, 10000, 0),
        {"block_identifier": 42},
    ]
