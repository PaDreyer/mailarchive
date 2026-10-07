"""Typed activity records assembled from durable processing state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ActivityKind = Literal["operation", "mail"]


@dataclass(frozen=True, slots=True)
class ActivityItem:
    key: str
    kind: ActivityKind
    status: str
    occurred_at: str
    finished_at: str | None
    source_id: str | None
    address: str | None
    subject: str | None
    rule_name: str | None
    summary: str
    can_stop: bool = False
    can_retry: bool = False
    mail_count: int = 0
    completed_outputs: int = 0
    previously_archived_outputs: int = 0
    failed_outputs: int = 0
    pending_outputs: int = 0
    rejected_messages: int = 0


@dataclass(frozen=True, slots=True)
class OutputAttempt:
    number: int
    status: str
    final_path: str
    started_at: str
    finished_at: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OutputResult:
    output_id: int
    status: str
    final_path: str
    requested_path: str
    error: str | None
    completed_at: str | None
    attempts: tuple[OutputAttempt, ...]
    previously_archived: bool = False

    @property
    def can_open(self) -> bool:
        return self.status == "done" and bool(self.final_path)


@dataclass(frozen=True, slots=True)
class MailResult:
    key: str
    source_id: str
    address: str
    subject: str | None
    status: str
    received_at: str | None
    rule_name: str | None
    error: str | None
    outputs: tuple[OutputResult, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceResult:
    source_id: str
    address: str
    status: str
    error: str | None


@dataclass(frozen=True, slots=True)
class OperationAttempt:
    number: int
    started_at: str
    finished_at: str | None
    status: str
    error: str | None
    sources: tuple[SourceResult, ...]


@dataclass(frozen=True, slots=True)
class ActivityDetail:
    item: ActivityItem
    mail: tuple[MailResult, ...]
    error: str | None = None
    sources: tuple[SourceResult, ...] = ()
    attempts: tuple[OperationAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivityPage:
    items: tuple[ActivityItem, ...]
    next_before: tuple[str, str] | None


class ActivityReader(Protocol):
    def current(self) -> tuple[ActivityItem, ...]: ...

    def history(
        self, *, before: tuple[str, str] | None = None, limit: int = 100
    ) -> ActivityPage: ...

    def detail(self, key: str) -> ActivityDetail: ...


class ActivityQueries:
    """Read-only projection of operations and actual mail outcomes."""

    def __init__(self, reader: ActivityReader) -> None:
        self.reader = reader

    def current(self) -> tuple[ActivityItem, ...]:
        return self.reader.current()

    def history(self, *, before: tuple[str, str] | None = None, limit: int = 100) -> ActivityPage:
        if limit < 1:
            raise ValueError("Activity page size must be positive.")
        return self.reader.history(before=before, limit=limit)

    def detail(self, key: str) -> ActivityDetail:
        return self.reader.detail(key)
