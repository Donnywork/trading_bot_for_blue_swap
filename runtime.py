"""Runtime configuration and diagnostics shared by executable entry points."""

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from requests import HTTPError
from web3.exceptions import Web3RPCError

ENV_FILE = Path(__file__).resolve().parent / ".env"


class ConfigurationError(ValueError):
    """An operator-facing error containing setting names, never secret values."""


def load_environment():
    # Exported variables win. Never search a parent directory for another project's secrets.
    load_dotenv(ENV_FILE, override=False)


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def http_status(exc):
    if isinstance(exc, HTTPError) and exc.response is not None:
        code = exc.response.status_code
        if type(code) is int and 100 <= code <= 599:
            return code
    return None


def error_kind(exc):
    code = http_status(exc)
    return f"{type(exc).__name__} (HTTP {code})" if code else type(exc).__name__


def rejected_log_range(exc):
    if http_status(exc) in (400, 413, 422):
        return True
    if not isinstance(exc, Web3RPCError):
        return False
    response = getattr(exc, "rpc_response", None)
    error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict) and error.get("code") in (401, 403, 429):
        return False
    message = error.get("message", "") if isinstance(error, dict) else str(exc)
    # Inspect provider text only for classification; never publish the body.
    if not isinstance(message, str):
        return False
    text = message.lower()
    if any(
        word in text
        for word in (
            "quota",
            "rate limit",
            "rate-limit",
            "too many requests",
            "compute units",
            "credits",
            "unauthorized",
            "unauthorised",
            "authentication",
            "access denied",
            "api key",
            "api-key",
            "forbidden",
        )
    ):
        return False
    return any(
        word in text
        for word in (
            "block range",
            "plan range exceeded",
            "query returned more than",
            "too many results",
            "response size exceeded",
            "log response size exceeded",
        )
    ) or bool(
        re.search(r"(?:limit|limited|maximum|at most).{0,80}\b\d+\s+blocks\b", text)
    )


def error_summary(exc):
    # Provider exception bodies can contain credentials. Preserve the failure location instead.
    if isinstance(exc, ConfigurationError):
        return str(exc)
    location = ""
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        location = f" at {Path(frame.f_code.co_filename).name}:{tb.tb_lineno} ({frame.f_code.co_name})"
        tb = tb.tb_next
    return f"{error_kind(exc)}{location}"
