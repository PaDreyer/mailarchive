"""Dependencies and read models for one active local profile."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from mailarchive.application.account_status import AccountStatusService
from mailarchive.application.activity import ActivityQueries
from mailarchive.application.events import RunProgress, ServiceEvent
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.domain.configuration import Settings


@dataclass(frozen=True, slots=True)
class ApplicationStatus:
    pending_count: int
    spool_bytes: int


@dataclass(frozen=True, slots=True)
class MonitoringStatus:
    status: str


@dataclass(frozen=True, slots=True)
class PausedScope:
    source_id: str
    scope_key: str
    error: str | None


@dataclass(frozen=True, slots=True)
class LogPage:
    events: tuple[ServiceEvent, ...]
    total: int
    offset: int


class Diagnostics(Protocol):
    def record(self, event: ServiceEvent) -> None: ...

    def page(
        self, *, since: datetime | None = None, offset: int = 0, limit: int = 50
    ) -> LogPage: ...

    def clear(self) -> None: ...


class Execution(Protocol):
    def start(self) -> None: ...

    def shutdown(self, *, timeout: float = 5.0) -> bool:
        """Wait for owned processing and settlement I/O within one time budget.

        ExecutionShutdownError reports failed settlement together with the actual
        stopped state; a storage error must not prevent cancellation or joining.
        """
        ...

    def check_mail_now(self) -> str | None: ...

    def stop_check(self, check_id: str) -> bool: ...

    def settings_changed(self, settings: Settings) -> None: ...

    def automatic_monitoring_state(self) -> AutomaticMonitoringState: ...

    def apply_to_past_mail(
        self,
        rule_id: str,
        start: datetime | None,
        end: datetime | None,
        timezone_name: str,
    ) -> str: ...

    def stop_operation(self, operation_id: str) -> None: ...

    def retry_activity(self, key: str) -> None: ...


class ProfileQueries(Protocol):
    def status(self) -> ApplicationStatus: ...

    def monitoring_status(self, source_id: str, folders: list[str]) -> MonitoringStatus: ...

    def paused_scopes(self, account_id: str) -> tuple[PausedScope, ...]: ...


@dataclass(slots=True)
class ProfileContext:
    database_path: Path
    settings: Settings
    save: Callable[[Settings], None]
    execution: Execution
    activity: ActivityQueries
    diagnostics: Diagnostics
    queries: ProfileQueries
    account_change: Callable[[], AbstractContextManager[None]]
    reset_scope: Callable[[str, str], int]
    report: Callable[[ServiceEvent], None]
    account_statuses: AccountStatusService | None = None


class ProfileManager(Protocol):
    @property
    def path(self) -> Path: ...

    def open(
        self,
        path: Path,
        on_event: Callable[[ServiceEvent], None],
        on_progress: Callable[[RunProgress], None],
    ) -> ProfileContext:
        """Open a profile without publishing its location.

        ProfileUnavailableError denotes retryable access failures throughout
        context creation. Invalid or damaged profiles raise fatal WorkspaceError.
        """
        ...

    def activate(self, path: Path) -> None: ...
