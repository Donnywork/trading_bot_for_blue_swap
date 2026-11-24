import json
from math import isqrt
from types import SimpleNamespace

import pytest

import phase3_auto as auto
import phase3_spot as spot
from phase3_chain import WAD


def test_discount_and_exit_thresholds():
    def sqrt(price):
        return isqrt((1 << 192) // price)

    assert not spot.discounted(sqrt(989), (1000 * WAD, 1800))
    assert not spot.discounted(sqrt(980), (1000 * WAD, 1799))
    assert spot.discounted(sqrt(987), (1000 * WAD, 1800))
    p = {"spent": 10000, "time": 100}
    assert spot.exit_reason(p, 10050, 101) == "take_profit"
    assert spot.exit_reason(p, 9850, 101) == "stop_loss"
    assert spot.exit_reason(p, 10049, 1899) is None
    assert spot.exit_reason(p, 10049, 1900) == "time_stop"


@pytest.mark.parametrize(("cap", "cash"), [(250, 1000), (1500, 1100)])
def test_single_pool_worker_recovers_and_sells_only_the_fill(
    combined, tmp_path, monkeypatch, cap, cash
):
    e = combined
    w = e.w3
    q = e.a.address
    b = e.b.address
    size = min(cap, cash) * WAD
    t = e.deploy(
        "TradingBot",
        e.owner,
        e.router.address,
        e.operator,
        [q, b],
        [cap * WAD, spot.PROPOSED_EXIT_CAP],
        [3000],
    )
    e.a.functions.mint(t.address, cash * WAD).transact()
    e.b.functions.mint(t.address, WAD).transact()
    e.router.functions.setRoute(q, 3000, (1, 980, 10**30, 0, False)).transact()
    e.router.functions.setRoute(b, 3000, (995, 1, 10**30, 0, False)).transact()
    monkeypatch.setattr(spot, "ADDRESS", t.address)
    monkeypatch.setattr(spot, "QUOTE", q)
    monkeypatch.setattr(spot, "BASE", b)
    monkeypatch.setattr(spot.vault, "OWNER", e.owner)
    monkeypatch.setattr(spot.vault, "OPERATOR", e.operator)
    monkeypatch.setattr(spot.vault, "CHAIN_ID", w.eth.chain_id)
    monkeypatch.setattr(
        spot, "RUNTIME_HASH", w.keccak(w.eth.get_code(t.address)).to_0x_hex()
    )
    monkeypatch.setattr(spot, "eligible_vault", lambda: True)
    monkeypatch.setattr(spot, "alert", lambda *args: None)
    monkeypatch.setattr(auto, "MAX_GAS_COST", 10**19)
    monkeypatch.setattr(auto, "DAILY_GAS", 10**19)
    monkeypatch.setattr(auto, "END", 2**32)

    def header(n):
        h = w.eth.get_block(n)
        return h.number, w.to_hex(h.hash), h.timestamp

    monkeypatch.setattr(spot, "time", SimpleNamespace(time=lambda: header("latest")[2]))
    folder = tmp_path / "phase3"
    folder.mkdir()
    now = header("latest")[2]
    (folder / "state.json").write_text(
        json.dumps(
            {
                "health": "ready",
                "updated": now,
                "site_checked_at": now,
                "site": {
                    "phase": "phase-3",
                    "pricingPool": spot.POOL,
                    "status": "running",
                    "tradingEnabled": False,
                },
            }
        )
    )
    sale = {
        "args": {"amount1": 1},
        "blockNumber": header("latest")[0] - 1,
        "transactionHash": w.keccak(text="sale"),
    }
    sales = []
    pool = SimpleNamespace(
        events=SimpleNamespace(
            Swap=lambda: SimpleNamespace(get_logs=lambda **kwargs: sales)
        )
    )
    contracts = {"spot": t, "pool": pool, "f564": e.a, "fETH": e.b}

    def views(n, specs):
        out = []
        for label, name, *args in specs:
            if name == "slot0":
                out.append((isqrt((1 << 192) // 980),))
            elif name == "oraclePrice":
                out.append((1000 * WAD, 1800))
            else:
                out.append(
                    getattr(contracts[label].functions, name)(*args).call(
                        block_identifier=n
                    )
                )
        return out

    trader = SimpleNamespace(
        live=True,
        chain=SimpleNamespace(w3=w, contracts=contracts, views=views, header=header),
        directory=folder,
        sender=w,
        signer=w.eth.account.from_key(
            w.provider.ethereum_tester.backend.account_keys[1].to_bytes()
        ),
    )
    worker = spot.Spot(trader)
    check = lambda: worker.step(header("latest")[0])
    assert check()["status"] == "waiting_for_confirmed_sale"
    sales.append(sale)
    assert check()["side"] == "buy"
    fill, _ = spot.inventory(folder)
    assert fill["received"] == size // 980
    assert fill["quote_cap_raw"] == cap * WAD
    if cap == 250:
        # A position from the original worker has no cap field; it must still exit.
        path = next((folder / "actions").glob("*-buy-*.json"))
        row = json.loads(path.read_text())
        del row["spot_fill"]["quote_cap_raw"]
        path.write_text(json.dumps(row))
    worker = spot.Spot(trader)
    assert check()["reason"] == "take_profit"
    assert spot.inventory(folder)[0] is None
    assert (
        e.b.functions.balanceOf(t.address).call() == WAD
        and e.a.functions.balanceOf(t.address).call() > cash * WAD
    )
    assert check()["status"] == "entry_cooldown"
    records = list((folder / "actions").glob("*.json"))
    assert len(records) == 2 and all(
        json.loads(p.read_text())["gas_cost_wei"] > 0 for p in records
    )
    buy = next(p for p in records if "-buy-" in p.name)
    row = json.loads(buy.read_text())
    row["spot_fill"]["received"] += 1
    buy.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="inconsistent spot position"):
        spot.inventory(folder)
