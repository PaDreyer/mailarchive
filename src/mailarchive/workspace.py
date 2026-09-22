"""The 0.0.1 profile database and durable processing protocol.

Configuration revisions and runtime state share one SQLite transaction domain. No
prototype database is opened or migrated by this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from mailarchive.intake_limits import MAX_ACTIVE_INTAKES, IntakeQueueCapacityError
from mailarchive.mail_identity import mailbox_namespace
from mailarchive.models import Account, Mailbox, MailProvider, Rule, Settings

APPLICATION_ID = 0x4D415243  # MARC
DATABASE_SCHEMA_VERSION = 1

# All WorkspaceStore instances in this process share these locks.  The desktop
# application also has a process-level single-instance guard, so together they
# serialize plan lifecycle changes with filesystem publication.
_PLAN_EXECUTION_LOCKS = tuple(threading.RLock() for _ in range(128))
_WORK_COPY_CLEANUP_LOCK = threading.Lock()
_PENDING_WORK_COPY_CLEANUP: dict[str, set[Path]] = {}

_REQUIRED_SCHEMA_COLUMNS = {
    "config_revision": {"id", "payload", "created_at", "active"},
    "source": {
        "id",
        "provider",
        "mailbox_key",
        "account_id",
        "address",
        "folders_json",
        "enabled",
        "discovery_pending",
    },
    "source_scope": {
        "source_id",
        "scope_key",
        "processing_namespace",
        "synchronization_namespace",
        "baseline_done",
        "cursor",
        "status",
        "error",
    },
    "source_message": {
        "source_id",
        "message_key",
        "first_seen_at",
        "received_at",
        "received_origin",
        "sender_at",
        "subject",
        "terminal_state",
    },
    "scan_run": {
        "id",
        "source_id",
        "kind",
        "selection_json",
        "config_revision",
        "settings_json",
        "status",
        "started_at",
        "finished_at",
        "error",
        "checkpoint",
    },
    "intake": {
        "id",
        "source_id",
        "message_key",
        "run_id",
        "scope_key",
        "remote_id",
        "status",
        "created_at",
        "error",
        "attempts",
        "retry_after",
    },
    "plan": {
        "id",
        "source_id",
        "message_key",
        "run_id",
        "raw_path",
        "raw_hash",
        "rule_json",
        "received_at",
        "status",
        "error",
        "created_at",
        "finished_at",
    },
    "plan_target": {
        "plan_id",
        "target_id",
        "path",
        "save_mode",
        "attachments_in_destination",
        "status",
        "error",
    },
    "active_message": {"source_id", "message_key", "kind", "ref_id"},
    "output": {
        "id",
        "plan_id",
        "artifact_key",
        "digest",
        "requested_path",
        "final_path",
        "status",
        "error",
        "attempts",
        "retry_after",
    },
    "output_target": {"output_id", "plan_id", "target_id"},
    "receipt": {
        "source_id",
        "message_key",
        "artifact_key",
        "digest",
        "requested_path",
        "final_path",
        "completed_at",
    },
    "activity_event": {"id", "created_at", "level", "message", "account_id"},
}

_REQUIRED_PRIMARY_KEYS = {
    "config_revision": ("id",),
    "source": ("id",),
    "source_scope": ("source_id", "scope_key"),
    "source_message": ("source_id", "message_key"),
    "scan_run": ("id",),
    "intake": ("id",),
    "plan": ("id",),
    "plan_target": ("plan_id", "target_id"),
    "active_message": ("source_id", "message_key"),
    "output": ("id",),
    "output_target": ("output_id", "target_id"),
    "receipt": ("source_id", "message_key", "artifact_key", "digest", "requested_path"),
    "activity_event": ("id",),
}

_REQUIRED_INDEXES = {
    "idx_plan_status": ("plan", False, False, ("status",)),
    "idx_intake_run": ("intake", False, False, ("run_id", "status")),
    "idx_active_config_revision": ("config_revision", True, True, ("active",)),
    "idx_activity_event_created_at": (
        "activity_event",
        False,
        False,
        ("created_at", "id"),
    ),
}

_REQUIRED_INDEX_PREDICATES = {
    "idx_active_config_revision": "active=1",
}

_REQUIRED_UNIQUE_KEYS = {
    "source": {("provider", "mailbox_key")},
    "scan_run": {("id", "source_id")},
    "output": {
        ("plan_id", "artifact_key", "digest", "requested_path"),
        ("id", "plan_id"),
    },
}

_REQUIRED_FOREIGN_KEYS = {
    "source_scope": {(("source_id", "source", "id"),)},
    "source_message": {(("source_id", "source", "id"),)},
    "scan_run": {
        (("source_id", "source", "id"),),
        (("config_revision", "config_revision", "id"),),
    },
    "intake": {
        (("run_id", "scan_run", "id"), ("source_id", "scan_run", "source_id")),
        (
            ("source_id", "source_message", "source_id"),
            ("message_key", "source_message", "message_key"),
        ),
    },
    "plan": {
        (("run_id", "scan_run", "id"), ("source_id", "scan_run", "source_id")),
        (
            ("source_id", "source_message", "source_id"),
            ("message_key", "source_message", "message_key"),
        ),
    },
    "plan_target": {(("plan_id", "plan", "id"),)},
    "output": {(("plan_id", "plan", "id"),)},
    "output_target": {
        (("output_id", "output", "id"),),
        (("output_id", "output", "id"), ("plan_id", "output", "plan_id")),
        (("plan_id", "plan_target", "plan_id"), ("target_id", "plan_target", "target_id")),
    },
}

_REQUIRED_CHECKS = {
    "config_revision": {"check(activein(0,1))"},
    "source": {"check(enabledin(0,1))", "check(discovery_pendingin(0,1))"},
    "source_scope": {
        "check(baseline_donein(0,1))",
        "check(statusin('new','active','paused'))",
    },
    "source_message": {
        "check(terminal_stateisnullorterminal_statein('baseline','unmatched','complete','aborted','rejected'))"
    },
    "scan_run": {
        "check(kindin('automatic','manual'))",
        "check(statusin('running','completed','failed','interrupted','cancelled'))",
    },
    "intake": {
        "check(statusin('reserved','error','accepted','unmatched','filtered','cancelled','rejected'))",
        "check(attempts>=0)",
    },
    "plan": {"check(statusin('open','paused','complete','aborted'))"},
    "plan_target": {
        "check(save_modein('email_only','email_and_attachments','attachments_only'))",
        "check(attachments_in_destinationin(0,1))",
        "check(statusin('pending','error','done','no_output'))",
    },
    "active_message": {"check(kindin('intake','plan'))"},
    "output": {"check(statusin('pending','error','done'))", "check(attempts>=0)"},
    "activity_event": {"check(levelin('info','success','warning','error'))"},
}

_REQUIRED_TRIGGERS = {
    "validate_active_message_insert",
    "validate_active_message_update",
    "protect_active_intake_delete",
    "protect_active_plan_delete",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_retry_after(attempts: int) -> str:
    delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def _intake_retry_due(status: str, retry_after: str | None, current: datetime) -> bool:
    if status == "reserved" or not retry_after:
        return True
    try:
        return datetime.fromisoformat(retry_after) <= current
    except (TypeError, ValueError):
        return True


def source_key(account: Account, mailbox: Mailbox) -> str:
    address = mailbox.address.casefold()
    if account.provider == MailProvider.GENERIC_IMAP:
        return json.dumps([account.host.casefold(), account.port, address])
    return address


class WorkspaceError(RuntimeError):
    pass


class RunNotActiveError(WorkspaceError):
    pass


class WorkspaceStore:
    def __init__(self, database_path: Path, *, recover: bool = False) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.spool_dir = self.database_path.parent / "work"
        self.spool_dir.mkdir(mode=0o700, exist_ok=True)
        self._validate_spool_directory()
        cleanup_key = str(self.database_path.absolute())
        self._cleanup_lock = _WORK_COPY_CLEANUP_LOCK
        with self._cleanup_lock:
            self._pending_work_copy_cleanup = _PENDING_WORK_COPY_CLEANUP.setdefault(
                cleanup_key, set()
            )
        self._initialize()
        if recover:
            self.recover()

    def _validate_spool_directory(self) -> os.stat_result:
        try:
            details = self.spool_dir.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceError("The MailArchive work directory is unavailable.") from exc
        if not stat.S_ISDIR(details.st_mode):
            raise WorkspaceError("The MailArchive work directory is not a safe directory.")
        return details

    @contextmanager
    def _spool_directory_handle(self) -> Iterator[int | None]:
        expected = self._validate_spool_directory()
        if not hasattr(os, "O_DIRECTORY"):
            yield None
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.spool_dir, flags)
        except OSError as exc:
            raise WorkspaceError("The MailArchive work directory is unavailable.") from exc
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(actual.st_mode)
                or actual.st_dev != expected.st_dev
                or actual.st_ino != expected.st_ino
            ):
                raise WorkspaceError("The MailArchive work directory changed unexpectedly.")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def plan_execution(self, plan_id: str) -> Iterator[None]:
        key = (str(self.database_path.absolute()), plan_id)
        lock = _PLAN_EXECUTION_LOCKS[hash(key) % len(_PLAN_EXECUTION_LOCKS)]
        with lock:
            yield

    def read_work_copy(self, path: Path, max_bytes: int) -> bytes:
        path = Path(path)
        if path.parent != self.spool_dir or not path.name:
            raise WorkspaceError("The local working copy path is outside the work directory.")
        with self._spool_directory_handle() as descriptor:
            if descriptor is None:
                try:
                    details = path.stat(follow_symlinks=False)
                except OSError as exc:
                    raise WorkspaceError("The local working copy is unavailable.") from exc
                if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
                    raise WorkspaceError("The local working copy is invalid or too large.")
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise WorkspaceError("The local working copy is unavailable.") from exc
                if len(content) > max_bytes:
                    raise WorkspaceError("The local working copy is invalid or too large.")
                return content
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                file_descriptor = os.open(path.name, flags, dir_fd=descriptor)
            except OSError as exc:
                raise WorkspaceError("The local working copy is unavailable.") from exc
            try:
                details = os.fstat(file_descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
                    raise WorkspaceError("The local working copy is invalid or too large.")
                with os.fdopen(file_descriptor, "rb") as handle:
                    file_descriptor = -1
                    content = handle.read(max_bytes + 1)
                    if len(content) > max_bytes:
                        raise WorkspaceError("The local working copy is invalid or too large.")
                    return content
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)

    def discard_work_copy(self, path: Path) -> None:
        self._remove_work_copy(Path(path))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        existed = self.database_path.exists()
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
                self._validate_schema(db)
                db.commit()
            except sqlite3.DatabaseError as exc:
                raise WorkspaceError(f"Could not open MailArchive profile database: {exc}") from exc
        if os.name != "nt":
            self.database_path.chmod(0o600)

    @staticmethod
    def _validate_schema(db: sqlite3.Connection) -> None:
        found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _REQUIRED_SCHEMA_COLUMNS.keys() <= found:
            raise WorkspaceError("MailArchive profile database is incomplete.")

        for table, required_columns in _REQUIRED_SCHEMA_COLUMNS.items():
            table_info = db.execute(f"PRAGMA table_info({table})").fetchall()
            columns = {row[1] for row in table_info}
            primary_key = tuple(
                row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]
            )
            if not required_columns <= columns or primary_key != _REQUIRED_PRIMARY_KEYS[table]:
                raise WorkspaceError("MailArchive profile database is incomplete.")

        for name, (table, unique, partial, columns) in _REQUIRED_INDEXES.items():
            indexes = {row[1]: row for row in db.execute(f"PRAGMA index_list({table})")}
            index = indexes.get(name)
            sql_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            normalized_sql = (
                "".join(str(sql_row[0]).lower().split()) if sql_row and sql_row[0] else ""
            )
            predicate = normalized_sql.partition("where")[2] or None
            if (
                index is None
                or bool(index[2]) != unique
                or bool(index[4]) != partial
                or tuple(row[2] for row in db.execute(f"PRAGMA index_info({name})")) != columns
                or predicate != _REQUIRED_INDEX_PREDICATES.get(name)
            ):
                raise WorkspaceError("MailArchive profile database is incomplete.")

        for table, required_keys in _REQUIRED_UNIQUE_KEYS.items():
            unique_keys = {
                tuple(row[2] for row in db.execute(f"PRAGMA index_info({index[1]})"))
                for index in db.execute(f"PRAGMA index_list({table})")
                if index[2]
            }
            if not required_keys <= unique_keys:
                raise WorkspaceError("MailArchive profile database is incomplete.")

        for table, required_keys in _REQUIRED_FOREIGN_KEYS.items():
            groups: dict[int, list[tuple[int, str, str, str]]] = {}
            for row in db.execute(f"PRAGMA foreign_key_list({table})"):
                groups.setdefault(row[0], []).append((row[1], row[3], row[2], row[4]))
            foreign_keys = {
                tuple(
                    (source, target_table, target)
                    for _, source, target_table, target in sorted(group)
                )
                for group in groups.values()
            }
            if not required_keys <= foreign_keys:
                raise WorkspaceError("MailArchive profile database is incomplete.")

        for table, required_checks in _REQUIRED_CHECKS.items():
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            normalized_sql = "".join(str(row[0]).lower().split()) if row and row[0] else ""
            if any(required_check not in normalized_sql for required_check in required_checks):
                raise WorkspaceError("MailArchive profile database is incomplete.")

        actual_triggers = {
            str(row[0]): "".join(str(row[1]).lower().split())
            for row in db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
            if row[1]
        }
        canonical = sqlite3.connect(":memory:")
        try:
            canonical.executescript(_SCHEMA)
            required_triggers = {
                str(row[0]): "".join(str(row[1]).lower().split())
                for row in canonical.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
                )
                if row[0] in _REQUIRED_TRIGGERS and row[1]
            }
        finally:
            canonical.close()
        if required_triggers.keys() != _REQUIRED_TRIGGERS or any(
            actual_triggers.get(name) != sql for name, sql in required_triggers.items()
        ):
            raise WorkspaceError("MailArchive profile database is incomplete.")

        WorkspaceStore._validate_runtime_references(db)

    @staticmethod
    def _validate_runtime_references(db: sqlite3.Connection) -> None:
        """Reject relational damage not representable by SQLite foreign keys."""

        WorkspaceStore._validate_config_revision_state(db)
        WorkspaceStore._validate_active_sources(db)

        dangling_active = db.execute(
            """SELECT 1 FROM active_message a
            LEFT JOIN intake i ON a.kind='intake' AND i.id=a.ref_id
              AND i.source_id=a.source_id AND i.message_key=a.message_key
              AND i.status IN ('reserved', 'error')
            LEFT JOIN plan p ON a.kind='plan' AND p.id=a.ref_id
              AND p.source_id=a.source_id AND p.message_key=a.message_key
              AND p.status IN ('open', 'paused')
            WHERE (a.kind='intake' AND i.id IS NULL)
               OR (a.kind='plan' AND p.id IS NULL) LIMIT 1"""
        ).fetchone()
        if dangling_active is not None:
            raise WorkspaceError("MailArchive profile database failed its integrity check.")

        missing_active = db.execute(
            """SELECT
              EXISTS(
                SELECT 1 FROM intake i
                LEFT JOIN active_message a ON a.kind='intake' AND a.ref_id=i.id
                  AND a.source_id=i.source_id AND a.message_key=i.message_key
                WHERE i.status IN ('reserved', 'error') AND a.ref_id IS NULL
              ) OR EXISTS(
                SELECT 1 FROM plan p
                LEFT JOIN active_message a ON a.kind='plan' AND a.ref_id=p.id
                  AND a.source_id=p.source_id AND a.message_key=p.message_key
                WHERE p.status IN ('open', 'paused') AND a.ref_id IS NULL
              )"""
        ).fetchone()[0]
        if missing_active:
            raise WorkspaceError("MailArchive profile database failed its integrity check.")

        completed_with_active_intake = db.execute(
            "SELECT 1 FROM scan_run r JOIN intake i ON i.run_id=r.id "
            "WHERE r.status='completed' AND i.status IN ('reserved', 'error') LIMIT 1"
        ).fetchone()
        if completed_with_active_intake is not None:
            raise WorkspaceError("MailArchive profile database failed its integrity check.")

        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkspaceError("MailArchive profile database failed its integrity check.")

        for run in db.execute(
            "SELECT r.source_id, r.kind, r.selection_json, r.settings_json, r.checkpoint, "
            "c.payload AS revision_payload FROM scan_run r "
            "JOIN config_revision c ON c.id=r.config_revision"
        ):
            WorkspaceStore._validate_run_checkpoint(run)
        WorkspaceStore._validate_plan_snapshots(db)

    @staticmethod
    def _validate_config_revision_state(db: sqlite3.Connection) -> None:
        revision = db.execute(
            "SELECT count(*) AS total, COALESCE(sum(active), 0) AS active FROM config_revision"
        ).fetchone()
        runtime_state = db.execute(
            """SELECT EXISTS(SELECT 1 FROM source)
            OR EXISTS(SELECT 1 FROM source_scope)
            OR EXISTS(SELECT 1 FROM source_message)
            OR EXISTS(SELECT 1 FROM scan_run)
            OR EXISTS(SELECT 1 FROM intake)
            OR EXISTS(SELECT 1 FROM plan)
            OR EXISTS(SELECT 1 FROM plan_target)
            OR EXISTS(SELECT 1 FROM active_message)
            OR EXISTS(SELECT 1 FROM output)
            OR EXISTS(SELECT 1 FROM output_target)
            OR EXISTS(SELECT 1 FROM receipt)
            OR EXISTS(SELECT 1 FROM activity_event)"""
        ).fetchone()[0]
        if (revision["total"] and revision["active"] != 1) or (
            not revision["total"] and runtime_state
        ):
            raise WorkspaceError("MailArchive profile database failed its integrity check.")
        for row in db.execute("SELECT payload FROM config_revision"):
            WorkspaceStore._settings_from_payload(
                row["payload"], "MailArchive profile database failed its integrity check."
            )

    @staticmethod
    def _settings_from_payload(value: object, error: str) -> Settings:
        try:
            if not isinstance(value, str):
                raise ValueError
            payload = json.loads(value)
            if not isinstance(payload, dict):
                raise ValueError
            settings = Settings.from_dict(payload)
            if payload != settings.to_dict():
                raise ValueError
            return settings
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(error) from exc

    @staticmethod
    def _validate_active_sources(db: sqlite3.Connection) -> None:
        revision = db.execute(
            "SELECT payload FROM config_revision WHERE active=1 LIMIT 1"
        ).fetchone()
        if revision is None:
            return
        settings = WorkspaceStore._settings_from_payload(
            revision["payload"], "MailArchive profile database failed its integrity check."
        )
        expected = {
            mailbox.id: (
                account.provider.value,
                source_key(account, mailbox),
                account.id,
                mailbox.address,
                json.dumps(mailbox.folders),
                int(account.enabled and mailbox.enabled),
            )
            for account in settings.accounts
            for mailbox in account.mailboxes
        }
        actual = {
            str(row["id"]): row
            for row in db.execute(
                "SELECT id, provider, mailbox_key, account_id, address, folders_json, enabled "
                "FROM source"
            )
        }
        for source_id, values in expected.items():
            row = actual.get(source_id)
            if (
                row is None
                or tuple(
                    row[key]
                    for key in (
                        "provider",
                        "mailbox_key",
                        "account_id",
                        "address",
                        "folders_json",
                        "enabled",
                    )
                )
                != values
            ):
                raise WorkspaceError("MailArchive profile database failed its integrity check.")
        if any(source_id not in expected and row["enabled"] for source_id, row in actual.items()):
            raise WorkspaceError("MailArchive profile database failed its integrity check.")

    @staticmethod
    def _run_settings(run: sqlite3.Row | dict) -> Settings:
        settings = WorkspaceStore._settings_from_payload(
            run["settings_json"], "The archive run snapshot is damaged."
        )
        keys = set(run.keys())
        if "revision_payload" in keys:
            WorkspaceStore._settings_from_payload(
                run["revision_payload"], "The archive run snapshot is damaged."
            )
            if run["settings_json"] != run["revision_payload"]:
                raise WorkspaceError("The archive run snapshot is damaged.")
        return settings

    @staticmethod
    def _range_context(run: sqlite3.Row | dict) -> tuple[Account, Mailbox, list[str]]:
        try:
            selection = json.loads(run["selection_json"])
            settings = WorkspaceStore._run_settings(run)
            folders = selection["folders"]
            if not isinstance(folders, list) or not all(
                isinstance(folder, str) and folder for folder in folders
            ):
                raise ValueError
            owner = next(
                (
                    (account, mailbox)
                    for account in settings.accounts
                    for mailbox in account.mailboxes
                    if mailbox.id == run["source_id"]
                ),
                None,
            )
            if owner is None or (owner[0].provider != MailProvider.GMAIL_API and not folders):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError("The archive run checkpoint is damaged.") from exc
        return owner[0], owner[1], folders

    @staticmethod
    def _range_scope_keys(run: sqlite3.Row | dict) -> set[str]:
        account, _, folders = WorkspaceStore._range_context(run)
        return {"gmail-mailbox"} if account.provider == MailProvider.GMAIL_API else set(folders)

    @staticmethod
    def _range_namespace_matches(
        account: Account, mailbox: Mailbox, scope_key: str, namespace: str
    ) -> bool:
        if account.provider != MailProvider.GENERIC_IMAP:
            return namespace == mailbox_namespace(account, mailbox)
        if not namespace.startswith("imap-v3:"):
            return False
        try:
            components = json.loads(namespace.removeprefix("imap-v3:"))
        except (TypeError, ValueError):
            return False
        folder = "INBOX" if scope_key.upper() == "INBOX" else scope_key
        expected = [
            account.host.casefold(),
            account.port,
            mailbox.address.strip().casefold(),
            folder,
        ]
        return (
            isinstance(components, list)
            and len(components) == 5
            and components[:4] == expected
            and isinstance(components[4], str)
            and components[4].isdigit()
            and 1 <= int(components[4]) <= 4_294_967_295
        )

    @staticmethod
    def _validate_run_checkpoint(run: sqlite3.Row | dict) -> dict:
        WorkspaceStore._run_settings(run)
        checkpoint = WorkspaceStore._checkpoint_data(run["checkpoint"])
        targets = checkpoint.get("range_targets", {})
        if not isinstance(targets, dict) or (targets and run["kind"] != "manual"):
            raise WorkspaceError("The archive run checkpoint is damaged.")
        context = WorkspaceStore._range_context(run) if run["kind"] == "manual" else None
        expected = (
            (
                {"gmail-mailbox"}
                if context[0].provider == MailProvider.GMAIL_API
                else set(context[2])
            )
            if context is not None
            else set()
        )
        if not targets.keys() <= expected:
            raise WorkspaceError("The archive run checkpoint is damaged.")
        for scope_key, target in targets.items():
            if not isinstance(target, dict) or set(target) != {
                "namespace",
                "token",
                "complete",
            }:
                raise WorkspaceError("The archive run checkpoint is damaged.")
            namespace = target["namespace"]
            token = target["token"]
            complete = target["complete"]
            if (
                not isinstance(namespace, str)
                or not namespace
                or (token is not None and (not isinstance(token, str) or not token))
                or type(complete) is not bool
                or (complete and token is not None)
                or (
                    context is not None
                    and not WorkspaceStore._range_namespace_matches(
                        context[0], context[1], scope_key, namespace
                    )
                )
            ):
                raise WorkspaceError("The archive run checkpoint is damaged.")
        return checkpoint

    @staticmethod
    def _validate_plan_snapshot(db: sqlite3.Connection, plan: sqlite3.Row) -> None:
        try:
            snapshot = json.loads(plan["rule_json"])
            if not isinstance(snapshot, dict) or set(snapshot) != {"rule", "timezone"}:
                raise ValueError
            if not isinstance(snapshot["rule"], dict) or not isinstance(snapshot["timezone"], str):
                raise ValueError
            rule = Rule.from_dict(snapshot["rule"])
            Settings(
                archive_root="", rules=[rule], archive_timezone=snapshot["timezone"]
            ).validate()
            if snapshot != {"rule": rule.to_dict(), "timezone": snapshot["timezone"]}:
                raise ValueError
            expected = {
                target.id: (
                    target.path,
                    target.save_mode.value,
                    int(target.attachments_in_destination),
                )
                for target in rule.targets
            }
            actual = {
                str(row["target_id"]): (
                    str(row["path"]),
                    str(row["save_mode"]),
                    int(row["attachments_in_destination"]),
                )
                for row in db.execute(
                    "SELECT target_id, path, save_mode, attachments_in_destination "
                    "FROM plan_target WHERE plan_id=?",
                    (plan["id"],),
                )
            }
            if actual != expected:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError("The archive plan snapshot is damaged.") from exc

    @staticmethod
    def _validate_plan_snapshots(db: sqlite3.Connection) -> None:
        for plan in db.execute("SELECT id, rule_json FROM plan"):
            WorkspaceStore._validate_plan_snapshot(db, plan)

    def recover(self) -> None:
        # Interrupted output attempts remain pending. Only unreferenced temporary
        # downloads are removed; accepted raw mail is never expired automatically.
        with self.connection() as db, db:
            db.execute("UPDATE scan_run SET status='interrupted' WHERE status='running'")
            referenced = {
                row[0]
                for row in db.execute(
                    "SELECT raw_path FROM plan WHERE status IN ('open', 'paused') "
                    "AND raw_path IS NOT NULL"
                )
            }
        with self._spool_directory_handle() as descriptor:
            if descriptor is None:
                paths = list(self.spool_dir.glob("*.tmp")) + list(self.spool_dir.glob("*.eml"))
                for path in paths:
                    try:
                        details = path.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode) and str(path) not in referenced:
                        self._unlink_work_copy(path, None)
                return
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if Path(entry.name).suffix not in {".eml", ".tmp"}:
                        continue
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    path = self.spool_dir / entry.name
                    if stat.S_ISREG(details.st_mode) and str(path) not in referenced:
                        self._unlink_work_copy(path, descriptor)

    def _unlink_work_copy(self, path: Path, descriptor: int | None) -> None:
        try:
            if descriptor is None:
                path.unlink(missing_ok=True)
            else:
                os.unlink(path.name, dir_fd=descriptor)
        except FileNotFoundError:
            with self._cleanup_lock:
                self._pending_work_copy_cleanup.discard(path)
        except OSError:
            with self._cleanup_lock:
                self._pending_work_copy_cleanup.add(path)
        else:
            with self._cleanup_lock:
                self._pending_work_copy_cleanup.discard(path)

    def _remove_work_copy(self, path: Path) -> None:
        """Delete obsolete spool data without reversing its durable state transition."""
        path = Path(path)
        if path.parent != self.spool_dir or not path.name:
            with self._cleanup_lock:
                self._pending_work_copy_cleanup.add(path)
            return
        try:
            with self._spool_directory_handle() as descriptor:
                self._unlink_work_copy(path, descriptor)
        except WorkspaceError:
            with self._cleanup_lock:
                self._pending_work_copy_cleanup.add(path)

    def _retry_work_copy_cleanup(self) -> None:
        with self._cleanup_lock:
            pending = tuple(self._pending_work_copy_cleanup)
        for path in pending:
            self._remove_work_copy(path)

    def load_settings(self) -> Settings:
        with self.connection() as db:
            row = db.execute(
                "SELECT id, payload FROM config_revision WHERE active=1 LIMIT 1"
            ).fetchone()
            revision_count = int(db.execute("SELECT count(*) FROM config_revision").fetchone()[0])
        if row is None:
            if revision_count:
                raise WorkspaceError("MailArchive profile settings are damaged.")
            return Settings.defaults()
        settings = self._settings_from_payload(
            row["payload"], "MailArchive profile settings are damaged."
        )
        with self.connection() as db:
            self._validate_active_sources(db)
        settings.config_revision = int(row["id"])
        return settings

    def ensure_configuration_revision(self) -> int:
        """Persist defaults before a component writes otherwise standalone runtime data."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_config_revision_state(db)
            self._validate_active_sources(db)
            row = db.execute("SELECT id FROM config_revision WHERE active=1 LIMIT 1").fetchone()
            if row is not None:
                return int(row["id"])
            payload = json.dumps(Settings.defaults().to_dict(), ensure_ascii=False, sort_keys=True)
            db.execute(
                "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 1)",
                (payload, now()),
            )
            return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    def save_settings(self, settings: Settings) -> int:
        settings.validate()
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._validate_config_revision_state(db)
            self._validate_active_sources(db)
            previous = db.execute(
                "SELECT id, payload FROM config_revision WHERE active=1 LIMIT 1"
            ).fetchone()
            self._normalize_source_ids(db, settings)
            payload = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
            if previous and previous["payload"] == payload:
                revision = int(previous["id"])
                settings.config_revision = revision
                return revision
            db.execute("UPDATE config_revision SET active=0 WHERE active=1")
            db.execute(
                "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 1)",
                (payload, now()),
            )
            revision = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            self._replace_current_sources(db, settings)
            settings.config_revision = revision
            return revision

    @staticmethod
    def _normalize_source_ids(db: sqlite3.Connection, settings: Settings) -> None:
        """Bind each configured mailbox to its durable provider identity."""
        bindings: set[tuple[str, str]] = set()
        source_ids: dict[str, tuple[str, str]] = {}
        for account in settings.accounts:
            for mailbox in account.mailboxes:
                binding = (account.provider.value, source_key(account, mailbox))
                if binding in bindings:
                    raise WorkspaceError(
                        f"Mailbox {mailbox.address} is configured twice for this provider."
                    )
                bindings.add(binding)
                if mailbox.id in source_ids:
                    raise WorkspaceError(
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
    def _source_values(
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

    def _replace_current_sources(self, db: sqlite3.Connection, settings: Settings) -> None:
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
                    self._remove_deselected_scopes(
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
                    self._source_values(account, mailbox, enabled, discovery_pending),
                )

    @staticmethod
    def _remove_deselected_scopes(
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
            self._validate_config_revision_state(db)
            self._validate_active_sources(db)
            claimed_revision = settings.config_revision
            self._normalize_source_ids(db, settings)
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
                    (payload, now()),
                )
                revision_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                self._replace_current_sources(db, settings)
            else:
                revision = db.execute(
                    "SELECT id FROM config_revision WHERE payload=? ORDER BY id DESC LIMIT 1",
                    (payload,),
                ).fetchone()
                if revision is None:
                    db.execute(
                        "INSERT INTO config_revision(payload, created_at, active) VALUES (?, ?, 0)",
                        (payload, now()),
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
                            self._source_values(account, mailbox, 0),
                        )
            settings.config_revision = revision_id
            return revision_id

    def configuration_revision(self) -> int:
        with self.connection() as db:
            self._validate_config_revision_state(db)
            row = db.execute("SELECT id FROM config_revision WHERE active=1 LIMIT 1").fetchone()
        return int(row[0]) if row else 0

    def start_run(
        self,
        source_id: str,
        kind: str,
        selection: dict,
        settings: Settings,
        config_revision: int,
    ) -> str:
        run_id = str(uuid4())
        with self.connection() as db, db:
            snapshot = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
            revision = db.execute(
                "SELECT payload FROM config_revision WHERE id=?", (config_revision,)
            ).fetchone()
            if revision is None or revision["payload"] != snapshot:
                raise WorkspaceError("The run configuration revision does not match its snapshot.")
            db.execute(
                "INSERT INTO scan_run(id, source_id, kind, selection_json, config_revision, "
                "settings_json, status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    run_id,
                    source_id,
                    kind,
                    json.dumps(selection),
                    config_revision,
                    snapshot,
                    now(),
                ),
            )
        return run_id

    def finish_run(self, run_id: str, *, error: str | None = None) -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT r.source_id, r.kind, r.status, r.selection_json, r.settings_json, "
                "r.checkpoint, "
                "c.payload AS revision_payload FROM scan_run r "
                "JOIN config_revision c ON c.id=r.config_revision WHERE r.id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise WorkspaceError("The archive run is unavailable.")
            if run["status"] == "cancelled":
                return
            if run["status"] != "running":
                raise WorkspaceError("The archive run is no longer active.")
            checkpoint = self._validate_run_checkpoint(run)
            unresolved = db.execute(
                "SELECT error FROM intake WHERE run_id=? AND status IN ('reserved', 'error')",
                (run_id,),
            ).fetchall()
            if unresolved:
                details = list(
                    dict.fromkeys(
                        str(row["error"] or "download is still pending") for row in unresolved
                    )
                )
                pending_error = (
                    f"{len(unresolved)} message intake(s) remain unresolved: "
                    + "; ".join(details[:3])
                )
                error = "; ".join(item for item in (error, pending_error) if item)
            if run["kind"] == "manual" and error is None:
                expected = self._range_scope_keys(run)
                targets = checkpoint.get("range_targets", {})
                complete = {
                    scope_key
                    for scope_key, target in targets.items()
                    if target["complete"] and target["token"] is None
                }
                if complete != expected:
                    error = "The range search did not complete every frozen target."
            changed = db.execute(
                "UPDATE scan_run SET status=?, error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                ("failed" if error else "completed", error, now(), run_id),
            ).rowcount
            if changed != 1:
                raise WorkspaceError("The archive run could not be finished safely.")

    def cancel_run(self, run_id: str) -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE scan_run SET status='cancelled', finished_at=? WHERE id=? "
                "AND status IN ('running', 'failed', 'interrupted')",
                (now(), run_id),
            ).rowcount
            if not changed:
                return
            rows = db.execute(
                "SELECT id, source_id, message_key FROM intake WHERE run_id=? "
                "AND status IN ('reserved', 'error')",
                (run_id,),
            ).fetchall()
            for row in rows:
                db.execute("UPDATE intake SET status='cancelled' WHERE id=?", (row["id"],))
                db.execute(
                    "DELETE FROM active_message WHERE source_id=? AND message_key=? AND kind='intake'",
                    (row["source_id"], row["message_key"]),
                )

    def run_status(self, run_id: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT status FROM scan_run WHERE id=?", (run_id,)).fetchone()
        return str(row[0]) if row else None

    def incomplete_manual_runs(self) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM scan_run WHERE kind='manual' "
                "AND status IN ('interrupted', 'failed') ORDER BY started_at"
            ).fetchall()

    def run_settings_snapshot(self, run_id: str) -> Settings:
        """Return a run snapshot only after validating it against its revision."""
        with self.connection() as db:
            run = db.execute(
                "SELECT r.source_id, r.kind, r.selection_json, r.settings_json, r.checkpoint, "
                "c.payload AS revision_payload FROM scan_run r "
                "JOIN config_revision c ON c.id=r.config_revision WHERE r.id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise WorkspaceError("The archive run snapshot is unavailable.")
            self._validate_run_checkpoint(run)
            return self._run_settings(run)

    def unresolved_intakes(self, run_id: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM intake WHERE run_id=? AND status IN ('reserved', 'error') "
                "ORDER BY created_at",
                (run_id,),
            ).fetchall()

    def intake_errors(
        self, limit: int = 100, *, before: tuple[str, str] | None = None
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("Intake error page size must be positive.")
        before_time, before_id = before or (None, None)
        with self.connection() as db:
            return db.execute(
                "SELECT i.*, r.kind, r.status AS run_status, m.subject "
                "FROM intake i JOIN scan_run r ON r.id=i.run_id "
                "LEFT JOIN source_message m ON m.source_id=i.source_id "
                "AND m.message_key=i.message_key WHERE i.status='error' "
                "AND (? IS NULL OR i.created_at < ? "
                "OR (i.created_at = ? AND i.id < ?)) "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
                (before_time, before_time, before_time, before_id, limit),
            ).fetchall()

    def intake_error_count(self) -> int:
        with self.connection() as db:
            return int(db.execute("SELECT count(*) FROM intake WHERE status='error'").fetchone()[0])

    def intake_error_snapshot(self, limit: int = 100) -> tuple[list[sqlite3.Row], int]:
        """Return the first error page and its total from one database snapshot."""
        if limit < 1:
            raise ValueError("Intake error page size must be positive.")
        with self.connection() as db:
            db.execute("BEGIN")
            total = int(
                db.execute("SELECT count(*) FROM intake WHERE status='error'").fetchone()[0]
            )
            rows = db.execute(
                "SELECT i.*, r.kind, r.status AS run_status, m.subject "
                "FROM intake i JOIN scan_run r ON r.id=i.run_id "
                "LEFT JOIN source_message m ON m.source_id=i.source_id "
                "AND m.message_key=i.message_key WHERE i.status='error' "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows, total

    def pending_automatic_intakes(
        self, *, due_only: bool = False, at: datetime | None = None
    ) -> list[sqlite3.Row]:
        """Return unfinished automatic intakes with their immutable run snapshot."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT i.*, r.settings_json FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE r.kind='automatic' "
                "AND i.status IN ('reserved', 'error') ORDER BY i.created_at"
            ).fetchall()
        if not due_only:
            return rows
        current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return [
            row
            for row in rows
            if _intake_retry_due(str(row["status"]), row["retry_after"], current)
        ]

    def cancel_intake(self, intake_id: str) -> None:
        """Explicitly abandon one unresolved intake without affecting accepted work."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT source_id, message_key FROM intake WHERE id=? "
                "AND status IN ('reserved', 'error')",
                (intake_id,),
            ).fetchone()
            if row is None:
                return
            db.execute(
                "UPDATE intake SET status='cancelled', error=? WHERE id=?",
                ("Message intake was cancelled by the user.", intake_id),
            )
            db.execute(
                "UPDATE source_message SET terminal_state='aborted' "
                "WHERE source_id=? AND message_key=?",
                (row["source_id"], row["message_key"]),
            )
            db.execute(
                "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                "AND kind='intake' AND ref_id=?",
                (row["source_id"], row["message_key"], intake_id),
            )

    def restart_run(self, run_id: str) -> None:
        with self.connection() as db, db:
            changed = db.execute(
                "UPDATE scan_run SET status='running', error=NULL, "
                "finished_at=NULL WHERE id=? AND kind='manual' "
                "AND status IN ('interrupted', 'failed')",
                (run_id,),
            ).rowcount
            if not changed:
                raise WorkspaceError("This range run cannot be resumed.")

    @staticmethod
    def _checkpoint_data(value: str | None) -> dict:
        if not value:
            return {}
        try:
            checkpoint = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("The archive run checkpoint is damaged.") from exc
        if not isinstance(checkpoint, dict):
            raise WorkspaceError("The archive run checkpoint is damaged.")
        return checkpoint

    def range_target_checkpoint(self, run_id: str, scope_key: str) -> dict[str, object]:
        with self.connection() as db:
            row = db.execute(
                "SELECT r.source_id, r.kind, r.selection_json, r.settings_json, r.checkpoint, "
                "c.payload AS revision_payload FROM scan_run r "
                "JOIN config_revision c ON c.id=r.config_revision WHERE r.id=?",
                (run_id,),
            ).fetchone()
        if row is None or row["kind"] != "manual":
            raise WorkspaceError("The range run checkpoint is unavailable.")
        expected = self._range_scope_keys(row)
        if scope_key not in expected:
            raise WorkspaceError("The archive run checkpoint is damaged.")
        checkpoint = self._validate_run_checkpoint(row)
        targets = checkpoint.get("range_targets", {})
        target = targets.get(scope_key, {})
        namespace = target.get("namespace")
        token = target.get("token")
        complete = target.get("complete", False)
        return {"namespace": namespace, "token": token, "complete": complete}

    def update_range_target_checkpoint(
        self,
        run_id: str,
        scope_key: str,
        namespace: str,
        token: str | None,
        complete: bool,
        *,
        force: bool = False,
    ) -> bool:
        """Persist a safe next-page boundary after all prior candidates were handled."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.source_id, r.kind, r.status, r.selection_json, r.settings_json, "
                "r.checkpoint, c.payload AS revision_payload FROM scan_run r "
                "JOIN config_revision c ON c.id=r.config_revision WHERE r.id=?",
                (run_id,),
            ).fetchone()
            if row is None or row["kind"] != "manual" or row["status"] != "running":
                raise RunNotActiveError("The range run is no longer active.")
            if (
                not isinstance(namespace, str)
                or not namespace
                or (token is not None and (not isinstance(token, str) or not token))
                or type(complete) is not bool
                or (complete and token is not None)
                or scope_key not in self._range_scope_keys(row)
            ):
                raise WorkspaceError("The archive run checkpoint is damaged.")
            if (
                not force
                and db.execute(
                    "SELECT 1 FROM intake WHERE run_id=? AND scope_key=? "
                    "AND status IN ('reserved', 'error') LIMIT 1",
                    (run_id, scope_key),
                ).fetchone()
            ):
                return False
            checkpoint = self._checkpoint_data(row["checkpoint"])
            targets = checkpoint.setdefault("range_targets", {})
            if not isinstance(targets, dict):
                raise WorkspaceError("The archive run checkpoint is damaged.")
            targets[scope_key] = {
                "namespace": namespace,
                "token": token,
                "complete": complete,
            }
            self._validate_run_checkpoint(
                {
                    "source_id": row["source_id"],
                    "kind": row["kind"],
                    "selection_json": row["selection_json"],
                    "settings_json": row["settings_json"],
                    "revision_payload": row["revision_payload"],
                    "checkpoint": json.dumps(checkpoint),
                }
            )
            checkpoint["updated_at"] = now()
            db.execute(
                "UPDATE scan_run SET checkpoint=? WHERE id=?",
                (json.dumps(checkpoint, sort_keys=True), run_id),
            )
        return True

    def scope(self, source_id: str, scope_key: str) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM source_scope WHERE source_id=? AND scope_key=?",
                (source_id, scope_key),
            ).fetchone()

    def source_monitoring_status(
        self, source_id: str, provider: MailProvider, folders: list[str]
    ) -> str:
        """Return the user-facing state of one configured mailbox baseline."""
        with self.connection() as db:
            source = db.execute(
                "SELECT discovery_pending FROM source WHERE id=?", (source_id,)
            ).fetchone()
            rows = db.execute(
                "SELECT scope_key, baseline_done, status FROM source_scope WHERE source_id=?",
                (source_id,),
            ).fetchall()
        scopes = {str(row["scope_key"]): row for row in rows}
        if any(row["status"] == "paused" for row in rows):
            return "paused"
        if source is None or source["discovery_pending"]:
            return "setting_up"
        if provider == MailProvider.GMAIL_API:
            expected = {"gmail-mailbox"}
            expected.update("gmail-label:" + item for item in (folders or ["*"]))
        elif folders:
            expected = set(folders)
        else:
            return (
                "active"
                if rows and all(row["baseline_done"] and row["status"] == "active" for row in rows)
                else "setting_up"
            )
        return (
            "active"
            if all(
                key in scopes and scopes[key]["baseline_done"] and scopes[key]["status"] == "active"
                for key in expected
            )
            else "setting_up"
        )

    def prepare_scope_discovery(self, source_id: str, scope_keys: set[str]) -> None:
        """Record the complete current folder set before any baseline can finish."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute("SELECT provider FROM source WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise WorkspaceError("The discovered source is not configured.")
            sorted_keys = sorted(scope_keys)
            if source["provider"] == MailProvider.GENERIC_IMAP.value:
                predicate = ""
                parameters: tuple = (source_id,)
                if sorted_keys:
                    placeholders = ",".join("?" for _ in sorted_keys)
                    predicate = f" AND i.scope_key NOT IN ({placeholders})"
                    parameters = (source_id, *sorted_keys)
                removed_intakes = db.execute(
                    "SELECT i.id, i.message_key FROM intake i "
                    "JOIN scan_run r ON r.id=i.run_id "
                    "WHERE i.source_id=? AND r.kind='automatic' "
                    "AND i.status IN ('reserved', 'error')" + predicate,
                    parameters,
                ).fetchall()
                for intake in removed_intakes:
                    db.execute("UPDATE intake SET status='filtered' WHERE id=?", (intake["id"],))
                    db.execute(
                        "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                        "AND kind='intake' AND ref_id=?",
                        (source_id, intake["message_key"], intake["id"]),
                    )
            if sorted_keys:
                placeholders = ",".join("?" for _ in sorted_keys)
                db.execute(
                    f"DELETE FROM source_scope WHERE source_id=? "
                    f"AND scope_key NOT IN ({placeholders})",
                    (source_id, *sorted_keys),
                )
            else:
                db.execute("DELETE FROM source_scope WHERE source_id=?", (source_id,))
            for scope_key in sorted_keys:
                db.execute(
                    "INSERT OR IGNORE INTO source_scope(source_id, scope_key) VALUES (?, ?)",
                    (source_id, scope_key),
                )
            db.execute("UPDATE source SET discovery_pending=0 WHERE id=?", (source_id,))

    def finish_scope(
        self,
        source_id: str,
        scope_key: str,
        processing_namespace: str,
        synchronization_namespace: str,
        cursor: str,
    ) -> None:
        with self.connection() as db, db:
            db.execute(
                """INSERT INTO source_scope(source_id, scope_key, processing_namespace,
                synchronization_namespace, baseline_done, cursor, status)
                VALUES (?, ?, ?, ?, 1, ?, 'active')
                ON CONFLICT(source_id, scope_key) DO UPDATE SET
                  processing_namespace=excluded.processing_namespace,
                  synchronization_namespace=excluded.synchronization_namespace,
                  baseline_done=1, cursor=excluded.cursor, status='active', error=NULL""",
                (source_id, scope_key, processing_namespace, synchronization_namespace, cursor),
            )

    def pause_scope(self, source_id: str, scope_key: str, error: str) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE source_scope SET status='paused', error=? WHERE source_id=? AND scope_key=?",
                (error, source_id, scope_key),
            )

    def reset_scope_baseline(self, source_id: str, scope_key: str) -> int:
        """Explicitly accept a new source identity after a paused IMAP folder."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT id, message_key FROM intake WHERE source_id=? AND scope_key=? "
                "AND status IN ('reserved', 'error')",
                (source_id, scope_key),
            ).fetchall()
            for row in pending:
                db.execute(
                    "UPDATE intake SET status='cancelled', error=? WHERE id=?",
                    ("Source baseline was reset before download.", row["id"]),
                )
                db.execute(
                    "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                    "AND kind='intake'",
                    (source_id, row["message_key"]),
                )
            db.execute(
                "UPDATE source_scope SET baseline_done=0, cursor=NULL, status='new', "
                "error=NULL, processing_namespace=NULL, synchronization_namespace=NULL "
                "WHERE source_id=? AND scope_key=?",
                (source_id, scope_key),
            )
        return len(pending)

    def baseline_message(self, source_id: str, message_key: str) -> None:
        with self.connection() as db, db:
            db.execute(
                "INSERT INTO source_message(source_id, message_key, first_seen_at, terminal_state) "
                "VALUES (?, ?, ?, 'baseline') ON CONFLICT(source_id, message_key) DO NOTHING",
                (source_id, message_key, now()),
            )

    def intake_snapshot(self, intake_id: str) -> Settings:
        with self.connection() as db:
            row = db.execute(
                "SELECT r.settings_json FROM intake i JOIN scan_run r ON r.id=i.run_id "
                "WHERE i.id=?",
                (intake_id,),
            ).fetchone()
        if not row:
            raise WorkspaceError("Intake reservation is missing its rule snapshot.")
        return Settings.from_dict(json.loads(row[0]))

    def release_intake(self, intake_id: str, status: str = "filtered") -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._active_intake(db, intake_id)
            changed = db.execute(
                "UPDATE intake SET status=? WHERE id=? AND status IN ('reserved', 'error')",
                (status, intake_id),
            ).rowcount
            if changed != 1:
                raise WorkspaceError("The message intake could not be released safely.")
            deleted = db.execute(
                "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                "AND kind='intake' AND ref_id=?",
                (row["source_id"], row["message_key"], intake_id),
            ).rowcount
            if deleted != 1:
                raise WorkspaceError("The intake's active message reference is damaged.")

    def mark_filtered(
        self,
        intake_id: str,
        *,
        received_at: str,
        received_origin: str,
    ) -> None:
        """Finish an out-of-range intake while retaining known provider metadata."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._active_intake(db, intake_id)
            db.execute(
                "UPDATE source_message SET received_at=?, received_origin=? "
                "WHERE source_id=? AND message_key=?",
                (received_at, received_origin, row["source_id"], row["message_key"]),
            )
            db.execute(
                "UPDATE intake SET status='filtered', error=NULL WHERE id=? "
                "AND status IN ('reserved', 'error')",
                (intake_id,),
            )
            db.execute(
                "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                "AND kind='intake' AND ref_id=?",
                (row["source_id"], row["message_key"], intake_id),
            )

    def discard_pending(self, source_id: str, scope_key: str, remote_ids: set[str]) -> int:
        """Release pending provider IDs that no longer belong to the scan scope."""
        if not remote_ids:
            return 0
        placeholders = ",".join("?" for _ in remote_ids)
        parameters = (source_id, scope_key, *sorted(remote_ids))
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                f"SELECT id, message_key FROM intake WHERE source_id=? AND scope_key=? "
                f"AND status IN ('reserved', 'error') AND remote_id IN ({placeholders})",
                parameters,
            ).fetchall()
            for row in rows:
                db.execute("UPDATE intake SET status='filtered' WHERE id=?", (row["id"],))
                db.execute(
                    "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                    "AND kind='intake' AND ref_id=?",
                    (source_id, row["message_key"], row["id"]),
                )
        return len(rows)

    def pending_rechecks(
        self,
        source_id: str,
        scope_key: str,
        *,
        force_retry: bool = False,
        at: datetime | None = None,
    ) -> set[str]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT i.remote_id, i.status, i.retry_after FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id "
                "WHERE i.source_id=? AND i.scope_key=? AND r.kind='automatic' "
                "AND i.status IN ('reserved', 'error')",
                (source_id, scope_key),
            ).fetchall()
        if force_retry:
            return {str(row["remote_id"]) for row in rows}
        current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return {
            str(row["remote_id"])
            for row in rows
            if _intake_retry_due(str(row["status"]), row["retry_after"], current)
        }

    def intake_retry_deferred(self, source_id: str, message_key: str) -> bool:
        """Whether an active automatic intake is still inside its retry backoff."""
        with self.connection() as db:
            row = db.execute(
                "SELECT i.status, i.retry_after FROM active_message a "
                "JOIN intake i ON a.kind='intake' AND i.id=a.ref_id "
                "JOIN scan_run r ON r.id=i.run_id "
                "WHERE a.source_id=? AND a.message_key=? AND r.kind='automatic'",
                (source_id, message_key),
            ).fetchone()
        return bool(
            row
            and not _intake_retry_due(
                str(row["status"]), row["retry_after"], datetime.now(timezone.utc)
            )
        )

    def reserve(
        self,
        source_id: str,
        message_key: str,
        run_id: str,
        *,
        automatic: bool,
        scope_key: str = "",
        remote_id: str = "",
        force_retry: bool = False,
    ) -> str | None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT status, checkpoint FROM scan_run WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "running":
                raise RunNotActiveError("The archive run is no longer active.")
            active = db.execute(
                "SELECT kind, ref_id FROM active_message WHERE source_id=? AND message_key=?",
                (source_id, message_key),
            ).fetchone()
            if active:
                if active["kind"] != "intake":
                    return None
                owner = db.execute(
                    "SELECT i.run_id, i.status, i.retry_after, r.kind FROM intake i "
                    "JOIN scan_run r ON r.id=i.run_id WHERE i.id=?",
                    (active["ref_id"],),
                ).fetchone()
                if owner and (
                    owner["run_id"] == run_id or (automatic and owner["kind"] == "automatic")
                ):
                    if (
                        automatic
                        and not force_retry
                        and not _intake_retry_due(
                            str(owner["status"]),
                            owner["retry_after"],
                            datetime.now(timezone.utc),
                        )
                    ):
                        return None
                    return str(active["ref_id"])
                return None
            if (
                automatic
                and db.execute(
                    "SELECT 1 FROM source_message WHERE source_id=? AND message_key=? "
                    "AND terminal_state IS NOT NULL",
                    (source_id, message_key),
                ).fetchone()
            ):
                return None
            if (
                not automatic
                and db.execute(
                    "SELECT 1 FROM intake WHERE run_id=? AND message_key=? LIMIT 1",
                    (run_id, message_key),
                ).fetchone()
            ):
                return None
            active_intakes = int(
                db.execute("SELECT count(*) FROM active_message WHERE kind='intake'").fetchone()[0]
            )
            if active_intakes >= MAX_ACTIVE_INTAKES:
                raise IntakeQueueCapacityError(
                    "The unresolved message intake queue reached its capacity. "
                    "Retry or cancel existing intake errors before continuing discovery."
                )
            intake_id = str(uuid4())
            db.execute(
                "INSERT INTO source_message(source_id, message_key, first_seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(source_id, message_key) DO NOTHING",
                (source_id, message_key, now()),
            )
            db.execute(
                "INSERT INTO intake(id, source_id, message_key, run_id, scope_key, remote_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)",
                (intake_id, source_id, message_key, run_id, scope_key, remote_id, now()),
            )
            db.execute(
                "INSERT INTO active_message(source_id, message_key, kind, ref_id) VALUES (?, ?, 'intake', ?)",
                (source_id, message_key, intake_id),
            )
            checkpoint = self._checkpoint_data(run["checkpoint"])
            checkpoint.update(
                {"scope_key": scope_key, "last_remote_id": remote_id, "updated_at": now()}
            )
            db.execute(
                "UPDATE scan_run SET checkpoint=? WHERE id=?",
                (json.dumps(checkpoint, sort_keys=True), run_id),
            )
            return intake_id

    def mark_unmatched(
        self,
        intake_id: str,
        *,
        received_at: str,
        received_origin: str,
        sender_at: str | None,
        subject: str,
    ) -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._active_intake(db, intake_id)
            db.execute(
                "UPDATE source_message SET received_at=?, received_origin=?, sender_at=?, subject=?, terminal_state='unmatched' "
                "WHERE source_id=? AND message_key=?",
                (
                    received_at,
                    received_origin,
                    sender_at,
                    subject,
                    row["source_id"],
                    row["message_key"],
                ),
            )
            db.execute("UPDATE intake SET status='unmatched' WHERE id=?", (intake_id,))
            db.execute("DELETE FROM active_message WHERE source_id=? AND message_key=?", tuple(row))

    def mark_intake_error(
        self,
        intake_id: str,
        error: str,
        *,
        received_at: str | None = None,
        received_origin: str | None = None,
    ) -> bool:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._active_intake(db, intake_id)
            except RunNotActiveError:
                return False
            intake = db.execute(
                "SELECT i.attempts, r.kind FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE i.id=?",
                (intake_id,),
            ).fetchone()
            attempts = int(intake["attempts"]) + 1
            retry_after = _next_retry_after(attempts) if intake["kind"] == "automatic" else None
            if received_at is not None:
                db.execute(
                    "UPDATE source_message SET received_at=?, received_origin=? "
                    "WHERE source_id=? AND message_key=?",
                    (
                        received_at,
                        received_origin or "",
                        row["source_id"],
                        row["message_key"],
                    ),
                )
            db.execute(
                "UPDATE intake SET status='error', error=?, attempts=?, retry_after=? WHERE id=? "
                "AND status IN ('reserved', 'error')",
                (error, attempts, retry_after, intake_id),
            )
            return True

    def reject_intake(
        self,
        intake_id: str,
        error: str,
        *,
        received_at: str | None = None,
        received_origin: str | None = None,
    ) -> None:
        """Finish an intake whose message can never fit within supported bounds."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._active_intake(db, intake_id)
            db.execute(
                "UPDATE source_message SET received_at=COALESCE(?, received_at), "
                "received_origin=COALESCE(?, received_origin), terminal_state='rejected' "
                "WHERE source_id=? AND message_key=?",
                (received_at, received_origin, row["source_id"], row["message_key"]),
            )
            db.execute(
                "UPDATE intake SET status='rejected', error=? WHERE id=?",
                (error, intake_id),
            )
            db.execute(
                "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                "AND kind='intake' AND ref_id=?",
                (row["source_id"], row["message_key"], intake_id),
            )

    @staticmethod
    def _active_intake(db: sqlite3.Connection, intake_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT i.source_id, i.message_key FROM intake i "
            "JOIN scan_run r ON r.id=i.run_id WHERE i.id=? "
            "AND i.status IN ('reserved', 'error') AND r.status!='cancelled'",
            (intake_id,),
        ).fetchone()
        if row is None:
            raise RunNotActiveError("The intake's archive run is no longer active.")
        active = db.execute(
            "SELECT 1 FROM active_message WHERE source_id=? AND message_key=? "
            "AND kind='intake' AND ref_id=?",
            (row["source_id"], row["message_key"], intake_id),
        ).fetchone()
        if active is None:
            raise WorkspaceError("The intake's active message reference is damaged.")
        return row

    @staticmethod
    def _active_plan(db: sqlite3.Connection, plan_id: str) -> sqlite3.Row | None:
        plan = db.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
        if plan is None or plan["status"] not in {"open", "paused"}:
            return None
        active = db.execute(
            "SELECT 1 FROM active_message WHERE source_id=? AND message_key=? "
            "AND kind='plan' AND ref_id=?",
            (plan["source_id"], plan["message_key"], plan_id),
        ).fetchone()
        if active is None:
            raise WorkspaceError("The archive plan's active message reference is damaged.")
        return plan

    def accept_plan(
        self,
        intake_id: str,
        raw_path: Path,
        raw_hash: str,
        received_at: str,
        received_origin: str,
        sender_at: str | None,
        subject: str,
        rule_json: str,
    ) -> str:
        plan_id = str(uuid4())
        try:
            snapshot = json.loads(rule_json)
            if not isinstance(snapshot, dict) or set(snapshot) != {"rule", "timezone"}:
                raise ValueError
            if not isinstance(snapshot["rule"], dict) or not isinstance(snapshot["timezone"], str):
                raise ValueError
            rule = Rule.from_dict(snapshot["rule"])
            Settings(
                archive_root="", rules=[rule], archive_timezone=snapshot["timezone"]
            ).validate()
            if snapshot != {"rule": rule.to_dict(), "timezone": snapshot["timezone"]}:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError("The archive plan snapshot is damaged.") from exc
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._active_intake(db, intake_id)
            intake = db.execute("SELECT * FROM intake WHERE id=?", (intake_id,)).fetchone()
            db.execute(
                "UPDATE source_message SET received_at=?, received_origin=?, sender_at=?, subject=?, terminal_state=NULL "
                "WHERE source_id=? AND message_key=?",
                (
                    received_at,
                    received_origin,
                    sender_at,
                    subject,
                    intake["source_id"],
                    intake["message_key"],
                ),
            )
            db.execute(
                "INSERT INTO plan(id, source_id, message_key, run_id, raw_path, raw_hash, rule_json, "
                "received_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    plan_id,
                    intake["source_id"],
                    intake["message_key"],
                    intake["run_id"],
                    str(raw_path),
                    raw_hash,
                    rule_json,
                    received_at,
                    now(),
                ),
            )
            db.execute("UPDATE intake SET status='accepted' WHERE id=?", (intake_id,))
            changed = db.execute(
                "UPDATE active_message SET kind='plan', ref_id=? "
                "WHERE source_id=? AND message_key=? AND kind='intake' AND ref_id=?",
                (plan_id, intake["source_id"], intake["message_key"], intake_id),
            ).rowcount
            if changed != 1:
                raise WorkspaceError("The intake's active message reference is damaged.")
            for target in rule.targets:
                db.execute(
                    "INSERT INTO plan_target(plan_id, target_id, path, save_mode, "
                    "attachments_in_destination, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                    (
                        plan_id,
                        target.id,
                        target.path,
                        target.save_mode.value,
                        int(target.attachments_in_destination),
                    ),
                )
        return plan_id

    def open_plans(self) -> list[sqlite3.Row]:
        with self.connection() as db:
            plans = db.execute(
                "SELECT * FROM plan WHERE status='open' ORDER BY created_at"
            ).fetchall()
            for plan in plans:
                self._validate_plan_snapshot(db, plan)
            return plans

    def automatic_work_due(self, at: datetime | None = None) -> bool:
        """Whether unfinished automatic work should wake the background runner."""
        current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.connection() as db:
            intakes = db.execute(
                "SELECT i.status, i.retry_after FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE r.kind='automatic' "
                "AND i.status IN ('reserved', 'error')"
            ).fetchall()
            for intake in intakes:
                if _intake_retry_due(str(intake["status"]), intake["retry_after"], current):
                    return True
            plans = db.execute(
                "SELECT id, error FROM plan WHERE status='open' ORDER BY created_at"
            ).fetchall()
            for plan in plans:
                outputs = db.execute(
                    "SELECT status, retry_after FROM output WHERE plan_id=?",
                    (plan["id"],),
                ).fetchall()
                if not outputs:
                    if plan["error"] is None:
                        return True
                    continue
                if all(output["status"] == "done" for output in outputs):
                    return True
                for output in outputs:
                    if output["status"] == "pending":
                        return True
                    if output["status"] == "error" and (
                        not output["retry_after"]
                        or datetime.fromisoformat(output["retry_after"]) <= current
                    ):
                        return True
        return False

    def work_plans(self) -> list[sqlite3.Row]:
        with self.connection() as db:
            plans = db.execute(
                "SELECT * FROM plan WHERE status IN ('open', 'paused') ORDER BY created_at"
            ).fetchall()
            for plan in plans:
                self._validate_plan_snapshot(db, plan)
            return plans

    def plan_targets(self, plan_id: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM plan_target WHERE plan_id=? ORDER BY rowid", (plan_id,)
            ).fetchall()

    def outputs(self, plan_id: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM output WHERE plan_id=? ORDER BY id", (plan_id,)
            ).fetchall()

    def reserved_output_path(self, path: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM output o JOIN plan p ON p.id=o.plan_id "
                    "WHERE o.final_path=? AND (o.status='done' OR p.status IN ('open', 'paused')) "
                    "LIMIT 1",
                    (path,),
                ).fetchone()
                is not None
            )

    def receipt(
        self, source_id: str, message_key: str, artifact_key: str, digest: str, requested_path: str
    ) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM receipt WHERE source_id=? AND message_key=? "
                "AND artifact_key=? AND digest=? AND requested_path=?",
                (source_id, message_key, artifact_key, digest, requested_path),
            ).fetchone()

    def add_output(
        self,
        plan_id: str,
        source_id: str,
        message_key: str,
        artifact_key: str,
        digest: str,
        requested_path: str,
        final_path: str,
        target_id: str,
        *,
        status: str = "pending",
    ) -> int:
        with self.connection() as db, db:
            db.execute(
                "INSERT OR IGNORE INTO output(plan_id, artifact_key, digest, requested_path, "
                "final_path, status) VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, artifact_key, digest, requested_path, final_path, status),
            )
            output = db.execute(
                "SELECT id FROM output WHERE plan_id=? AND artifact_key=? AND digest=? "
                "AND requested_path=?",
                (plan_id, artifact_key, digest, requested_path),
            ).fetchone()
            output_id = int(output["id"])
            db.execute(
                "INSERT OR IGNORE INTO output_target(output_id, plan_id, target_id) VALUES (?, ?, ?)",
                (output_id, plan_id, target_id),
            )
            return output_id

    def mark_target_no_output(self, plan_id: str, target_id: str) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE plan_target SET status='no_output', error=NULL "
                "WHERE plan_id=? AND target_id=?",
                (plan_id, target_id),
            )

    def refresh_target_statuses(self, plan_id: str) -> None:
        with self.connection() as db, db:
            targets = db.execute(
                "SELECT target_id, status FROM plan_target WHERE plan_id=?", (plan_id,)
            ).fetchall()
            for target in targets:
                outputs = db.execute(
                    "SELECT o.status, o.error FROM output o JOIN output_target t ON t.output_id=o.id "
                    "WHERE t.plan_id=? AND t.target_id=?",
                    (plan_id, target["target_id"]),
                ).fetchall()
                if not outputs:
                    continue
                errors = [str(row["error"]) for row in outputs if row["status"] == "error"]
                if errors:
                    status, error = "error", "; ".join(dict.fromkeys(errors))
                elif all(row["status"] == "done" for row in outputs):
                    status, error = "done", None
                else:
                    status, error = "pending", None
                db.execute(
                    "UPDATE plan_target SET status=?, error=? WHERE plan_id=? AND target_id=?",
                    (status, error, plan_id, target["target_id"]),
                )

    def set_output_path(self, output_id: int, final_path: str) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE output SET final_path=? WHERE id=? AND status!='done'",
                (final_path, output_id),
            )

    def output_done(self, output_id: int, plan: sqlite3.Row) -> None:
        with self.connection() as db, db:
            output = db.execute("SELECT * FROM output WHERE id=?", (output_id,)).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO receipt(source_id, message_key, artifact_key, digest, "
                "requested_path, final_path, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    plan["source_id"],
                    plan["message_key"],
                    output["artifact_key"],
                    output["digest"],
                    output["requested_path"],
                    output["final_path"],
                    now(),
                ),
            )
            db.execute(
                "UPDATE output SET status='done', error=NULL, retry_after=NULL WHERE id=?",
                (output_id,),
            )
        self.refresh_target_statuses(plan["id"])

    def output_error(self, output_id: int, error: str) -> None:
        with self.connection() as db, db:
            row = db.execute("SELECT attempts FROM output WHERE id=?", (output_id,)).fetchone()
            attempts = int(row[0]) + 1
            retry_after = _next_retry_after(attempts)
            db.execute(
                "UPDATE output SET status='error', error=?, attempts=?, retry_after=? WHERE id=?",
                (error, attempts, retry_after, output_id),
            )
            plan_id = str(
                db.execute("SELECT plan_id FROM output WHERE id=?", (output_id,)).fetchone()[0]
            )
        self.refresh_target_statuses(plan_id)

    def set_plan_error(self, plan_id: str, error: str | None) -> None:
        with self.plan_execution(plan_id), self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._active_plan(db, plan_id)
            if plan is not None:
                changed = db.execute(
                    "UPDATE plan SET error=? WHERE id=? AND status IN ('open', 'paused')",
                    (error, plan_id),
                ).rowcount
                if changed != 1:
                    raise WorkspaceError("The archive plan error could not be updated safely.")

    def finish_plan_if_complete(self, plan_id: str) -> bool:
        with self.plan_execution(plan_id):
            with self.connection() as db, db:
                db.execute("BEGIN IMMEDIATE")
                plan = self._active_plan(db, plan_id)
                if plan is None:
                    return False
                pending = db.execute(
                    "SELECT 1 FROM output WHERE plan_id=? AND status!='done' LIMIT 1", (plan_id,)
                ).fetchone()
                if pending:
                    return False
                completed = db.execute(
                    "UPDATE plan SET status='complete', error=NULL, finished_at=? WHERE id=?",
                    (now(), plan_id),
                ).rowcount
                if completed != 1:
                    raise WorkspaceError("The archive plan could not be completed safely.")
                db.execute(
                    "UPDATE source_message SET terminal_state='complete' "
                    "WHERE source_id=? AND message_key=?",
                    (plan["source_id"], plan["message_key"]),
                )
                deleted = db.execute(
                    "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                    "AND kind='plan' AND ref_id=?",
                    (plan["source_id"], plan["message_key"], plan_id),
                ).rowcount
                if deleted != 1:
                    raise WorkspaceError("The archive plan's active message reference is damaged.")
            self._remove_work_copy(Path(plan["raw_path"]))
            return True

    def pause_plan(self, plan_id: str) -> None:
        with self.plan_execution(plan_id), self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._active_plan(db, plan_id)
            if plan is not None and plan["status"] == "open":
                changed = db.execute(
                    "UPDATE plan SET status='paused' WHERE id=? AND status='open'", (plan_id,)
                ).rowcount
                if changed != 1:
                    raise WorkspaceError("The archive plan could not be paused safely.")

    def resume_plan(self, plan_id: str) -> None:
        with self.plan_execution(plan_id), self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._active_plan(db, plan_id)
            if plan is not None and plan["status"] == "paused":
                changed = db.execute(
                    "UPDATE plan SET status='open' WHERE id=? AND status='paused'", (plan_id,)
                ).rowcount
                if changed != 1:
                    raise WorkspaceError("The archive plan could not be resumed safely.")

    def abort_plan(self, plan_id: str) -> None:
        with self.plan_execution(plan_id):
            with self.connection() as db, db:
                db.execute("BEGIN IMMEDIATE")
                plan = self._active_plan(db, plan_id)
                if not plan:
                    return
                changed = db.execute(
                    "UPDATE plan SET status='aborted', finished_at=? WHERE id=?", (now(), plan_id)
                ).rowcount
                if changed != 1:
                    raise WorkspaceError("The archive plan could not be aborted safely.")
                db.execute(
                    "UPDATE source_message SET terminal_state='aborted' "
                    "WHERE source_id=? AND message_key=?",
                    (plan["source_id"], plan["message_key"]),
                )
                deleted = db.execute(
                    "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                    "AND kind='plan' AND ref_id=?",
                    (plan["source_id"], plan["message_key"], plan_id),
                ).rowcount
                if deleted != 1:
                    raise WorkspaceError("The archive plan's active message reference is damaged.")
            self._remove_work_copy(Path(plan["raw_path"]))

    def spool_usage(self) -> tuple[int, int]:
        self._retry_work_copy_cleanup()
        plans = self.work_plans()
        usage = 0
        with self._spool_directory_handle() as descriptor:
            if descriptor is None:
                entries = self.spool_dir.iterdir()
                for path in entries:
                    if path.suffix not in {".eml", ".tmp"}:
                        continue
                    try:
                        details = path.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode):
                        usage += details.st_size
                return len(plans), usage
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if Path(entry.name).suffix not in {".eml", ".tmp"}:
                        continue
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode):
                        usage += details.st_size
        return len(plans), usage

    def processing_history(
        self,
        limit: int = 100,
        *,
        before: tuple[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Return durable plan and nonaccepted intake history, newest first."""
        if limit < 1:
            raise ValueError("History page size must be positive.")
        before_time, before_key = before or (None, None)
        with self.connection() as db:
            rows = db.execute(
                """WITH history AS (
                  SELECT 'plan' AS item_type, 'plan:' || p.id AS history_key,
                    p.id, p.created_at AS occurred_at, p.source_id,
                    s.address, s.provider, m.subject, p.received_at,
                    m.received_origin,
                    json_extract(p.rule_json, '$.rule.name') AS rule_name,
                    p.rule_json, p.status, p.error, r.kind AS run_kind
                  FROM plan p JOIN source s ON s.id=p.source_id
                  JOIN scan_run r ON r.id=p.run_id
                  LEFT JOIN source_message m ON m.source_id=p.source_id
                    AND m.message_key=p.message_key
                  UNION ALL
                  SELECT 'intake' AS item_type, 'intake:' || i.id AS history_key,
                    i.id, i.created_at AS occurred_at, i.source_id,
                    s.address, s.provider, m.subject, m.received_at,
                    m.received_origin,
                    NULL AS rule_name, NULL AS rule_json, i.status, i.error,
                    r.kind AS run_kind
                  FROM intake i JOIN source s ON s.id=i.source_id
                  JOIN scan_run r ON r.id=i.run_id
                  LEFT JOIN source_message m ON m.source_id=i.source_id
                    AND m.message_key=i.message_key
                  WHERE i.status!='accepted'
                )
                SELECT * FROM history
                WHERE ? IS NULL OR occurred_at < ?
                  OR (occurred_at = ? AND history_key < ?)
                ORDER BY occurred_at DESC, history_key DESC LIMIT ?""",
                (before_time, before_time, before_time, before_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def target_outputs(self, plan_id: str, target_id: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT o.* FROM output o JOIN output_target t ON t.output_id=o.id "
                "WHERE t.plan_id=? AND t.target_id=? ORDER BY o.id",
                (plan_id, target_id),
            ).fetchall()


_SCHEMA = """
CREATE TABLE config_revision(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)));
CREATE TABLE source(id TEXT PRIMARY KEY, provider TEXT NOT NULL, mailbox_key TEXT NOT NULL,
  account_id TEXT NOT NULL, address TEXT NOT NULL, folders_json TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
  discovery_pending INTEGER NOT NULL DEFAULT 0 CHECK(discovery_pending IN (0, 1)),
  UNIQUE(provider, mailbox_key));
CREATE TABLE source_scope(source_id TEXT NOT NULL REFERENCES source(id), scope_key TEXT NOT NULL,
  processing_namespace TEXT, synchronization_namespace TEXT,
  baseline_done INTEGER NOT NULL DEFAULT 0 CHECK(baseline_done IN (0, 1)), cursor TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'active', 'paused')),
  error TEXT, PRIMARY KEY(source_id, scope_key));
CREATE TABLE source_message(source_id TEXT NOT NULL REFERENCES source(id), message_key TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, received_at TEXT, received_origin TEXT, sender_at TEXT, subject TEXT,
  terminal_state TEXT CHECK(terminal_state IS NULL OR terminal_state IN
    ('baseline', 'unmatched', 'complete', 'aborted', 'rejected')),
  PRIMARY KEY(source_id, message_key));
CREATE TABLE scan_run(id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id),
  kind TEXT NOT NULL CHECK(kind IN ('automatic', 'manual')), selection_json TEXT NOT NULL,
  config_revision INTEGER NOT NULL REFERENCES config_revision(id),
  settings_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'interrupted', 'cancelled')),
  started_at TEXT NOT NULL,
  finished_at TEXT, error TEXT, checkpoint TEXT,
  UNIQUE(id, source_id));
CREATE TABLE intake(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, message_key TEXT NOT NULL,
  run_id TEXT NOT NULL, scope_key TEXT NOT NULL, remote_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('reserved', 'error', 'accepted', 'unmatched', 'filtered', 'cancelled', 'rejected')),
  created_at TEXT NOT NULL,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), retry_after TEXT,
  FOREIGN KEY(run_id, source_id) REFERENCES scan_run(id, source_id),
  FOREIGN KEY(source_id, message_key) REFERENCES source_message(source_id, message_key));
CREATE TABLE plan(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, message_key TEXT NOT NULL,
  run_id TEXT NOT NULL, raw_path TEXT NOT NULL, raw_hash TEXT NOT NULL,
  rule_json TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open', 'paused', 'complete', 'aborted')),
  error TEXT, created_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY(run_id, source_id) REFERENCES scan_run(id, source_id),
  FOREIGN KEY(source_id, message_key) REFERENCES source_message(source_id, message_key));
CREATE TABLE plan_target(plan_id TEXT NOT NULL REFERENCES plan(id), target_id TEXT NOT NULL,
  path TEXT NOT NULL,
  save_mode TEXT NOT NULL CHECK(save_mode IN
    ('email_only', 'email_and_attachments', 'attachments_only')),
  attachments_in_destination INTEGER NOT NULL CHECK(attachments_in_destination IN (0, 1)),
  status TEXT NOT NULL CHECK(status IN ('pending', 'error', 'done', 'no_output')),
  error TEXT, PRIMARY KEY(plan_id, target_id));
CREATE TABLE active_message(source_id TEXT NOT NULL, message_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('intake', 'plan')), ref_id TEXT NOT NULL,
  PRIMARY KEY(source_id, message_key));
CREATE TABLE output(id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plan(id),
  artifact_key TEXT NOT NULL, digest TEXT NOT NULL, requested_path TEXT NOT NULL,
  final_path TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending', 'error', 'done')),
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  retry_after TEXT,
  UNIQUE(plan_id, artifact_key, digest, requested_path), UNIQUE(id, plan_id));
CREATE TABLE output_target(output_id INTEGER NOT NULL REFERENCES output(id),
  plan_id TEXT NOT NULL, target_id TEXT NOT NULL,
  PRIMARY KEY(output_id, target_id),
  FOREIGN KEY(output_id, plan_id) REFERENCES output(id, plan_id),
  FOREIGN KEY(plan_id, target_id) REFERENCES plan_target(plan_id, target_id));
CREATE TABLE receipt(source_id TEXT NOT NULL, message_key TEXT NOT NULL, artifact_key TEXT NOT NULL,
  digest TEXT NOT NULL, requested_path TEXT NOT NULL, final_path TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY(source_id, message_key, artifact_key, digest, requested_path));
CREATE TABLE activity_event(id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('info', 'success', 'warning', 'error')),
  message TEXT NOT NULL, account_id TEXT);
CREATE INDEX idx_plan_status ON plan(status);
CREATE INDEX idx_intake_run ON intake(run_id, status);
CREATE INDEX idx_activity_event_created_at ON activity_event(created_at DESC, id DESC);
CREATE UNIQUE INDEX idx_active_config_revision ON config_revision(active) WHERE active=1;
CREATE TRIGGER validate_active_message_insert BEFORE INSERT ON active_message
WHEN NOT (
  (NEW.kind='intake' AND EXISTS (
    SELECT 1 FROM intake WHERE id=NEW.ref_id AND source_id=NEW.source_id
      AND message_key=NEW.message_key AND status IN ('reserved', 'error')
  )) OR
  (NEW.kind='plan' AND EXISTS (
    SELECT 1 FROM plan WHERE id=NEW.ref_id AND source_id=NEW.source_id
      AND message_key=NEW.message_key AND status IN ('open', 'paused')
  ))
)
BEGIN
  SELECT RAISE(ABORT, 'active message reference is missing or inactive');
END;
CREATE TRIGGER validate_active_message_update BEFORE UPDATE OF
  source_id, message_key, kind, ref_id ON active_message
WHEN NOT (
  (NEW.kind='intake' AND EXISTS (
    SELECT 1 FROM intake WHERE id=NEW.ref_id AND source_id=NEW.source_id
      AND message_key=NEW.message_key AND status IN ('reserved', 'error')
  )) OR
  (NEW.kind='plan' AND EXISTS (
    SELECT 1 FROM plan WHERE id=NEW.ref_id AND source_id=NEW.source_id
      AND message_key=NEW.message_key AND status IN ('open', 'paused')
  ))
)
BEGIN
  SELECT RAISE(ABORT, 'active message reference is missing or inactive');
END;
CREATE TRIGGER protect_active_intake_delete BEFORE DELETE ON intake
WHEN EXISTS (SELECT 1 FROM active_message WHERE kind='intake' AND ref_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'cannot delete an active intake');
END;
CREATE TRIGGER protect_active_plan_delete BEFORE DELETE ON plan
WHEN EXISTS (SELECT 1 FROM active_message WHERE kind='plan' AND ref_id=OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'cannot delete an active plan');
END;
"""
