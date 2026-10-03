from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, available_timezones

from tzlocal import get_localzone_name

logger = logging.getLogger(__name__)


def local_timezone_name(fallback: str) -> str:
    """Use a named system zone so past dates retain their daylight-saving rules."""
    try:
        name = get_localzone_name()
        if name:
            ZoneInfo(name)
            return name
    except (OSError, LookupError, ValueError):
        logger.warning("Could not read the system timezone; using %s.", fallback, exc_info=True)
    return fallback


def timezone_choices(selected: str) -> tuple[str, ...]:
    """Offer installed IANA zones while retaining the current saved choice."""
    return tuple(sorted(available_timezones() | {"UTC", selected}))
