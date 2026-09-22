"""Run-scoped synchronization contract between the service and provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class SyncSession:
    cursor_for: Callable[[str], str | None]
    recheck_ids_for: Callable[[str], set[str]]
    report_reset: Callable[[], None] = lambda: None
    baseline: bool = False
    next_cursor: str | None = None
    discarded_ids: set[str] = field(default_factory=set)
    present_ids: set[str] = field(default_factory=set)

    def mark_present(self, message_id: str) -> None:
        self.present_ids.add(message_id)
        self.discarded_ids.discard(message_id)

    def discard(self, message_id: str) -> None:
        self.discarded_ids.add(message_id)
        self.present_ids.discard(message_id)


@dataclass(slots=True)
class RangePagination:
    """Durable provider continuation state for one manual range target."""

    resume_namespace: str | None
    resume_token: str | None
    save: Callable[[str, str | None, bool, bool], bool]
    namespace: str | None = None
    complete: bool = False

    def start(self, namespace: str) -> str | None:
        self.namespace = namespace
        if self.resume_namespace != namespace:
            self.resume_namespace = namespace
            self.resume_token = None
            self.complete = False
            self.save(namespace, None, False, True)
        return self.resume_token

    def advance(self, token: str) -> bool:
        if self.namespace is None:
            raise RuntimeError("Range pagination was not initialized.")
        saved = self.save(self.namespace, token, False, False)
        if saved:
            self.resume_namespace = self.namespace
            self.resume_token = token
        return saved

    def finish(self) -> bool:
        if self.namespace is None:
            raise RuntimeError("Range pagination was not initialized.")
        saved = self.save(self.namespace, None, True, False)
        if saved:
            self.resume_namespace = self.namespace
            self.resume_token = None
            self.complete = True
        return saved

    def reset(self) -> None:
        if self.namespace is None:
            raise RuntimeError("Range pagination was not initialized.")
        self.save(self.namespace, None, False, True)
        self.resume_namespace = self.namespace
        self.resume_token = None
        self.complete = False
