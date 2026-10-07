"""Profile composition translates retryable adapter errors at the application port."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.errors import ProfileUnavailableError, WorkspaceError
from mailarchive.bootstrap import LocalProfiles
from mailarchive.domain.configuration import Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.polling_repository import SqlitePollingSchedule
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore


class ProfilePortErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConfigStore(self.root / "selected")
        self.store.save(Settings(start_at_login=False))
        self.profiles = LocalProfiles(self.store, MemoryCredentialStore())
        self.native_connect = sqlite3.connect

    def short_connection(self, *args, **kwargs):
        kwargs["timeout"] = 0.01
        return self.native_connect(*args, **kwargs)

    def open(self, path=None):
        return self.profiles.open(path or self.store.path, lambda _: None, lambda _: None)

    def test_native_busy_recovery_is_typed_and_keeps_original_exception_details(self):
        state = ProfileDatabase(self.store.path)
        with state.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with patch(
                "mailarchive.infrastructure.sqlite_core.sqlite3.connect", self.short_connection
            ):
                with self.assertRaises(ProfileUnavailableError) as caught:
                    self.open()
            db.rollback()
        self.assertEqual(str(caught.exception), "database is locked")
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(state.configuration.load_settings().start_at_login, False)

    def test_wrapped_native_initialization_failure_is_retryable_without_losing_its_cause(self):
        destination = self.root / "legacy-polling" / "workspace.sqlite3"
        state = ProfileDatabase(destination)
        with state.connection() as db:
            # An older valid profile has no optional polling extension yet.
            db.execute("DROP TABLE polling_schedule")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            with patch(
                "mailarchive.infrastructure.sqlite_core.sqlite3.connect", self.short_connection
            ):
                with self.assertRaises(ProfileUnavailableError) as caught:
                    self.open(destination)
            db.rollback()
        self.assertIn("database is locked", str(caught.exception))
        wrapped = caught.exception.__cause__
        self.assertIsInstance(wrapped, WorkspaceError)
        self.assertIsInstance(wrapped.__cause__, sqlite3.OperationalError)
        context = self.open(destination)
        self.addCleanup(context.execution.shutdown)
        self.assertEqual(context.database_path, destination)

    def test_late_polling_schedule_read_is_inside_the_same_translation_boundary(self):
        state = ProfileDatabase(self.store.path)
        original_load = SqlitePollingSchedule.load

        def locked_read(schedule):
            with state.connection() as db:
                db.execute("BEGIN EXCLUSIVE")
                try:
                    return original_load(schedule)
                finally:
                    db.rollback()

        with (
            patch("mailarchive.infrastructure.sqlite_core.sqlite3.connect", self.short_connection),
            patch.object(SqlitePollingSchedule, "load", locked_read),
        ):
            with self.assertRaises(ProfileUnavailableError) as caught:
                self.open()
        self.assertEqual(str(caught.exception), "database is locked")
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
        context = self.open()
        self.addCleanup(context.execution.shutdown)
        self.assertEqual(context.settings.start_at_login, False)

    def test_profile_damage_remains_fatal_and_is_not_reclassified_as_temporary_access(self):
        destination = self.root / "damaged" / "workspace.sqlite3"
        destination.parent.mkdir()
        original = b"Not a SQLite database; preserve these bytes."
        destination.write_bytes(original)
        with self.assertRaises(WorkspaceError) as caught:
            self.open(destination)
        self.assertNotIsInstance(caught.exception, ProfileUnavailableError)
        self.assertIsInstance(caught.exception.__cause__, sqlite3.DatabaseError)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(self.store.path, self.profiles.path)
