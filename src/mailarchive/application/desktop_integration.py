"""Presentation-facing desktop installation commands and read models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class IntegrationError(RuntimeError):
    """Desktop integration could not be read or changed safely."""


@dataclass(frozen=True)
class IntegrationOptions:
    menu_entry: bool = True
    desktop_shortcut: bool = False


@dataclass(frozen=True)
class IntegrationState:
    schema_version: int = 1
    prompt_seen: bool = False
    installed_version: str = ""
    menu_entry: bool = False
    desktop_path: str = ""

    @property
    def options(self) -> IntegrationOptions:
        return IntegrationOptions(self.menu_entry, bool(self.desktop_path))


@dataclass(frozen=True)
class IntegrationStatus:
    state: IntegrationState
    application_path: Path
    desktop_available: bool
    application_present: bool


@dataclass(frozen=True)
class IntegrationResult:
    state: IntegrationState
    warnings: tuple[str, ...] = ()


class DesktopIntegrationPort(Protocol):
    def status(self) -> IntegrationStatus: ...

    def mark_prompt_seen(self) -> None: ...

    def apply(self, options: IntegrationOptions, *, start_at_login: bool) -> IntegrationResult: ...
