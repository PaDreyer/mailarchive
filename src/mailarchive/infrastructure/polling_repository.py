"""UTC polling checkpoints, separate from immutable configuration revisions."""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from mailarchive.application.errors import WorkspaceError
from mailarchive.application.polling import PollingCheckpoint

_SCHEMA = """CREATE TABLE IF NOT EXISTS polling_schedule(
    account_id TEXT PRIMARY KEY NOT NULL,
    first_due_at TEXT NOT NULL,
    last_checked_at TEXT
)"""


class SqlitePollingSchedule:
    def __init__(
        self, connection: Callable[[], AbstractContextManager[sqlite3.Connection]]
    ) -> None:
        self.connection = connection

    @classmethod
    def initialize(cls, db: sqlite3.Connection) -> None:
        """Extend only an already validated profile; tolerate pre-feature profiles."""
        db.execute(_SCHEMA)
        columns = db.execute("PRAGMA table_info(polling_schedule)").fetchall()
        if {row[1] for row in columns} != {"account_id", "first_due_at", "last_checked_at"} or (
            tuple(row[1] for row in columns if row[5]) != ("account_id",)
        ):
            raise WorkspaceError("MailArchive polling schedule is damaged.")
        cls._load(db)

    @staticmethod
    def _timestamp(value: str) -> datetime:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("Polling timestamps need a timezone.")
        return stamp.astimezone(timezone.utc)

    @classmethod
    def _load(cls, db: sqlite3.Connection) -> dict[str, PollingCheckpoint]:
        try:
            return {
                row["account_id"]: PollingCheckpoint(
                    cls._timestamp(row["first_due_at"]),
                    cls._timestamp(row["last_checked_at"]) if row["last_checked_at"] else None,
                )
                for row in db.execute("SELECT * FROM polling_schedule")
            }
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("MailArchive polling schedule is damaged.") from exc

    def load(self) -> dict[str, PollingCheckpoint]:
        with self.connection() as db:
            return self._load(db)

    def save(self, account_id: str, checkpoint: PollingCheckpoint) -> None:
        first_due = self._timestamp(checkpoint.first_due_at.isoformat()).isoformat()
        last_checked = (
            self._timestamp(checkpoint.last_checked_at.isoformat()).isoformat()
            if checkpoint.last_checked_at is not None
            else None
        )
        with self.connection() as db, db:
            db.execute(
                "INSERT INTO polling_schedule(account_id, first_due_at, last_checked_at) "
                "VALUES (?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
                "first_due_at=excluded.first_due_at, last_checked_at=excluded.last_checked_at",
                (account_id, first_due, last_checked),
            )
