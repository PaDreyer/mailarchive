"""Convert inclusive local calendar days to exact UTC half-open intervals."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def local_days_to_utc(
    start_day: date | None, through_day: date | None, zone_name: str
) -> tuple[datetime | None, datetime | None]:
    zone = ZoneInfo(zone_name)
    start = (
        datetime.combine(start_day, time.min, zone).astimezone(timezone.utc) if start_day else None
    )
    end = (
        datetime.combine(through_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        if through_day
        else None
    )
    if start and end and start >= end:
        raise ValueError("The end date must follow the start date.")
    return start, end
