"""Change only the execution adapter. The feed, snapshots and counters belong to the process."""

import json
from contextlib import ExitStack
from dataclasses import replace

from web3.exceptions import Web3Exception

import ledger
import operator_lock
from event_execution import Executor
from event_store import EventStore
from legacy_main import Halt
from runtime import ConfigurationError, error_summary, required


class Modes:
    def __init__(self, w3, pools, settings, directory, identity):
        self.w3, self.pools, self.settings = w3, pools, settings
        self.directory, self.identity = directory, identity
        self.lock = ExitStack()
        self.executor = None

    def close(self):
        self.lock.close()

    def startup(self):
        mode = self.settings.mode
        try:
            live = json.loads((self.directory / "execution_live.json").read_text())
            if live["position"] is not None or live["intent"] is not None:
                mode = "live"
        except FileNotFoundError:
            pass
        except (KeyError, TypeError, OSError, ValueError):
            raise Halt(
                "Cannot validate the live ledger. Reconcile before restarting."
            ) from None
        try:
            return self.activate(mode, startup=True)
        except ConfigurationError as exc:
            if mode == "live":
                raise operator_lock.SendBlocked(
                    f"Live recovery blocked. {exc}"
                ) from None
            raise

    def activate(self, mode, *, startup=False):
        current = self.executor
        was_live = current is not None and current.settings.mode == "live"
        if mode == "live" and not self.settings.live_enabled:
            raise ConfigurationError("Live execution requires DRY_RUN=false")
        if mode != "paper" and not self.settings.bot_address:
            raise ConfigurationError(
                "Simulation and Live require a configured Phase2TradingBot"
            )
        root = self.directory.parent
        with ExitStack() as pending:
            if not was_live and (not startup or mode == "live"):
                pending.enter_context(operator_lock.sender(root, "events"))
            if not startup:
                if any(
                    p.exists() for p in (root / "HALTED", self.directory / "HALTED")
                ):
                    raise operator_lock.SendBlocked(
                        "Reconcile the halt marker before changing mode"
                    )
                if (
                    current.store.data["position"] is not None
                    or current.store.data["intent"] is not None
                ):
                    raise operator_lock.SendBlocked(
                        "Open position or unresolved intent"
                    )
                if not current.store.path.is_file():
                    raise operator_lock.SendBlocked(
                        "Current execution ledger is missing"
                    )
                try:
                    legacy = ledger.load(root / "ledger.json")
                except (ValueError, OSError):
                    raise operator_lock.SendBlocked(
                        "Cannot validate legacy ledger"
                    ) from None
                if legacy["open"] is not None:
                    raise operator_lock.SendBlocked(
                        "Legacy position requires reconciliation"
                    )
                # Re-read persisted state under the sender lock, including a same-mode request.
                for name in dict.fromkeys(("live", current.settings.mode, mode)):
                    path = self.directory / f"execution_{name}.json"
                    if path.exists():
                        operator_lock.require_flat(path, "position", "intent")
                        if name != mode:
                            EventStore(self.directory, self.identity, name)
            settings = replace(self.settings, mode=mode)
            store = EventStore(self.directory, self.identity, mode)
            account = None
            if mode == "live":
                try:
                    account = self.w3.eth.account.from_key(required("PRIVATE_KEY"))
                except (TypeError, ValueError):
                    raise ConfigurationError(
                        "Live requires a valid operator PRIVATE_KEY"
                    ) from None
            try:
                candidate = Executor(self.w3, self.pools, settings, store, account)
                if (
                    mode != "live"
                    and (self.directory / "execution_live.json").is_file()
                ):
                    candidate.live_history = EventStore(
                        self.directory, self.identity, "live"
                    )
                if mode == "live" and not startup:
                    candidate.check_live_limits()
            except (OSError, Web3Exception) as exc:
                raise ConfigurationError(
                    f"Vault checks failed: {error_summary(exc)}"
                ) from None
            if mode == "live" and not was_live:
                self.lock = pending.pop_all()
            elif was_live and mode != "live":
                self.lock.close()
            self.executor = candidate
            return candidate
