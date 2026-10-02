"""Bounded retry scheduling shared by intake and file delivery."""

from datetime import datetime, timedelta


def retry_at(attempts: int, current: datetime) -> datetime:
    if attempts < 1:
        raise ValueError("Retry attempts must be positive.")
    return current + timedelta(seconds=min(3600, 30 * 2 ** min(attempts - 1, 7)))


def retry_due(status: str, retry_after: str | None, current: datetime) -> bool:
    if status == "reserved" or not retry_after:
        return True
    try:
        return datetime.fromisoformat(retry_after) <= current
    except (TypeError, ValueError):
        return True
