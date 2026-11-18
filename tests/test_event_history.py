import copy
import json
from pathlib import Path

import pytest

import event_history as history


@pytest.fixture
def snapshot():
    return json.loads(
        (
            Path(__file__).parents[1]
            / "research/2026-11-17-phase2-arbitrage/snapshot.json"
        ).read_text()
    )


def test_price_is_quote_per_base_for_reversed_tokens(snapshot):
    for p in snapshot["profiles"]:
        p["sqrt" + "PriceX96"] = 2**95  # raw token1/token0 = 0.25 fETH per f564
    row = history.observation(snapshot, "simulate", 100, 101)
    assert row["prices"] == {"500": 4, "10000": 4}
    assert row["identity"]["chain_id"] == 11155111


def test_recorder_deduplicates_bounds_and_replaces_reorg_branch(
    snapshot, tmp_path, monkeypatch
):
    path = tmp_path / "prices.jsonl"
    recorder = history.Recorder(path)
    row = history.observation(snapshot, "live", 100, 101)
    recorder.append(row)
    before = path.read_bytes()
    recorder.append(row)
    assert path.read_bytes() == before
    # Deduplication also works when the recorder is reopened.
    history.Recorder(path).append(row)
    assert path.read_bytes() == before
    later = row | {
        "block": row["block"] + 1,
        "timestamp": row["timestamp"] + 12,
        "block_hash": "0x" + "aa" * 32,
    }
    recorder.append(later)
    replacement = row | {"block_hash": "0x" + "bb" * 32}
    recorder.append(replacement)
    assert history.read(path) == [replacement]
    monkeypatch.setattr(history, "MAX_BYTES", len(before) * 5)
    for i in range(10):
        recorder.append(
            row
            | {
                "block": row["block"] + i,
                "timestamp": row["timestamp"] + i * 12,
                "block_hash": "0x" + f"{i:064x}",
            }
        )
    assert path.stat().st_size <= history.MAX_BYTES
    assert 1 <= len(history.read(path)) < 10
    assert history.read(path)[-1]["block"] == row["block"] + 9


def test_segments_preserve_mode_session_and_time_gaps(snapshot):
    row = history.observation(snapshot, "live", 1, 1)
    rows = [
        row | {"timestamp": t, "session": session, "mode": mode}
        for t, session, mode in (
            (100, 1, "live"),
            (112, 1, "live"),
            (124, 1, "simulate"),
            (136, 1, "live"),
            (148, 2, "live"),
            (240, 2, "live"),
        )
    ]
    assert [[p[0] for p in s] for s in history.segments(rows, "live", 500, 0, 300)] == [
        [100, 112],
        [136],
        [148],
        [240],
    ]


@pytest.mark.parametrize(
    "damage", ["identity", "price", "mode", "truncated", "price_list"]
)
def test_corrupt_history_is_reported_and_never_overwritten(snapshot, tmp_path, damage):
    path = tmp_path / "prices.jsonl"
    row = history.observation(snapshot, "paper", 100, 101)
    broken = copy.deepcopy(row)
    if damage == "identity":
        broken["identity"]["chain_id"] = 1
    elif damage == "price":
        broken["prices"]["500"] = float("nan")
    elif damage == "mode":
        broken["mode"] = "legacy"
    elif damage == "price_list":
        broken["prices"] = ["500", "10000"]
    text = "{broken" if damage == "truncated" else json.dumps(broken) + "\n"
    path.write_text(text)
    with pytest.raises(ValueError):
        history.Recorder(path).append(row)
    assert path.read_text() == text
    with pytest.raises(ValueError):
        history.Recorder(path).invalidate()
    assert path.read_text() == text


@pytest.mark.parametrize("canonical", [True, False])
def test_restart_revalidates_saved_price_anchor_before_appending(
    snapshot, tmp_path, canonical
):
    path = tmp_path / "prices.jsonl"
    row = history.observation(snapshot, "live", 1, 1)
    history.Recorder(path).append(row)
    recorder = history.Recorder(path)
    reads = []
    recorder.revalidate(
        lambda n: (
            reads.append(n)
            or {
                "hash": bytes.fromhex(row["block_hash"][2:] if canonical else "ef" * 32)
            }
        )
    )
    recorder.append(
        row
        | {
            "block": row["block"] + 2,
            "block_hash": "0x" + "cd" * 32,
            "timestamp": row["timestamp"] + 24,
        }
    )
    recorder.revalidate(lambda n: pytest.fail("repeated recovery read"))
    rows = history.read(path)
    assert reads == [row["block"]]
    assert rows[0].get("canonical", True) == canonical
    assert sum(
        map(len, history.segments(rows, "all", 500, 0, row["timestamp"] + 30))
    ) == (2 if canonical else 1)


def test_price_anchor_read_failure_preserves_file_and_retries(snapshot, tmp_path):
    path = tmp_path / "prices.jsonl"
    row = history.observation(snapshot, "live", 1, 1)
    history.Recorder(path).append(row)
    before = path.read_bytes()
    recorder = history.Recorder(path)

    def fail(n):
        raise ConnectionError("unavailable")

    with pytest.raises(ConnectionError):
        recorder.revalidate(fail)
    assert not recorder.verified and path.read_bytes() == before
    recorder.revalidate(lambda n: {"hash": bytes.fromhex("ef" * 32)})
    assert not history.read(path)[0]["canonical"]
