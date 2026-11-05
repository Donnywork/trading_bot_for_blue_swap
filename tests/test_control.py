import json

import pytest

import control
import history
import strategy


def test_missing_file_means_defaults(tmp_path):
    assert control.load(tmp_path / "control.json") == strategy.DEFAULT


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "logs" / "control.json"
    config = strategy.parse_config({"strategy": "breakout", "stop_loss_pct": 2.5})
    control.save(path, config)
    assert control.load(path) == config
    assert not list(path.parent.glob("*.tmp"))


def test_the_buttons_are_saved_beside_the_settings_but_not_in_the_form_view(tmp_path):
    path = tmp_path / "control.json"
    config = strategy.parse_config({"paused": True, "scan_request": 1_700_000_123})
    control.save(path, config)
    on_disk = json.loads(path.read_text())
    assert on_disk["paused"] is True and on_disk["scan_request"] == 1_700_000_123
    loaded = control.load(path)
    assert loaded.paused and loaded.scan_request == 1_700_000_123
    # the settings form never sees them (it would post them back and could undo a click)
    assert "paused" not in strategy.to_dict(config)
    assert "scan_request" not in strategy.to_dict(config)


def test_a_file_from_before_the_buttons_existed_means_not_paused_and_no_request(
    tmp_path,
):
    path = tmp_path / "control.json"
    path.write_text(json.dumps(strategy.to_dict(strategy.DEFAULT)))
    loaded = control.load(path)
    assert loaded.paused is False and loaded.scan_request == 0


@pytest.mark.parametrize(
    "text",
    [
        '{"paused": "yes"}',
        '{"paused": 1}',
        '{"paused": null}',
        '{"scan_request": -1}',
        '{"scan_request": 1.5}',
        '{"scan_request": "5"}',
        '{"scan_request": true}',
        '{"scan_request": 1e12}',
    ],
)
def test_a_button_value_that_makes_no_sense_is_an_invalid_file(tmp_path, text):
    path = tmp_path / "control.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="control.json"):
        control.load(path)


@pytest.mark.parametrize("text", ["", "{nope", "[]", '{"strategy": "yolo"}'])
def test_an_invalid_file_is_an_error_naming_the_file(tmp_path, text):
    path = tmp_path / "control.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="control.json"):
        control.load(path)


# ---------------------------------------------------------------- price history


def test_history_append_and_tail(tmp_path):
    path = tmp_path / "logs" / "prices.csv"
    for i in range(5):
        history.append(path, 1000 + i, 100.5 + i)
    assert history.tail(path, 3) == [(1002, 102.5), (1003, 103.5), (1004, 104.5)]
    assert len(history.tail(path, 100)) == 5


def test_history_tail_of_a_missing_file_and_skips_torn_lines(tmp_path):
    assert history.tail(tmp_path / "none.csv", 10) == []
    path = tmp_path / "prices.csv"
    path.write_text("1000,1.5\nthis is not a line\n1001,\n1002,2.5\n")
    assert history.tail(path, 10) == [(1000, 1.5), (1002, 2.5)]


def test_history_drops_the_oldest_half_when_it_grows_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "MAX_BYTES", 200)
    path = tmp_path / "prices.csv"
    for i in range(100):
        history.append(path, 1000 + i, 100.0)
    points = history.tail(path, 1000)
    assert points[-1][0] == 1099  # the newest is always kept
    assert 0 < len(points) < 100
    assert path.stat().st_size <= 200 + 20


@pytest.mark.parametrize(
    "text",
    [
        '{"strategy": ["x"]}',
        '{"strategy": {}}',
        '{"take_profit_pct": ' + "9" * 400 + "}",
    ],
)
def test_odd_json_in_the_file_is_an_invalid_file_not_a_crash(tmp_path, text):
    path = tmp_path / "control.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="control.json"):
        control.load(path)


def test_history_skips_prices_that_are_not_prices(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text("1,100.0\n2,nan\n3,inf\n4,0.0\n5,-3.0\n6,101.0\n")
    assert history.tail(path, 10) == [(1, 100.0), (6, 101.0)]


def test_history_append_starts_a_new_line_after_a_torn_one(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text("1,100.0\n2,10")  # a crash cut the second line short
    history.append(path, 3, 101.0)
    assert history.tail(path, 10) == [(1, 100.0), (2, 10.0), (3, 101.0)]
    # the cut line still parses as a (wrong) number; what matters is that sample 3 was not lost


def test_trailing_run_keeps_only_the_recent_unbroken_stretch():
    now = 10_000
    points = [(100, 1.0), (130, 2.0), (9_800, 3.0), (9_830, 4.0), (9_860, 5.0)]
    assert history.trailing_run(points, now, 600) == [
        3.0,
        4.0,
        5.0,
    ]  # cut at the long gap
    assert (
        history.trailing_run(points, now + 5_000, 600) == []
    )  # the newest point is too old
    assert history.trailing_run(points, 9_000, 600) == []  # dated in the future
    assert history.trailing_run([], now, 600) == []
