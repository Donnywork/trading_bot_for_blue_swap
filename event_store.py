"""Public event-service state and durable execution intent. No secret fields."""

import json
import math
import os
import time
from pathlib import Path

import event_market as market
import ledger
from legacy_main import Halt, redact

DIRECTORY = ledger.LOG_DIR / "events"


class EventStore:
    def __init__(self, directory, identity, mode, clock=time.time):
        self.directory, self.clock = Path(directory), clock
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"execution_{mode}.json"
        self.halt_path = self.directory / "HALTED"
        if self.halt_path.exists():
            raise Halt(
                "event service halted; reconcile before manually clearing events/HALTED"
            )
        if mode != "live" and (self.directory / "execution_live.json").exists():
            try:
                live = json.loads((self.directory / "execution_live.json").read_text())
                if (
                    live["position"] is not None
                    or live["intent"] is not None
                    or live.get("intent_extra") is not None
                ):
                    raise Halt(
                        "live event inventory/intent requires live reconciliation before changing modes"
                    )
            except (KeyError, TypeError, ValueError, OSError):
                raise Halt(
                    "cannot verify live execution ledger before changing modes"
                ) from None
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {
                "version": 1,
                "identity": identity,
                "intent": None,
                "position": None,
                "attempts": {},
                "trades": [],
                "exit_done": 0,
            }
            self.save()
        except (ValueError, OSError) as exc:
            self.halt("unreadable event execution ledger")
            raise Halt("unreadable event execution ledger") from exc
        if (
            not isinstance(self.data, dict)
            or self.data.get("version") != 1
            or self.data.get("identity") != identity
            or not {"intent", "position", "attempts", "trades", "exit_done"}
            <= set(self.data)
            or not isinstance(self.data["attempts"], dict)
            or not isinstance(self.data["trades"], list)
        ):
            self.halt("event execution ledger identity/schema mismatch")
        # The sweep slot postdates the first ledgers; absent means none.
        self.data.setdefault("intent_extra", None)
        if self.data["intent"] is not None or self.data["intent_extra"] is not None:
            self.halt(
                "unfinished execution intent; reconcile transaction before restart"
            )
        p = self.data["position"]
        if p is not None and (
            not isinstance(p, dict)
            or not {
                "base_raw",
                "quote_raw",
                "opened",
                "target",
                "target_nonce",
                "target_sender",
                "exit_fee",
            }
            <= set(p)
            or type(p["base_raw"]) is not int
            or p["base_raw"] <= 0
            or type(p["quote_raw"]) is not int
            or p["quote_raw"] <= 0
        ):
            self.halt("invalid event position")
        if p is not None:
            try:
                market.hex_value(p["target"], 32)
                market.hex_value(p["target_sender"], 20)
                market.hex_value(p["entry_tx"], 32)
                market.hex_value(p["entry_block_hash"], 32)
                if (
                    type(p["target_nonce"]) is not int
                    or p["target_nonce"] < 0
                    or type(p["entry_block"]) is not int
                    or p["entry_block"] < 0
                    or type(p["opened"]) not in (float, int)
                    or not math.isfinite(p["opened"])
                    or p["opened"] <= 0
                    or p["exit_fee"] not in market.POOLS
                ):
                    raise ValueError("invalid position metadata")
            except (ValueError, KeyError, TypeError):
                self.halt("invalid event position metadata")
        if (
            type(self.data["exit_done"]) is not int
            or self.data["exit_done"] < 0
            or any(
                v not in ("simulated", "confirmed", "not_sent", "reverted")
                for v in self.data["attempts"].values()
            )
        ):
            self.halt("invalid event execution history")
        for row in self.data["trades"]:
            if not isinstance(row, dict) or row.get("status") not in (
                "confirmed",
                "not_sent",
                "simulated",
                "reverted",
            ):
                self.halt("invalid execution result")
            if row["status"] == "confirmed":
                try:
                    market.hex_value(row["block_hash"], 32)
                    market.hex_value(row["tx_hash"], 32)
                    if type(row["block"]) is not int or row["block"] < 0:
                        raise ValueError("invalid block")
                except (ValueError, KeyError, TypeError):
                    self.halt("invalid confirmed receipt metadata")

    def save(self):
        ledger.atomic_write(self.path, json.dumps(self.data, allow_nan=False))
        self.sync_directory()

    def sync_directory(self):
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def scan_request_done(self):
        path = self.directory / "scan.json"
        try:
            if path.exists():
                values = [json.loads(path.read_text())["request"]]
            else:
                # Migrate acknowledgements from all modes; switching mode cannot repeat a scan.
                values = [self.data.get("scan_done", 0)]
                for mode in ("paper", "simulate", "live"):
                    legacy = self.directory / f"execution_{mode}.json"
                    if legacy.exists():
                        values.append(
                            json.loads(legacy.read_text()).get("scan_done", 0)
                        )
            if any(type(v) is not int or v < 0 for v in values):
                raise ValueError("invalid scan acknowledgement")
            return max(values)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.halt(
                "unreadable scan acknowledgement; restore before requesting another scan"
            )

    def scan_status(self):
        try:
            status = json.loads((self.directory / "scan.json").read_text()).get(
                "status", "Acknowledged"
            )
        except FileNotFoundError:
            return "Idle"
        return (
            "Previous scan interrupted. Request another scan to retry."
            if status == "Scanning"
            else status
        )

    def scan_result(self, request, status):
        ledger.atomic_write(
            self.directory / "scan.json",
            json.dumps({"request": request, "status": status}),
        )
        self.sync_directory()

    def acknowledge_scan(self, request):
        self.scan_result(request, "Scanning")
        self.data["scan_done"] = request
        self.save()

    def halt(self, reason):
        ledger.atomic_write(self.halt_path, redact(str(reason)))
        raise Halt(redact(str(reason)))

    def state(self, value):
        ledger.atomic_write(
            self.directory / "state.json", redact(json.dumps(value, allow_nan=False))
        )

    def decision(self, value):
        # Keep disk bounded; the dashboard reads only state.json, which contains recent rows.
        path = self.directory / "decisions.jsonl"
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(self.directory / "decisions.previous.jsonl")
        with path.open("a") as stream:
            stream.write(redact(json.dumps(value, allow_nan=False)) + "\n")

    def reserve_seq(self):
        # Exit retries need fresh keys after a definite pre-send failure, and
        # len(attempts) stops being unique once attempts are trimmed. A
        # persisted monotonic counter never repeats; an unpersisted increment
        # (reserve without a begin) merely skips a number.
        self.data["attempt_seq"] = self.data.get("attempt_seq", 0) + 1
        return self.data["attempt_seq"]

    def begin(self, key, details):
        if key in self.data["attempts"] or self.data["intent"] is not None:
            raise ValueError("execution already attempted")
        self.data["intent"] = {"key": key, "at": self.clock(), **details}
        self.data["attempts"][key] = "inflight"
        # attempts gate duplicate sends only; keep the ledger bounded like
        # trades so the pre-broadcast save stays fast as days accumulate.
        # The fresh intent key is always last and can never be trimmed away.
        if len(self.data["attempts"]) > 1000:
            kept = dict(list(self.data["attempts"].items())[-1000:])
            self.data["attempts"] = kept
        self.save()  # durable BEFORE any possible broadcast

    def begin_extra(self, key, details):
        # The next-block sweep shares a race window with the primary attempt:
        # its own durable slot, saved BEFORE its broadcast, settled
        # independently so either outcome never leaves the other unrecorded.
        if key in self.data["attempts"] or self.data["intent_extra"] is not None:
            raise ValueError("sweep execution already attempted")
        self.data["intent_extra"] = {"key": key, "at": self.clock(), **details}
        self.data["attempts"][key] = "inflight"
        if len(self.data["attempts"]) > 1000:
            kept = dict(list(self.data["attempts"].items())[-1000:])
            self.data["attempts"] = kept
        self.save()  # durable BEFORE any possible broadcast

    def finish_extra(self, key, status, row):
        # Never touches `position` or the primary intent: the primary
        # attempt's finish owns the position lifecycle.
        self.data["attempts"][key] = status
        self.data["intent_extra"] = None
        self.data["trades"].append({"at": self.clock(), "status": status, **row})
        self.data["trades"] = self.data["trades"][-200:]
        self.save()

    def finish(self, key, status, position, row):
        self.data["attempts"][key] = status
        self.data["position"] = position
        self.data["intent"] = None
        self.data["trades"].append({"at": self.clock(), "status": status, **row})
        self.data["trades"] = self.data["trades"][-200:]
        self.save()


def dashboard_view(path, now):
    # Only files under logs; no dotenv loading, sockets, or chain queries.
    try:
        value = json.loads(Path(path).read_text())
        if (
            not isinstance(value, dict)
            or type(value.get("updated"))
            not in (
                int,
                float,
            )
            or not math.isfinite(value["updated"])
        ):
            raise ValueError("invalid event status")
        value["stale"] = not 0 <= now - value["updated"] <= 30
    except FileNotFoundError:
        value = {
            "health": "not_started",
            "stale": True,
            "message": "Event service has not written status yet.",
        }
    except (ValueError, OSError, TypeError):
        value = {
            "health": "unreadable",
            "stale": True,
            "message": "Event status is unreadable; entries must remain paused.",
        }
    if Path(path).with_name("HALTED").exists() and value.get("health") != "halted":
        value.update(
            health="halted",
            message="Execution halted. Reconcile events/HALTED before restarting.",
        )
    return value
