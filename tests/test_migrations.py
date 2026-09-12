import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from mailarchive import migrations
from mailarchive.migrations import DATABASE_SCHEMA_VERSION, DatabaseMigrationError
from mailarchive.storage import ArchiveState


class MigrationTests(unittest.TestCase):
    def test_new_database_runs_every_step_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            state = ArchiveState(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(tables, {"processed_message", "skipped_message", "source_checkpoint"})
            self.assertIsNone(state.migration_backup_path)

    def test_unversioned_current_database_preserves_history_and_only_migrates_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            state = ArchiveState(path)
            state.complete_initial_scan("account", "gmail:inbox", {"old-message"})
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version = 0")

            upgraded = ArchiveState(path)
            self.assertTrue(upgraded.has_completed_initial_scan("account", "gmail:inbox"))
            self.assertEqual(
                upgraded.processed_message_ids("account", "gmail:inbox", include_skipped=True),
                {"old-message"},
            )
            with closing(sqlite3.connect(upgraded.migration_backup_path)) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(
                    backup.execute("SELECT message_id FROM skipped_message").fetchone()[0],
                    "old-message",
                )
            self.assertIsNone(ArchiveState(path).migration_backup_path)
            self.assertEqual(len(list(Path(temporary).glob("*.bak"))), 1)

    def test_version_one_adds_checkpoints_and_backup_contains_wal_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                with connection:
                    migrations.MIGRATIONS[0](connection)
                    connection.execute("PRAGMA user_version = 1")
                    connection.execute(
                        "INSERT INTO processed_message VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "account",
                            "imap:123",
                            "7",
                            "2026-01-01",
                            "Subject",
                            "Rule",
                            "Inbox",
                            "[]",
                        ),
                    )
                state = ArchiveState(path)
                self.assertTrue(state.was_processed("account", "imap:123", "7"))
                state.complete_initial_scan("account", "imap:123", {"8"})
                with closing(sqlite3.connect(state.migration_backup_path)) as backup:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertEqual(
                        backup.execute("SELECT message_id FROM processed_message").fetchone()[0],
                        "7",
                    )
                    self.assertIsNone(
                        backup.execute(
                            "SELECT 1 FROM sqlite_master WHERE name = 'source_checkpoint'"
                        ).fetchone()
                    )

    def test_future_schema_is_rejected_without_changes_or_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version = 999")
            original = path.read_bytes()
            with self.assertRaisesRegex(DatabaseMigrationError, "Install a newer version"):
                ArchiveState(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temporary).glob("*.bak")), [])

    def test_failed_migration_rolls_back_ddl_data_and_version_and_keeps_backup(self) -> None:
        def succeed(connection):
            connection.execute("CREATE TABLE completed_step (value TEXT)")

        def fail(connection):
            connection.execute("CREATE TABLE partial_change (value TEXT)")
            connection.execute("DELETE FROM skipped_message")
            connection.execute("SELECT missing_column FROM skipped_message")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            ArchiveState(path).complete_initial_scan("account", "namespace", {"message"})
            with (
                mock.patch.object(
                    migrations, "MIGRATIONS", (*migrations.MIGRATIONS, succeed, fail)
                ),
                mock.patch.object(
                    migrations, "DATABASE_SCHEMA_VERSION", DATABASE_SCHEMA_VERSION + 2
                ),
                self.assertRaisesRegex(DatabaseMigrationError, "rolled back.*Backup:"),
            ):
                ArchiveState(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION
                )
                self.assertEqual(
                    connection.execute("SELECT message_id FROM skipped_message").fetchone()[0],
                    "message",
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name IN ('completed_step', 'partial_change')"
                    ).fetchone()
                )
            self.assertEqual(len(list(Path(temporary).glob("*.bak"))), 1)

    def test_negative_schema_is_rejected_without_modifying_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version = -1")
            original = path.read_bytes()
            with self.assertRaisesRegex(DatabaseMigrationError, "invalid schema version"):
                ArchiveState(path)
            self.assertEqual(path.read_bytes(), original)

    def test_backup_failure_prevents_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                migrations.MIGRATIONS[0](connection)
                connection.execute("PRAGMA user_version = 1")
            with (
                mock.patch("mailarchive.migrations._backup", side_effect=OSError("disk full")),
                self.assertRaisesRegex(DatabaseMigrationError, "disk full"),
            ):
                ArchiveState(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'source_checkpoint'"
                    ).fetchone()
                )

    def test_incomplete_backup_file_is_removed_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            with (
                mock.patch(
                    "mailarchive.migrations.sqlite3.connect", side_effect=OSError("unavailable")
                ),
                self.assertRaises(OSError),
            ):
                migrations._backup(path, 0)
            self.assertEqual(list(Path(temporary).glob("*.bak")), [])
