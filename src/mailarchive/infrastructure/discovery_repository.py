"""Source discovery progress and bounded intake admission in SQLite."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from uuid import uuid4

from mailarchive.application.errors import RunNotActiveError, WorkspaceError
from mailarchive.application.intake_limits import MAX_ACTIVE_INTAKES, IntakeQueueCapacityError
from mailarchive.domain.configuration import MailProvider, Settings
from mailarchive.infrastructure import profile_integrity as integrity
from mailarchive.infrastructure.intake_transactions import require_active_intake
from mailarchive.infrastructure.persistence_time import intake_retry_due as _intake_retry_due
from mailarchive.infrastructure.persistence_time import next_retry_after as _next_retry_after
from mailarchive.infrastructure.persistence_time import now


class DiscoveryRepository:
    def __init__(
        self, connection: Callable[[], AbstractContextManager[sqlite3.Connection]]
    ) -> None:
        self.connection = connection

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
        self,
        *,
        due_only: bool = False,
        at: datetime | None = None,
        excluded_source_ids: frozenset[str] = frozenset(),
    ) -> list[sqlite3.Row]:
        """Return unfinished automatic intakes with their immutable run snapshot."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT i.*, r.settings_json FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE r.kind='automatic' "
                "AND i.status IN ('reserved', 'error') ORDER BY i.created_at"
            ).fetchall()
        rows = [row for row in rows if row["source_id"] not in excluded_source_ids]
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
                "SELECT i.source_id, i.message_key, r.operation_id FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE i.id=? "
                "AND i.status IN ('reserved', 'error')",
                (intake_id,),
            ).fetchone()
            if row is None:
                return
            if row["operation_id"] is not None:
                raise WorkspaceError("Stop the past-mail operation to cancel its intake.")
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

    def intake_operation_id(self, intake_id: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT r.operation_id FROM intake i JOIN scan_run r ON r.id=i.run_id WHERE i.id=?",
                (intake_id,),
            ).fetchone()
        return str(row["operation_id"]) if row and row["operation_id"] else None

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
                synchronization_namespace, baseline_done, cursor, status, last_checked_at)
                VALUES (?, ?, ?, ?, 1, ?, 'active', ?)
                ON CONFLICT(source_id, scope_key) DO UPDATE SET
                  processing_namespace=excluded.processing_namespace,
                  synchronization_namespace=excluded.synchronization_namespace,
                  baseline_done=1, cursor=excluded.cursor, status='active', error=NULL,
                  last_checked_at=excluded.last_checked_at, last_error=NULL""",
                (
                    source_id,
                    scope_key,
                    processing_namespace,
                    synchronization_namespace,
                    cursor,
                    now(),
                ),
            )

    def pause_scope(self, source_id: str, scope_key: str, error: str) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE source_scope SET status='paused', error=?, last_error=? "
                "WHERE source_id=? AND scope_key=?",
                (error, error, source_id, scope_key),
            )

    def record_scope_check_error(self, source_id: str, scope_key: str, error: str) -> None:
        """Record source health even if discovery produced no mail outcome."""
        with self.connection() as db, db:
            db.execute(
                "INSERT INTO source_scope(source_id, scope_key, last_checked_at, last_error) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(source_id, scope_key) DO UPDATE SET "
                "last_checked_at=excluded.last_checked_at, last_error=excluded.last_error",
                (source_id, scope_key, now(), error),
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
            row = require_active_intake(db, intake_id)
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
            row = require_active_intake(db, intake_id)
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
                "SELECT r.status, r.checkpoint, r.operation_id, m.status AS operation_status "
                "FROM scan_run r LEFT JOIN manual_operation m ON m.id=r.operation_id "
                "WHERE r.id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or run["status"] != "running"
                or (run["operation_id"] is not None and run["operation_status"] != "running")
            ):
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
            checkpoint = integrity.checkpoint_data(run["checkpoint"])
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
            row = require_active_intake(db, intake_id)
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
                row = require_active_intake(db, intake_id)
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
            row = require_active_intake(db, intake_id)
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
