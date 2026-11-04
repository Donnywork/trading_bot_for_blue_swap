from decimal import Decimal

import pytest

import ledger


def opened(base="0.0001", spent="0.01", price=1000.0, now=100):
    return ledger.open_position(
        ledger.empty(), price, Decimal(base), Decimal(spent), now
    )


def test_open_then_close_moves_the_position_to_the_history():
    data = opened()
    assert data["open"]["entry_price"] == 1000.0
    data = ledger.close_position(data, 1050.0, Decimal("0.0104"), "take_profit", 200)
    assert data["open"] is None
    (trade,) = data["closed"]
    assert (trade["opened"], trade["closed"], trade["exit_price"]) == (100, 200, 1050.0)
    assert ledger.profit(trade) == Decimal("0.0004")


def test_save_and_load_round_trip_keeps_decimals_exact(tmp_path):
    path = tmp_path / "logs" / "ledger.json"
    data = opened(base="0.000009443123456789", spent="0.01")
    ledger.save(path, data)
    assert ledger.load(path) == data
    assert Decimal(ledger.load(path)["open"]["base"]) == Decimal("0.000009443123456789")
    assert not list(path.parent.glob("*.tmp"))  # the temp file was renamed away


def test_a_missing_file_is_a_fresh_start(tmp_path):
    assert ledger.load(tmp_path / "nope.json") == ledger.empty()


@pytest.mark.parametrize(
    "text", ["", "{not json", "[]", '{"open": null}', '{"closed": 3}']
)
def test_a_broken_file_is_an_error_not_a_fresh_start(tmp_path, text):
    path = tmp_path / "ledger.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="unreadable"):
        ledger.load(path)


def test_summarize():
    data = ledger.empty()
    for received, reason in [
        ("0.0105", "take_profit"),
        ("0.0097", "stop_loss"),
        ("0.0102", "take_profit"),
    ]:
        data = ledger.open_position(data, 1000.0, Decimal("0.0001"), Decimal("0.01"), 0)
        data = ledger.close_position(data, 1000.0, Decimal(received), reason, 1)
    s = ledger.summarize(data)
    assert (s["trades"], s["wins"], s["losses"]) == (3, 2, 1)
    assert (s["take_profit"], s["stop_loss"]) == (2, 1)
    assert s["spent"] == Decimal("0.03")
    assert s["profit"] == Decimal("0.0004")
    assert s["profit_pct"] == pytest.approx(1.3333, rel=1e-3)


def test_summarize_with_no_trades():
    s = ledger.summarize(ledger.empty())
    assert (s["trades"], s["profit"], s["profit_pct"]) == (0, 0, 0.0)


# ---------------------------------------------------------------- shape checks


def good_open():
    return {
        "opened": 1,
        "entry_price": 1000.0,
        "base": "0.00001",
        "quote_spent": "0.01",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"open": {}, "closed": []},  # an empty record would be a KeyError mid-tick
        {"open": {"entry_price": 1.0}, "closed": []},
        {"open": {**good_open(), "base": "abc"}, "closed": []},
        {"open": {**good_open(), "entry_price": 0}, "closed": []},
        {"open": {**good_open(), "entry_price": "nan"}, "closed": []},
        {"open": {**good_open(), "entry_price": None}, "closed": []},
        {"open": [], "closed": []},
        {"open": None, "closed": [{"base": "1"}]},
        {"open": None, "closed": "x"},
        {"closed": []},
    ],
)
def test_a_damaged_record_is_rejected_when_loaded(tmp_path, bad):
    import json

    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="unreadable"):
        ledger.load(path)


def test_what_the_bot_writes_always_loads_back(tmp_path):
    data = ledger.open_position(
        ledger.empty(), 1000.0, Decimal("0.00001"), Decimal("0.01"), 1
    )
    ledger.save(tmp_path / "a.json", data)
    assert ledger.load(tmp_path / "a.json") == data
    data = ledger.close_position(data, 1050.0, Decimal("0.0105"), "take_profit", 2)
    ledger.save(tmp_path / "b.json", data)
    assert ledger.load(tmp_path / "b.json") == data


def test_concurrent_writers_never_leave_a_corrupt_file(tmp_path):
    import threading

    path = tmp_path / "control.json"
    errors = []

    def writer(size):
        try:
            for _ in range(50):
                ledger.atomic_write(path, '{"pad": "' + "x" * size + '"}')
        except OSError as e:  # a shared temp file would make the second rename fail
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n * 500,)) for n in range(1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    import json

    assert errors == []
    assert json.loads(path.read_text())["pad"].startswith("x")
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text("old")

    def fail(*a):
        raise OSError("disk full")

    monkeypatch.setattr(ledger.os, "replace", fail)
    with pytest.raises(OSError):
        ledger.atomic_write(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.glob("*.tmp")) == []
