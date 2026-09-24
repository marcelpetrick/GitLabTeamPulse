"""Human-readable stdout logging with secret redaction.

Lines look like ``2026-09-24 10:00:00 INFO    [sync] refreshed user=42 duration=1.2s`` so they stay
useful in ``docker logs``. Loggers live below ``teampulse.<subsystem>``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

ROOT_LOGGER = "teampulse"
REDACTED = "***"


def get_logger(subsystem: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT_LOGGER}.{subsystem}")


class SubsystemFormatter(logging.Formatter):
    """Adds a ``[subsystem]`` column derived from the logger name."""

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        prefix = f"{ROOT_LOGGER}."
        record.subsystem = name[len(prefix) :] if name.startswith(prefix) else name
        return super().format(record)


class RedactingFilter(logging.Filter):
    """Replaces every configured secret value in the rendered message."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self.secrets:
            redacted = redacted.replace(secret, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> logging.Handler:
    """Install one stdout handler on the root logger; idempotent across calls."""
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name("teampulse")
    handler.setFormatter(
        SubsystemFormatter(
            fmt="%(asctime)s %(levelname)-7s [%(subsystem)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == "teampulse":
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it quiet unless debugging.
    logging.getLogger("httpx").setLevel(logging.WARNING if level != "DEBUG" else logging.DEBUG)
    return handler
