"""UTC timestamps at the local persistence boundary."""

from datetime import datetime, timezone

from mailarchive.application.retry_policy import retry_at
from mailarchive.application.retry_policy import retry_due as intake_retry_due

__all__ = ["now", "next_retry_after", "intake_retry_due"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_retry_after(attempts: int) -> str:
    return retry_at(attempts, datetime.now(timezone.utc)).isoformat()
