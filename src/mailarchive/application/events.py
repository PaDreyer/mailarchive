"""Notifications emitted by application use cases, independent of their observers."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class EventLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ServiceEvent:
    level: EventLevel
    message: str
    account_id: str | None = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True, slots=True)
class RunProgress:
    message: str
    active: bool = True
