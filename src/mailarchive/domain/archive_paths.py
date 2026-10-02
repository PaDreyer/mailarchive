"""Pure archive filename and destination template rules."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from string import Formatter

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_filename(value: str, fallback: str = "File", max_length: int = 100) -> str:
    cleaned = _INVALID_FILENAME.sub("_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:max_length].rstrip(" .") or fallback


def destination_path(destination: str, *, mail_date: datetime | None = None) -> Path:
    """Resolve a full user path without changing its components.

    Double braces escape literal braces. A missing date keeps placeholder text for
    previews; accepted plans always pass the provider reception date.
    """
    if not destination or destination != destination.strip():
        raise ValueError("Enter a full destination path without outer whitespace.")
    values = {
        "year": f"{mail_date.year:04d}" if mail_date else "YYYY",
        "month": f"{mail_date.month:02d}" if mail_date else "MM",
    }
    try:
        for _, field, spec, conversion in Formatter().parse(destination):
            if field not in {None, "year", "month"} or spec or conversion:
                raise ValueError("Use only {year}, {month}, {{ or }} in destination paths.")
        expanded = destination.format(**values)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid destination template: {exc}") from exc
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise ValueError("Enter a full destination path.")
    return candidate
