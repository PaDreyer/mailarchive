from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from mailarchive.application.events import EventLevel, ServiceEvent
from mailarchive.application.profile import LogPage


class ActivityLog:
    """Local event history in the common profile database."""

    def __init__(
        self,
        connection: Callable[[], AbstractContextManager[sqlite3.Connection]],
        ensure_configuration: Callable[[], int],
    ) -> None:
        self.connection = connection
        self.ensure_configuration = ensure_configuration

    def record(self, event: ServiceEvent) -> None:
        self.ensure_configuration()
        with self.connection() as connection, connection:
            connection.execute(
                "INSERT INTO activity_event (created_at, level, message, account_id) "
                "VALUES (?, ?, ?, ?)",
                (event.created_at.timestamp(), event.level.value, event.message, event.account_id),
            )

    def page(self, *, since: datetime | None = None, offset: int = 0, limit: int = 50) -> LogPage:
        if limit < 1 or offset < 0:
            raise ValueError("The page size must be positive and the offset cannot be negative.")
        where = " WHERE created_at >= ?" if since is not None else ""
        parameters = (since.timestamp(),) if since is not None else ()
        with self.connection() as connection, connection:
            # Keep count and page consistent while background threads append events.
            connection.execute("BEGIN")
            total = connection.execute(
                "SELECT COUNT(*) FROM activity_event" + where, parameters
            ).fetchone()[0]
            offset = min(offset, ((total - 1) // limit) * limit) if total else 0
            rows = connection.execute(
                "SELECT created_at, level, message, account_id FROM activity_event"
                + where
                + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return LogPage(
            events=tuple(
                ServiceEvent(
                    level=EventLevel(row["level"]),
                    message=row["message"],
                    account_id=row["account_id"],
                    created_at=datetime.fromtimestamp(row["created_at"]).astimezone(),
                )
                for row in rows
            ),
            total=total,
            offset=offset,
        )

    def clear(self) -> None:
        with self.connection() as connection, connection:
            connection.execute("DELETE FROM activity_event")
