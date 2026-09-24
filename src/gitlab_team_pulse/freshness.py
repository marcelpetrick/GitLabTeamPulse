"""Freshness classification shared by the API and the UI.

``status`` is one of fresh / refreshing / stale / error / never. The independent ``stale`` and
``error`` flags keep their meaning even when ``status`` shows a higher-priority state, e.g. a
refresh running on top of stale data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Freshness(StrEnum):
    FRESH = "fresh"
    REFRESHING = "refreshing"
    STALE = "stale"
    ERROR = "error"
    NEVER = "never"


FAILED_STATUSES = {"error", "partial"}


@dataclass(frozen=True)
class FreshnessInfo:
    status: Freshness
    stale: bool
    error: bool
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    age_seconds: float | None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "stale": self.stale,
            "error": self.error,
            "last_success_at": self.last_success_at,
            "last_attempt_at": self.last_attempt_at,
            "age_seconds": self.age_seconds,
            "message": self.message,
        }


def classify(
    *,
    last_success_at: datetime | None,
    last_attempt_at: datetime | None,
    last_status: str | None,
    running: bool,
    interval_seconds: float,
    grace_seconds: float,
    now: datetime,
    message: str | None = None,
) -> FreshnessInfo:
    """Classify a dataset; precedence is refreshing > never > error > stale > fresh."""
    age = (now - last_success_at).total_seconds() if last_success_at else None
    error = last_status in FAILED_STATUSES
    stale = age is None or age > interval_seconds + grace_seconds
    if running:
        status = Freshness.REFRESHING
    elif last_success_at is None:
        status = Freshness.NEVER
    elif error:
        status = Freshness.ERROR
    elif stale:
        status = Freshness.STALE
    else:
        status = Freshness.FRESH
    return FreshnessInfo(
        status=status,
        stale=stale and last_success_at is not None,
        error=error,
        last_success_at=last_success_at,
        last_attempt_at=last_attempt_at,
        age_seconds=age,
        message=message if error else None,
    )
