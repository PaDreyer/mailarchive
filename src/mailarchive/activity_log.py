from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mailarchive.service import EventLevel, ServiceEvent


@dataclass(slots=True)
class ActivityPage:
    events: list[ServiceEvent]
    total: int
    offset: int


class ActivityLog:
    """Local event history, independent of the archive's duplicate-prevention index."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_event (
                    id INTEGER PRIMARY KEY,
                    created_at REAL NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    account_id TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_event_created_at "
                "ON activity_event(created_at DESC, id DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, event: ServiceEvent) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO activity_event (created_at, level, message, account_id) "
                "VALUES (?, ?, ?, ?)",
                (event.created_at.timestamp(), event.level.value, event.message, event.account_id),
            )

    def page(
        self, *, since: datetime | None = None, offset: int = 0, limit: int = 50
    ) -> ActivityPage:
        if limit < 1 or offset < 0:
            raise ValueError("The page size must be positive and the offset cannot be negative.")
        where = " WHERE created_at >= ?" if since is not None else ""
        parameters = (since.timestamp(),) if since is not None else ()
        with closing(self._connect()) as connection, connection:
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
        return ActivityPage(
            events=[
                ServiceEvent(
                    level=EventLevel(row["level"]),
                    message=row["message"],
                    account_id=row["account_id"],
                    created_at=datetime.fromtimestamp(row["created_at"]).astimezone(),
                )
                for row in rows
            ],
            total=total,
            offset=offset,
        )

    def clear(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM activity_event")
