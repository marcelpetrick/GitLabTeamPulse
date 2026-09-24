from __future__ import annotations

import logging

import pytest

from gitlab_team_pulse.logging_setup import (
    REDACTED,
    RedactingFilter,
    configure_logging,
    get_logger,
)


def test_log_lines_are_readable_and_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", secrets=["s3cr3t", ""])
    get_logger("sync").info("token is %s for user=%d", "s3cr3t", 7)
    logging.getLogger("other").warning("plain")
    out = capsys.readouterr().out
    assert "INFO    [sync] token is *** for user=7" in out
    assert "s3cr3t" not in out
    assert "[other] plain" in out


def test_configure_is_idempotent() -> None:
    configure_logging("DEBUG")
    configure_logging("WARNING")
    handlers = [h for h in logging.getLogger().handlers if h.get_name() == "teampulse"]
    assert len(handlers) == 1
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
    configure_logging("DEBUG")
    assert logging.getLogger("httpx").level == logging.DEBUG


def test_filter_without_secrets_passes_through() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "hello world"


def test_filter_leaves_unrelated_messages_untouched() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    RedactingFilter(["token"]).filter(record)
    assert record.args == ("world",)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "has token", None, None)
    RedactingFilter(["token"]).filter(record)
    assert record.getMessage() == f"has {REDACTED}"
