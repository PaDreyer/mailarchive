"""Pure archive filename and destination template rules."""

from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha256
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
MAX_FILENAME_BYTES = 255


def safe_filename(value: str, fallback: str = "File", max_length: int = 100) -> str:
    cleaned = _INVALID_FILENAME.sub("_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:max_length].rstrip(" .") or fallback


def bounded_filename(value: str, *, max_bytes: int = MAX_FILENAME_BYTES, number: int = 1) -> str:
    """Fit a physical filename, preserving its extension and collision suffix."""
    if max_bytes < 1 or number < 1:
        raise ValueError("Filename limits and collision numbers must be positive.")
    path = Path(value)
    stem, extension = path.stem, path.suffix
    collision = "" if number == 1 else f"-{number}"
    result = f"{stem}{collision}{extension}"
    if len(result.encode("utf-8")) <= max_bytes:
        return result
    identity = "-" + sha256(value.encode("utf-8")).hexdigest()[:12]
    suffix = identity + collision + extension
    if len(suffix.encode("utf-8")) >= max_bytes:
        stem, extension = value, ""
        suffix = identity + collision
    budget = max_bytes - len(suffix.encode("utf-8"))
    if budget < 1:
        raise ValueError("The filesystem filename limit is too small for an archive name.")
    shortened = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")
    return (shortened or "File"[:budget]) + suffix


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
