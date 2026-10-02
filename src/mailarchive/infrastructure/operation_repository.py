"""Durable manual-operation and per-mailbox scan state in SQLite."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import uuid4

from mailarchive.application.errors import RunNotActiveError, WorkspaceError
from mailarchive.domain.configuration import Settings
from mailarchive.infrastructure import profile_integrity as integrity
from mailarchive.infrastructure.configuration_repository import ConfigurationRepository
from mailarchive.infrastructure.persistence_time import now


class OperationRepository:
    """Own a frozen manual selection and each child scan in one transaction domain."""

    def __init__(
        self,
        connection: Callable[[], AbstractContextManager[sqlite3.Connection]],
        configuration: ConfigurationRepository,
    ) -> None:
        self.connection = connection
        self.configuration = configuration

    def create_manual_operation(
        self, settings: Settings, source_ids: list[str], rule_id: str, selection: dict
    ) -> str:
        """Persist a frozen whole-selection operation before worker dispatch."""
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError("Select one or more distinct mailboxes.")
        revision = self.configuration.prepare_run_settings(settings)
        snapshot = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
        operation_id = str(uuid4())
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT payload FROM config_revision WHERE id=?", (revision,)
            ).fetchone()
            if current is None or current["payload"] != snapshot:
                raise WorkspaceError("The operation configuration revision does not match.")
            db.execute(
                "INSERT INTO manual_operation(id, rule_id, selection_json, config_revision, "
                "settings_json, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (
                    operation_id,
                    rule_id,
                    json.dumps(selection, sort_keys=True),
                    revision,
                    snapshot,
                    now(),
                ),
            )
            for position, source_id in enumerate(source_ids):
                db.execute(
                    "INSERT INTO manual_operation_source(operation_id, source_id, position, status) "
                    "VALUES (?, ?, ?, 'queued')",
                    (operation_id, source_id, position),
                )
        return operation_id

    def manual_operation(self, operation_id: str) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM manual_operation WHERE id=?", (operation_id,)
            ).fetchone()

    def manual_operation_sources(self, operation_id: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM manual_operation_source WHERE operation_id=? ORDER BY position",
                (operation_id,),
            ).fetchall()

    def claim_manual_operation(self, operation_id: str) -> bool:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE manual_operation SET status='running', started_at=coalesce(started_at, ?), "
                "finished_at=NULL, error=NULL WHERE id=? AND status IN "
                "('queued', 'interrupted', 'failed', 'waiting')",
                (now(), operation_id),
            ).rowcount
            if changed:
                number = int(
                    db.execute(
                        "SELECT coalesce(max(number), 0) + 1 FROM manual_operation_attempt "
                        "WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()[0]
                )
                db.execute(
                    "INSERT INTO manual_operation_attempt(operation_id, number, started_at, status) "
                    "VALUES (?, ?, ?, 'running')",
                    (operation_id, number, now()),
                )
            return changed == 1

    @staticmethod
    def _operation_sources_json(db: sqlite3.Connection, operation_id: str) -> str:
        rows = db.execute(
            "SELECT mos.source_id, s.address, mos.status, mos.error "
            "FROM manual_operation_source mos JOIN source s ON s.id=mos.source_id "
            "WHERE mos.operation_id=? ORDER BY mos.position",
            (operation_id,),
        ).fetchall()
        return json.dumps([dict(row) for row in rows], sort_keys=True)

    @classmethod
    def _finish_operation_attempt(
        cls, db: sqlite3.Connection, operation_id: str, status: str, error: str | None
    ) -> None:
        attempt = db.execute(
            "SELECT id FROM manual_operation_attempt WHERE operation_id=? "
            "AND status='running' ORDER BY number DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if attempt is not None:
            db.execute(
                "UPDATE manual_operation_attempt SET status=?, error=?, finished_at=?, "
                "sources_json=? WHERE id=?",
                (
                    status,
                    error,
                    now(),
                    cls._operation_sources_json(db, operation_id),
                    attempt["id"],
                ),
            )

    def recover(self, db: sqlite3.Connection) -> None:
        """Settle interrupted selections and attempts in the caller's startup transaction."""
        db.execute("UPDATE scan_run SET status='interrupted' WHERE status='running'")
        interrupted = db.execute(
            "SELECT id FROM manual_operation WHERE status IN ('queued', 'running')"
        ).fetchall()
        stopping = db.execute("SELECT id FROM manual_operation WHERE status='stopping'").fetchall()
        for row in interrupted:
            operation_id = row["id"]
            db.execute(
                "UPDATE manual_operation_source SET status='failed', error=? "
                "WHERE operation_id=? AND status='running'",
                ("Interrupted while scanning this mailbox.", operation_id),
            )
            error = "The operation was interrupted before it finished."
            self._finish_operation_attempt(db, operation_id, "interrupted", error)
            db.execute(
                "UPDATE manual_operation SET status='interrupted', error=? WHERE id=?",
                (error, operation_id),
            )
        for row in stopping:
            operation_id = row["id"]
            db.execute(
                "UPDATE manual_operation_source SET status='stopped' "
                "WHERE operation_id=? AND status IN ('queued', 'running')",
                (operation_id,),
            )
            self._finish_operation_attempt(db, operation_id, "stopped", "Stopped by the user.")
            db.execute(
                "UPDATE manual_operation SET status='stopped', finished_at=? WHERE id=?",
                (now(), operation_id),
            )
        db.execute(
            "UPDATE plan SET status='paused' WHERE status='open' AND run_id IN "
            "(SELECT id FROM scan_run WHERE operation_id IN "
            "(SELECT id FROM manual_operation WHERE status='stopped'))"
        )
        db.execute(
            "UPDATE manual_operation SET status=CASE WHEN error IS NULL THEN 'completed' "
            "ELSE 'failed' END, finished_at=? WHERE status='waiting' AND NOT EXISTS "
            "(SELECT 1 FROM plan p JOIN scan_run r ON r.id=p.run_id "
            "WHERE r.operation_id=manual_operation.id AND p.status IN ('open', 'paused'))",
            (now(),),
        )

    def interrupt_queued_manual_operations(self) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE manual_operation SET status='interrupted', error=? WHERE status='queued'",
                ("The application closed before the operation started.",),
            )

    def manual_operation_accepts_work(self, operation_id: str) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT status FROM manual_operation WHERE id=?", (operation_id,)
            ).fetchone()
        return bool(row and row["status"] == "running")

    def manual_operation_accepts_outputs(self, operation_id: str) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT status FROM manual_operation WHERE id=?", (operation_id,)
            ).fetchone()
        return bool(row and row["status"] in {"running", "waiting"})

    def mark_operation_source(
        self, operation_id: str, source_id: str, status: str, error: str | None = None
    ) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE manual_operation_source SET status=?, error=? "
                "WHERE operation_id=? AND source_id=?",
                (status, error, operation_id, source_id),
            )

    def request_stop_manual_operation(self, operation_id: str) -> bool:
        """Gate new work promptly; the worker settles any in-flight publication."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE manual_operation SET status='stopping' WHERE id=? "
                "AND status IN ('queued', 'running', 'waiting', 'failed', 'interrupted')",
                (operation_id,),
            ).rowcount
            if not changed:
                return False
            db.execute(
                "UPDATE scan_run SET status='cancelled', finished_at=? "
                "WHERE operation_id=? AND status IN ('running', 'failed', 'interrupted')",
                (now(), operation_id),
            )
            rows = db.execute(
                "SELECT i.id, i.source_id, i.message_key FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE r.operation_id=? "
                "AND i.status IN ('reserved', 'error')",
                (operation_id,),
            ).fetchall()
            for row in rows:
                db.execute("UPDATE intake SET status='cancelled' WHERE id=?", (row["id"],))
                db.execute(
                    "DELETE FROM active_message WHERE source_id=? AND message_key=? "
                    "AND kind='intake' AND ref_id=?",
                    (row["source_id"], row["message_key"], row["id"]),
                )
            return True

    def finalize_stop_manual_operation(self, operation_id: str) -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE plan SET status='paused' WHERE status='open' AND run_id IN "
                "(SELECT id FROM scan_run WHERE operation_id=?)",
                (operation_id,),
            )
            db.execute(
                "UPDATE manual_operation_source SET status='stopped' "
                "WHERE operation_id=? AND status IN ('queued', 'running')",
                (operation_id,),
            )
            db.execute(
                "UPDATE manual_operation SET status='stopped', finished_at=? "
                "WHERE id=? AND status='stopping'",
                (now(), operation_id),
            )

            self._finish_operation_attempt(db, operation_id, "stopped", "Stopped by the user.")

    def finish_manual_operation(self, operation_id: str, error: str | None = None) -> None:
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM manual_operation WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise WorkspaceError("The manual operation is unavailable.")
            if row["status"] == "stopping":
                return
            if row["status"] != "running":
                return
            failed_sources = db.execute(
                "SELECT source_id, error FROM manual_operation_source WHERE operation_id=? "
                "AND status='failed' ORDER BY position",
                (operation_id,),
            ).fetchall()
            incomplete = db.execute(
                "SELECT count(*) FROM manual_operation_source WHERE operation_id=? "
                "AND status NOT IN ('completed', 'failed')",
                (operation_id,),
            ).fetchone()[0]
            reasons = [
                str(row["error"] or f"Mailbox {row['source_id']} scan failed")
                for row in failed_sources
            ]
            if incomplete:
                reasons.append(f"{incomplete} selected mailbox scan(s) did not complete.")
            if error:
                reasons.insert(0, error)
            error = "; ".join(dict.fromkeys(reasons)) or None
            waiting = (
                db.execute(
                    "SELECT 1 FROM plan p JOIN scan_run r ON r.id=p.run_id "
                    "WHERE r.operation_id=? AND p.status IN ('open', 'paused') LIMIT 1",
                    (operation_id,),
                ).fetchone()
                is not None
            )
            status = "waiting" if waiting else ("failed" if error else "completed")
            db.execute(
                "UPDATE manual_operation SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, None if waiting else now(), operation_id),
            )
            self._finish_operation_attempt(
                db, operation_id, "failed" if error else "completed", error
            )

    def reconcile_manual_operation_for_plan(self, plan_id: str) -> None:
        """Move a waiting selection to history after its last accepted output settles."""
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.operation_id, m.error FROM plan p JOIN scan_run r ON r.id=p.run_id "
                "JOIN manual_operation m ON m.id=r.operation_id "
                "WHERE p.id=? AND m.status='waiting'",
                (plan_id,),
            ).fetchone()
            if row is None:
                return
            unfinished = db.execute(
                "SELECT 1 FROM plan p JOIN scan_run r ON r.id=p.run_id "
                "WHERE r.operation_id=? AND p.status IN ('open', 'paused') LIMIT 1",
                (row["operation_id"],),
            ).fetchone()
            if unfinished is None:
                db.execute(
                    "UPDATE manual_operation SET status=?, finished_at=? "
                    "WHERE id=? AND status='waiting'",
                    ("failed" if row["error"] else "completed", now(), row["operation_id"]),
                )

    def start_run(
        self,
        source_id: str,
        kind: str,
        selection: dict,
        settings: Settings,
        config_revision: int,
        operation_id: str | None = None,
    ) -> str:
        run_id = str(uuid4())
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if operation_id is not None:
                operation = db.execute(
                    "SELECT status FROM manual_operation WHERE id=?", (operation_id,)
                ).fetchone()
                if kind != "manual" or operation is None or operation["status"] != "running":
                    raise RunNotActiveError("The manual operation is no longer active.")
            snapshot = json.dumps(settings.to_dict(), ensure_ascii=False, sort_keys=True)
            revision = db.execute(
                "SELECT payload FROM config_revision WHERE id=?", (config_revision,)
            ).fetchone()
            if revision is None or revision["payload"] != snapshot:
                raise WorkspaceError("The run configuration revision does not match its snapshot.")
            db.execute(
                "INSERT INTO scan_run(id, source_id, kind, selection_json, config_revision, "
                "settings_json, operation_id, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    run_id,
                    source_id,
                    kind,
                    json.dumps(selection),
                    config_revision,
                    snapshot,
                    operation_id,
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
            checkpoint = integrity.validate_run_checkpoint(run)
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
                expected = integrity.range_scope_keys(run)
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
            owner = db.execute("SELECT operation_id FROM scan_run WHERE id=?", (run_id,)).fetchone()
            if owner is not None and owner["operation_id"] is not None:
                raise WorkspaceError("Stop the past-mail operation to cancel its scan.")
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

    def run_operation_id(self, run_id: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT operation_id FROM scan_run WHERE id=?", (run_id,)).fetchone()
        return str(row["operation_id"]) if row and row["operation_id"] else None

    def manual_run_for_source(self, operation_id: str, source_id: str) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM scan_run WHERE operation_id=? AND source_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (operation_id, source_id),
            ).fetchone()

    def manual_open_plans(self, operation_id: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            plans = db.execute(
                "SELECT p.* FROM plan p JOIN scan_run r ON r.id=p.run_id "
                "WHERE r.operation_id=? AND p.status='open' ORDER BY p.created_at",
                (operation_id,),
            ).fetchall()
            for plan in plans:
                integrity.validate_plan_snapshot(db, plan)
            return plans

    def manual_output_failure_count(self, operation_id: str) -> int:
        with self.connection() as db:
            return int(
                db.execute(
                    "SELECT count(*) FROM output o JOIN plan p ON p.id=o.plan_id "
                    "JOIN scan_run r ON r.id=p.run_id WHERE r.operation_id=? "
                    "AND o.status IN ('pending', 'error')",
                    (operation_id,),
                ).fetchone()[0]
            )

    def interrupt_run(self, run_id: str) -> None:
        with self.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET status='interrupted', finished_at=?, error=? "
                "WHERE id=? AND status='running'",
                (now(), "Mail processing was interrupted.", run_id),
            )

    def plan_operation_active(self, plan_id: str, *, explicit: bool = False) -> bool:
        """Only a running or waiting manual operation may publish an output."""
        with self.connection() as db:
            row = db.execute(
                "SELECT r.operation_id, m.status FROM plan p "
                "JOIN scan_run r ON r.id=p.run_id "
                "LEFT JOIN manual_operation m ON m.id=r.operation_id WHERE p.id=?",
                (plan_id,),
            ).fetchone()
        return bool(
            row and (row["operation_id"] is None or row["status"] in {"running", "waiting"})
        )

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
            integrity.validate_run_checkpoint(run)
            return integrity.run_settings(run)

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
        expected = integrity.range_scope_keys(row)
        if scope_key not in expected:
            raise WorkspaceError("The archive run checkpoint is damaged.")
        checkpoint = integrity.validate_run_checkpoint(row)
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
                or scope_key not in integrity.range_scope_keys(row)
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
            checkpoint = integrity.checkpoint_data(row["checkpoint"])
            targets = checkpoint.setdefault("range_targets", {})
            if not isinstance(targets, dict):
                raise WorkspaceError("The archive run checkpoint is damaged.")
            targets[scope_key] = {
                "namespace": namespace,
                "token": token,
                "complete": complete,
            }
            integrity.validate_run_checkpoint(
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
