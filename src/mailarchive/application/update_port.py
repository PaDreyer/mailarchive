"""Update information returned to the desktop presentation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    url: str
