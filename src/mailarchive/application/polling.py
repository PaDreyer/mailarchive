"""Account deadlines, persistent checkpoints, and automatic-monitoring state."""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol

STARTUP_DELAY_SECONDS = 30


class AutomaticMonitoringState(str, Enum):
    ACTIVE = "active"
    PAUSING = "pausing"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class PollingCheckpoint:
    first_due_at: datetime
    last_checked_at: datetime | None = None


class PollingSchedulePort(Protocol):
    def load(self) -> dict[str, PollingCheckpoint]: ...

    def save(self, account_id: str, checkpoint: PollingCheckpoint) -> None: ...


@dataclass(frozen=True, slots=True)
class _AccountTiming:
    checkpoint: PollingCheckpoint | None
    first_due: float
    last_checked: float | None = None


class PollingSchedule:
    """Own account timing; the coordinator serializes access to this object.

    UTC checkpoints retain time across restarts. Their monotonic projection keeps
    deadlines stable during a session, including pauses and wall-clock changes.
    A manual stop defers the next attempt without persisting a completed check.
    """

    def __init__(
        self,
        storage: PollingSchedulePort | None = None,
        *,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        now = self._utc_now()
        current = time.monotonic()
        self._startup_due_at = now + timedelta(seconds=STARTUP_DELAY_SECONDS)
        checkpoints = storage.load() if storage is not None else {}
        self._accounts = {
            account_id: _AccountTiming(
                checkpoint,
                current + (checkpoint.first_due_at - now).total_seconds(),
                current + (checkpoint.last_checked_at - now).total_seconds()
                if checkpoint.last_checked_at is not None
                else None,
            )
            for account_id, checkpoint in checkpoints.items()
        }

    def _initial_timing(self) -> _AccountTiming:
        return _AccountTiming(
            None,
            time.monotonic() + (self._startup_due_at - self._utc_now()).total_seconds(),
        )

    def initialize(self, account_ids: Iterable[str]) -> None:
        if self._storage is None:
            return
        for account_id in account_ids:
            timing = self._accounts.get(account_id) or self._initial_timing()
            if timing.checkpoint is None:
                checkpoint = PollingCheckpoint(self._startup_due_at)
                self._storage.save(account_id, checkpoint)
                self._accounts[account_id] = replace(timing, checkpoint=checkpoint)

    def is_due(self, account_id: str, interval_seconds: float) -> bool:
        timing = self._accounts.get(account_id)
        if timing is None:
            return True
        current = time.monotonic()
        if timing.last_checked is None:
            return current >= timing.first_due
        return current - timing.last_checked >= interval_seconds

    def record_completed(self, account_id: str) -> None:
        timing = self._accounts.get(account_id) or self._initial_timing()
        checkpoint = replace(
            timing.checkpoint or PollingCheckpoint(self._startup_due_at),
            last_checked_at=self._utc_now(),
        )
        if self._storage is not None:
            self._storage.save(account_id, checkpoint)
        self._accounts[account_id] = replace(
            timing, checkpoint=checkpoint, last_checked=time.monotonic()
        )

    def defer_after_stop(self, account_id: str) -> None:
        timing = self._accounts.get(account_id) or self._initial_timing()
        self._accounts[account_id] = replace(timing, last_checked=time.monotonic())
