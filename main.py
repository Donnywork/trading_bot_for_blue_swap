"""Start the Phase 2 bot."""

from event_service import cli
from legacy_main import redact

__all__ = ["cli", "redact"]  # Historical read-only research scripts import redact here.

if __name__ == "__main__":
    cli()
