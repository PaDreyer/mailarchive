from __future__ import annotations

from zoneinfo import available_timezones


def timezone_choices(selected: str) -> tuple[str, ...]:
    """Offer installed IANA zones while retaining the current saved choice."""
    return tuple(sorted(available_timezones() | {"UTC", selected}))
