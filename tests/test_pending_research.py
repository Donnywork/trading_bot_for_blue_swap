"""Offline checks that observation timing cannot be mistaken for submission provenance."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "research/2026-11-17-phase2-flow/pending.py"
SPEC = importlib.util.spec_from_file_location("pending_research", SCRIPT)
pending = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pending)


def state():
    return {
        "pending": {},
        "mined": set(),
        "nonces": {},
        "pending_notifications": 0,
        "mined_notifications": 0,
    }


def test_only_sepolia_alchemy_configuration_is_accepted(monkeypatch):
    for key in ("ALCHEMY_WS_URL", "RPC_URL", "RPC_FALLBACK_URLS"):
        monkeypatch.delenv(key, raising=False)
    assert pending.endpoint() is None
    monkeypatch.setenv("RPC_URL", "https://eth-mainnet.g.alchemy.com/v2/placeholder")
    assert pending.endpoint() is None
    monkeypatch.setenv(
        "RPC_FALLBACK_URLS", "https://eth-sepolia.g.alchemy.com/v2/placeholder"
    )
    assert pending.endpoint() == "wss://eth-sepolia.g.alchemy.com/v2/placeholder"


def test_filters_include_all_signers_and_capture_full_transaction_objects():
    signers = ["0x" + "11" * 20, "0x" + "22" * 20]
    first, second = pending.subscriptions(signers)
    assert first[1][1] == {"fromAddress": signers, "hashesOnly": False}
    assert second[1][1]["addresses"] == [{"from": s} for s in signers]
    assert second[1][1]["includeRemoved"] is True


def test_pending_duplicate_preserves_first_arrival_and_detects_replacement():
    s = state()
    tx = {"hash": "original", "from": "sender", "nonce": "0x1", "blockNumber": None}
    pending.observe(s, "pending", tx, 10)
    pending.observe(s, "pending", tx, 11)
    replacement = pending.observe(s, "pending", tx | {"hash": "replacement"}, 12)
    assert replacement["same_nonce_previously_seen_hash"] == "original"
    record = pending.observe(s, "mined", {"transaction": tx, "removed": False}, 15)
    assert record["notification_lead_seconds"] == 5


def test_mined_first_and_populated_block_number_are_not_pending_lead():
    s = state()
    tx = {"hash": "h", "from": "sender", "nonce": "0x1", "blockNumber": "0x10"}
    pending.observe(s, "pending", tx, 1)
    first = pending.observe(s, "mined", {"transaction": tx, "removed": False}, 2)
    assert first["seen_pending_before_mined_notification"] is False
    pending.observe(s, "pending", tx | {"blockNumber": None}, 3)
    second = pending.observe(s, "mined", {"transaction": tx, "removed": False}, 4)
    assert second["seen_pending_before_mined_notification"] is False
    removed = pending.observe(s, "mined", {"transaction": tx, "removed": True}, 5)
    assert removed["removed"] is True
    assert "notification_lead_seconds" not in removed
