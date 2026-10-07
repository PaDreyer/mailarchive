"""Compose the repositories of one local profile and recover interrupted work."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mailarchive.application.errors import WorkspaceError
from mailarchive.infrastructure.configuration_repository import ConfigurationRepository
from mailarchive.infrastructure.delivery_repository import DeliveryRepository
from mailarchive.infrastructure.discovery_repository import DiscoveryRepository
from mailarchive.infrastructure.operation_repository import OperationRepository
from mailarchive.infrastructure.persistence_time import now
from mailarchive.infrastructure.polling_repository import SqlitePollingSchedule
from mailarchive.infrastructure.profile_ownership import exclusive_profile_directory
from mailarchive.infrastructure.spool import LocalSpool, SpoolError
from mailarchive.infrastructure.sqlite_core import SqliteDatabase

# The application has a process-level single-instance guard. These locks also
# serialize publication with lifecycle transitions across profile handles.
_PLAN_LOCKS = tuple(threading.RLock() for _ in range(128))


class ProfileDatabase:
    def __init__(self, database_path: Path, *, recover: bool = False) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_profile_directory(self.database_path):
            try:
                self.spool = LocalSpool(self.database_path.parent / "work")
            except SpoolError as exc:
                raise WorkspaceError(str(exc)) from exc
            self.sql = SqliteDatabase(self.database_path)
        self.configuration = ConfigurationRepository(
            self.connection, error_type=WorkspaceError, now=now
        )
        self.polling = SqlitePollingSchedule(self.connection)
        self.discovery = DiscoveryRepository(self.connection)
        self.operations = OperationRepository(self.connection, self.configuration)
        self.delivery = DeliveryRepository(
            self.connection, self.spool, self.plan_execution, self.operations
        )
        if recover:
            self.recover()

    def connection(self):
        return self.sql.connection()

    @contextmanager
    def plan_execution(self, plan_id: str) -> Iterator[None]:
        key = (str(self.database_path.resolve()), plan_id)
        with _PLAN_LOCKS[hash(key) % len(_PLAN_LOCKS)]:
            yield

    def recover(self) -> None:
        with exclusive_profile_directory(self.database_path):
            self._recover_owned_work()

    def _recover_owned_work(self) -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.operations.recover(db)
            retained = {
                row[0]
                for row in db.execute(
                    "SELECT raw_path FROM plan WHERE status IN ('open', 'paused') "
                    "AND raw_path IS NOT NULL"
                )
            }
            protected_names = frozenset(
                Path(row[0]).name
                for row in db.execute(
                    "SELECT final_path FROM output WHERE final_path!='' "
                    "UNION SELECT final_path FROM receipt"
                )
            )
        try:
            self.spool.cleanup_unreferenced(retained, protected_names=protected_names)
        except SpoolError as exc:
            raise WorkspaceError(str(exc)) from exc
