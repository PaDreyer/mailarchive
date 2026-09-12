"""Run-scoped synchronization contract between the service and provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class SyncSession:
    cursor_for: Callable[[str], str | None]
    recheck_ids_for: Callable[[str], set[str]]
    report_reset: Callable[[], None] = lambda: None
    next_cursor: str | None = None
    discarded_ids: set[str] = field(default_factory=set)
    present_ids: set[str] = field(default_factory=set)

    def mark_present(self, message_id: str) -> None:
        self.present_ids.add(message_id)
        self.discarded_ids.discard(message_id)
