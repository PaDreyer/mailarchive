"""One SQLite connection policy and profile-format validation boundary."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mailarchive.application.errors import WorkspaceError
from mailarchive.infrastructure.polling_repository import SqlitePollingSchedule
from mailarchive.infrastructure.profile_integrity import validate_runtime_references
from mailarchive.infrastructure.sqlite_schema import (
    _SCHEMA,
    APPLICATION_ID,
    DATABASE_SCHEMA_VERSION,
    validate_schema,
)


class SqliteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        existed = self.path.exists()
        with self.connection() as db:
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                marker = db.execute("PRAGMA application_id").fetchone()[0]
                if existed and (version != DATABASE_SCHEMA_VERSION or marker != APPLICATION_ID):
                    raise WorkspaceError("Unknown or damaged MailArchive profile database.")
                if not existed:
                    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    db.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")
                    db.executescript(_SCHEMA)
                elif db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise WorkspaceError("MailArchive profile database failed its integrity check.")
                validate_schema(db, error_type=WorkspaceError)
                validate_runtime_references(db)
                SqlitePollingSchedule.initialize(db)
                db.commit()
            except sqlite3.DatabaseError as exc:
                raise WorkspaceError(f"Could not open MailArchive profile database: {exc}") from exc
        if os.name != "nt":
            self.path.chmod(0o600)
