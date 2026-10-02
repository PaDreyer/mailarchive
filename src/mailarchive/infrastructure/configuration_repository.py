"""Configuration revisions and source bindings in the profile SQLite database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import uuid4

from mailarchive.domain.configuration import Account, Mailbox, MailProvider, Settings
from mailarchive.domain.source_identity import source_key
from mailarchive.infrastructure import profile_integrity as integrity


class ConfigurationRepository:
    """Own immutable settings revisions and current source identity in one transaction."""

    def __init__(
        self,
        connection: Callable[[], AbstractContextManager[sqlite3.Connection]],
        *,
        error_type: type[RuntimeError],
        now: Callable[[], str],
    ) -> None:
        self.connection = connection
        self.error_type = error_type
        self.now = now

    def load_settings(self) -> Settings:
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT id, payload FROM config_revision WHERE active=1 LIMIT 1"
            ).fetchone()
            revision_count = int(db.execute("SELECT count(*) FROM config_revision").fetchone()[0])
            integrity.validate_active_sources(db)
        if row is None:
            if revision_count:
                raise self.error_type("MailArchive profile settings are damaged.")
            return Settings.defaults()
        settings = integrity.settings_from_payload(
            row["payload"], "MailArchive profile settings are damaged."
        )
        settings.config_revision = int(row["id"])
        return settings

    def ensure_configuration_revision(self) -> int:
        """Persist defaults before a component writes otherwise standalone runtime data."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            integrity.validate_config_revision_state(db)
            integrity.validate_active_sources(db)
            row = db.execute("SELECT id FROM config_revision WHERE active=1 LIMIT 1").fetchone()
            if row is not None:
                return int(row["id"])
            payload = json.dumps(Settings.defaults().to_dict(), ensure_ascii=False, sort_keys=True)
            db.execute(
                "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 1)",
                (payload, self.now()),
            )
            return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    def save_settings(self, settings: Settings) -> int:
        settings.validate()
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            integrity.validate_config_revision_state(db)
            integrity.validate_active_sources(db)
            previous = db.execute(
                "SELECT id, payload FROM config_revision WHERE active=1 LIMIT 1"
            ).fetchone()
            self.normalize_source_ids(db, settings)
            payload = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
            if previous and previous["payload"] == payload:
                revision = int(previous["id"])
                settings.config_revision = revision
                return revision
            db.execute("UPDATE config_revision SET active=0 WHERE active=1")
            db.execute(
                "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 1)",
                (payload, self.now()),
            )
            revision = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            self.replace_current_sources(db, settings)
            settings.config_revision = revision
            return revision

    def normalize_source_ids(self, db: sqlite3.Connection, settings: Settings) -> None:
        """Bind each configured mailbox to its durable provider identity."""
        bindings: set[tuple[str, str]] = set()
        source_ids: dict[str, tuple[str, str]] = {}
        for account in settings.accounts:
            for mailbox in account.mailboxes:
                binding = (account.provider.value, source_key(account, mailbox))
                if binding in bindings:
                    raise self.error_type(
                        f"Mailbox {mailbox.address} is configured twice for this provider."
                    )
                bindings.add(binding)
                if mailbox.id in source_ids:
                    raise self.error_type(
                        "Each configured mailbox needs a distinct local source identity."
                    )
                source_ids[mailbox.id] = binding
                existing = db.execute(
                    "SELECT provider, mailbox_key FROM source WHERE id=?", (mailbox.id,)
                ).fetchone()
                if existing and tuple(existing) != binding:
                    mailbox.id = str(uuid4())
                duplicate = db.execute(
                    "SELECT id FROM source WHERE provider=? AND mailbox_key=? AND id!=?",
                    (*binding, mailbox.id),
                ).fetchone()
                if duplicate:
                    mailbox.id = str(duplicate["id"])

    @staticmethod
    def source_values(
        account: Account, mailbox: Mailbox, enabled: int, discovery_pending: int = 0
    ) -> tuple:
        return (
            mailbox.id,
            account.provider.value,
            source_key(account, mailbox),
            account.id,
            mailbox.address,
            json.dumps(mailbox.folders),
            enabled,
            discovery_pending,
        )

    def replace_current_sources(self, db: sqlite3.Connection, settings: Settings) -> None:
        previous_sources = {
            row["id"]: row
            for row in db.execute(
                "SELECT id, provider, folders_json, enabled, discovery_pending FROM source"
            )
        }
        db.execute("UPDATE source SET enabled=0")
        for account in settings.accounts:
            for mailbox in account.mailboxes:
                old = previous_sources.get(mailbox.id)
                enabled = int(account.enabled and mailbox.enabled)
                old_folders = set(json.loads(old["folders_json"])) if old else set()
                if old and enabled and not old["enabled"]:
                    db.execute("DELETE FROM source_scope WHERE source_id=?", (mailbox.id,))
                elif old:
                    self.remove_deselected_scopes(
                        db,
                        mailbox.id,
                        account.provider,
                        old_folders,
                        set(mailbox.folders),
                    )
                discovery_pending = int(
                    account.provider != MailProvider.GMAIL_API
                    and not mailbox.folders
                    and (
                        old is None
                        or bool(old_folders)
                        or bool(old["discovery_pending"])
                        or bool(enabled and not old["enabled"])
                    )
                )
                db.execute(
                    """INSERT INTO source(id, provider, mailbox_key, account_id, address,
                    folders_json, enabled, discovery_pending)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,
                      address=excluded.address, folders_json=excluded.folders_json,
                      enabled=excluded.enabled,
                      discovery_pending=excluded.discovery_pending""",
                    self.source_values(account, mailbox, enabled, discovery_pending),
                )

    @staticmethod
    def remove_deselected_scopes(
        db: sqlite3.Connection,
        source_id: str,
        provider: MailProvider,
        old_folders: set[str],
        new_folders: set[str],
    ) -> None:
        # An empty selection means every accessible folder or label. Expanding an
        # explicit selection to all must therefore retain every existing cursor.
        if not new_folders:
            return
        if provider == MailProvider.GMAIL_API:
            if not old_folders:
                mailbox_scope = db.execute(
                    "SELECT processing_namespace, synchronization_namespace, baseline_done, "
                    "cursor, status, error FROM source_scope "
                    "WHERE source_id=? AND scope_key='gmail-mailbox'",
                    (source_id,),
                ).fetchone()
                if mailbox_scope and mailbox_scope["baseline_done"]:
                    for label in new_folders:
                        db.execute(
                            """INSERT OR IGNORE INTO source_scope
                            (source_id, scope_key, processing_namespace,
                             synchronization_namespace, baseline_done, cursor, status, error)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                source_id,
                                "gmail-label:" + label,
                                mailbox_scope["processing_namespace"],
                                mailbox_scope["synchronization_namespace"],
                                mailbox_scope["baseline_done"],
                                mailbox_scope["cursor"],
                                mailbox_scope["status"],
                                mailbox_scope["error"],
                            ),
                        )
                selected_scope_keys = ["gmail-label:" + label for label in new_folders]
                placeholders = ",".join("?" for _ in selected_scope_keys)
                db.execute(
                    "DELETE FROM source_scope WHERE source_id=? "
                    "AND scope_key LIKE 'gmail-label:%' "
                    f"AND scope_key NOT IN ({placeholders})",
                    (source_id, *selected_scope_keys),
                )
                return
            removed = {"gmail-label:" + label for label in old_folders - new_folders}
        elif not old_folders:
            placeholders = ",".join("?" for _ in new_folders)
            db.execute(
                f"DELETE FROM source_scope WHERE source_id=? AND scope_key NOT IN ({placeholders})",
                (source_id, *sorted(new_folders)),
            )
            return
        else:
            removed = old_folders - new_folders
        for scope_key in removed:
            db.execute(
                "DELETE FROM source_scope WHERE source_id=? AND scope_key=?",
                (source_id, scope_key),
            )

    def prepare_run_settings(self, settings: Settings) -> int:
        """Persist an exact immutable run snapshot without changing current settings."""
        settings.validate()
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            integrity.validate_config_revision_state(db)
            integrity.validate_active_sources(db)
            claimed_revision = settings.config_revision
            self.normalize_source_ids(db, settings)
            payload = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
            active = db.execute(
                "SELECT id, payload FROM config_revision WHERE active=1 LIMIT 1"
            ).fetchone()
            if active is not None and active["payload"] == payload:
                revision_id = int(active["id"])
            elif active is None or claimed_revision == int(active["id"]):
                db.execute("UPDATE config_revision SET active=0 WHERE active=1")
                db.execute(
                    "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 1)",
                    (payload, self.now()),
                )
                revision_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                self.replace_current_sources(db, settings)
            else:
                revision = db.execute(
                    "SELECT id FROM config_revision WHERE payload=? ORDER BY id DESC LIMIT 1",
                    (payload,),
                ).fetchone()
                if revision is None:
                    db.execute(
                        "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 0)",
                        (payload, self.now()),
                    )
                    revision_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                else:
                    revision_id = int(revision["id"])
                # Preserve removed sources for run history without changing which
                # sources belong to the user's current configuration.
                for account in settings.accounts:
                    for mailbox in account.mailboxes:
                        db.execute(
                            """INSERT OR IGNORE INTO source
                            (id, provider, mailbox_key, account_id, address, folders_json,
                             enabled, discovery_pending)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            self.source_values(account, mailbox, 0),
                        )
            settings.config_revision = revision_id
            return revision_id

    def configuration_revision(self) -> int:
        with self.connection() as db:
            integrity.validate_config_revision_state(db)
            row = db.execute("SELECT id FROM config_revision WHERE active=1 LIMIT 1").fetchone()
        return int(row[0]) if row else 0
