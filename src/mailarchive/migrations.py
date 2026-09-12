"""Ordered SQLite upgrades, independent of the application and JSON settings versions."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


class DatabaseMigrationError(RuntimeError):
    """Startup must stop rather than use an incompatible or partially upgraded index."""


def _processed_messages(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_message (
            account_id TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            message_id TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            subject TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            destination TEXT NOT NULL,
            files_json TEXT NOT NULL,
            PRIMARY KEY (account_id, source_namespace, message_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_processed_message_archived_at "
        "ON processed_message(archived_at DESC)"
    )
    legacy_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'processed_mail'"
    ).fetchone()
    if legacy_table:
        connection.execute(
            """
            INSERT OR IGNORE INTO processed_message (
                account_id, source_namespace, message_id, archived_at,
                subject, rule_name, destination, files_json
            )
            SELECT account_id, 'imap:' || uid_validity, uid, archived_at,
                   subject, rule_name, destination, files_json
            FROM processed_mail
            """
        )


def _initial_checkpoints(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS skipped_message (
            account_id TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            message_id TEXT NOT NULL,
            PRIMARY KEY (account_id, source_namespace, message_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_checkpoint (
            account_id TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            initialized_at TEXT NOT NULL,
            PRIMARY KEY (account_id, source_namespace)
        )
        """
    )


def _unmatched_messages(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS unmatched_message (
            account_id TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            message_id TEXT NOT NULL,
            rules_fingerprint TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (account_id, source_namespace, message_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_unmatched_message_rules "
        "ON unmatched_message(account_id, source_namespace, rules_fingerprint)"
    )


def _synchronization_checkpoints(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS synchronization_checkpoint (
            account_id TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            identity TEXT NOT NULL,
            cursor TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (account_id, source_namespace)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS unavailable_message (
            account_id TEXT NOT NULL,
            source_namespace TEXT NOT NULL,
            message_id TEXT NOT NULL,
            PRIMARY KEY (account_id, source_namespace, message_id)
        )
        """
    )


# Append new migrations; never edit or reorder steps shipped in a release.
def _mailbox_history_upgrades(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_processed_message_identity "
        "ON processed_message(source_namespace, message_id)"
    )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS mailbox_history_upgrade (
            account_id TEXT NOT NULL,
            legacy_namespace TEXT NOT NULL,
            target_namespace TEXT NOT NULL,
            PRIMARY KEY (account_id, legacy_namespace, target_namespace)
        )
    """)


def _mailbox_checks(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS mailbox_check (
            account_id TEXT NOT NULL,
            mailbox_namespace TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            last_successful_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
            error TEXT,
            PRIMARY KEY (account_id, mailbox_namespace)
        )
    """)


MIGRATIONS = (
    _processed_messages,
    _initial_checkpoints,
    _unmatched_messages,
    _synchronization_checkpoints,
    _mailbox_history_upgrades,
    _mailbox_checks,
)
DATABASE_SCHEMA_VERSION = len(MIGRATIONS)


def _backup(database_path: Path, version: int) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f"{database_path.name}.pre-v{version}-", suffix=".bak", dir=database_path.parent
    )
    os.close(descriptor)
    backup_path = Path(name)
    try:
        # Use a separate reader: backing up the connection holding the write transaction
        # would wait for its own transaction. BEGIN IMMEDIATE keeps other writers out.
        with (
            closing(sqlite3.connect(database_path, timeout=15)) as source,
            closing(sqlite3.connect(backup_path)) as destination,
        ):
            source.backup(destination)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def migrate_database(connection: sqlite3.Connection, database_path: Path) -> Path | None:
    """Back up existing state, then commit all pending steps and versions together."""
    backup_path = None
    try:
        with connection:
            # Explicit BEGIN also makes DDL transactional with Python's sqlite3 defaults.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 0:
                raise DatabaseMigrationError(
                    f"Database '{database_path}' has invalid schema version {version}. "
                    "The database has not been changed."
                )
            if version > DATABASE_SCHEMA_VERSION:
                raise DatabaseMigrationError(
                    f"Database '{database_path}' uses schema {version}, but this MailArchive "
                    f"supports only schema {DATABASE_SCHEMA_VERSION}. Install a newer version "
                    "of MailArchive. The database has not been changed."
                )
            if version == DATABASE_SCHEMA_VERSION:
                return None
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone():
                backup_path = _backup(database_path, version)
            for next_version, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                migration(connection)
                connection.execute(f"PRAGMA user_version = {next_version}")
    except (sqlite3.Error, OSError) as exc:
        recovery = f" Backup: '{backup_path}'." if backup_path else ""
        raise DatabaseMigrationError(
            f"Could not upgrade database '{database_path}': {exc}. "
            f"The upgrade was rolled back.{recovery}"
        ) from exc
    return backup_path
