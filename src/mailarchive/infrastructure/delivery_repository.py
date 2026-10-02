"""Durable accepted-mail plans, output attempts, receipts, and delivery transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from mailarchive.application.errors import WorkspaceError
from mailarchive.domain.configuration import Rule, Settings
from mailarchive.infrastructure import profile_integrity as integrity
from mailarchive.infrastructure.intake_transactions import require_active_intake
from mailarchive.infrastructure.operation_repository import OperationRepository
from mailarchive.infrastructure.persistence_time import intake_retry_due as _intake_retry_due
from mailarchive.infrastructure.persistence_time import next_retry_after as _next_retry_after
from mailarchive.infrastructure.persistence_time import now
from mailarchive.infrastructure.spool import LocalSpool, SpoolError


class DeliveryRepository:
    """Own accepted mail and per-target publication state; spool owns raw files."""

    def __init__(
        self,
        connection: Callable[[], AbstractContextManager[sqlite3.Connection]],
        spool: LocalSpool,
        plan_execution: Callable[[str], AbstractContextManager[None]],
        operations: OperationRepository,
    ) -> None:
        self.connection = connection
        self.spool = spool
        self.plan_execution = plan_execution
        self.operations = operations

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

    @staticmethod
    def _require_independent_plan(db: sqlite3.Connection, plan: sqlite3.Row) -> None:
        owner = db.execute(
            "SELECT operation_id FROM scan_run WHERE id=?", (plan["run_id"],)
        ).fetchone()
        if owner is None:
            raise WorkspaceError("The archive plan's run is unavailable.")
        if owner["operation_id"] is not None:
            raise WorkspaceError("Manage manual mail through its past-mail operation.")

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
            Settings(rules=[rule], archive_timezone=snapshot["timezone"]).validate()
            if snapshot != {"rule": rule.to_dict(), "timezone": snapshot["timezone"]}:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError("The archive plan snapshot is damaged.") from exc
        with self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            require_active_intake(db, intake_id)
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
                integrity.validate_plan_snapshot(db, plan)
            return plans

    def automatic_work_sources(self) -> list[sqlite3.Row]:
        """Sources and frozen settings of work eligible for the polling worker."""
        with self.connection() as db:
            return db.execute(
                "SELECT DISTINCT i.source_id AS source_id, r.settings_json, r.config_revision FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE r.kind='automatic' "
                "AND i.status IN ('reserved', 'error') UNION "
                "SELECT DISTINCT p.source_id, r.settings_json, r.config_revision FROM plan p "
                "JOIN scan_run r ON r.id=p.run_id "
                "LEFT JOIN manual_operation m ON m.id=r.operation_id "
                "WHERE p.status='open' AND (r.kind='automatic' OR m.status='waiting') "
                "ORDER BY config_revision DESC, source_id"
            ).fetchall()

    def auto_resumable_plans(
        self, *, excluded_source_ids: frozenset[str] = frozenset()
    ) -> list[sqlite3.Row]:
        """Retry accepted work except explicitly stopped or interrupted selections."""
        with self.connection() as db:
            plans = db.execute(
                "SELECT p.* FROM plan p JOIN scan_run r ON r.id=p.run_id "
                "LEFT JOIN manual_operation m ON m.id=r.operation_id "
                "WHERE p.status='open' AND "
                "(r.kind='automatic' OR m.status='waiting') "
                "ORDER BY p.created_at"
            ).fetchall()
            plans = [plan for plan in plans if plan["source_id"] not in excluded_source_ids]
            for plan in plans:
                integrity.validate_plan_snapshot(db, plan)
            return plans

    def automatic_work_due(
        self, at: datetime | None = None, *, excluded_source_ids: frozenset[str] = frozenset()
    ) -> bool:
        """Whether unfinished automatic work should wake the background runner."""
        current = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.connection() as db:
            intakes = db.execute(
                "SELECT i.source_id, i.status, i.retry_after FROM intake i "
                "JOIN scan_run r ON r.id=i.run_id WHERE r.kind='automatic' "
                "AND i.status IN ('reserved', 'error')"
            ).fetchall()
            for intake in intakes:
                if intake["source_id"] in excluded_source_ids:
                    continue
                if _intake_retry_due(str(intake["status"]), intake["retry_after"], current):
                    return True
            plans = db.execute(
                "SELECT p.id, p.source_id, p.error FROM plan p JOIN scan_run r ON r.id=p.run_id "
                "LEFT JOIN manual_operation m ON m.id=r.operation_id "
                "WHERE p.status='open' AND "
                "(r.kind='automatic' OR m.status='waiting') "
                "ORDER BY p.created_at"
            ).fetchall()
            for plan in plans:
                if plan["source_id"] in excluded_source_ids:
                    continue
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
                integrity.validate_plan_snapshot(db, plan)
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

    def output_done(
        self, output_id: int, plan: sqlite3.Row, *, started_at: str | None = None
    ) -> None:
        with self.connection() as db, db:
            output = db.execute("SELECT * FROM output WHERE id=?", (output_id,)).fetchone()
            if output is None or output["status"] == "done":
                return
            attempt_number = int(output["attempts"]) + 1
            db.execute(
                "INSERT INTO output_attempt(output_id, number, status, final_path, started_at, "
                "finished_at) VALUES (?, ?, 'done', ?, ?, ?)",
                (output_id, attempt_number, output["final_path"], started_at or now(), now()),
            )
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
                "UPDATE output SET status='done', error=NULL, retry_after=NULL, attempts=? "
                "WHERE id=?",
                (attempt_number, output_id),
            )
        self.refresh_target_statuses(plan["id"])

    def output_error(self, output_id: int, error: str, *, started_at: str | None = None) -> None:
        with self.connection() as db, db:
            row = db.execute("SELECT * FROM output WHERE id=?", (output_id,)).fetchone()
            if row is None or row["status"] == "done":
                return
            attempts = int(row["attempts"]) + 1
            retry_after = _next_retry_after(attempts)
            db.execute(
                "INSERT INTO output_attempt(output_id, number, status, final_path, started_at, "
                "finished_at, error) VALUES (?, ?, 'error', ?, ?, ?, ?)",
                (output_id, attempts, row["final_path"], started_at or now(), now(), error),
            )
            db.execute(
                "UPDATE output SET status='error', error=?, attempts=?, retry_after=? WHERE id=?",
                (error, attempts, retry_after, output_id),
            )
            plan_id = str(
                db.execute("SELECT plan_id FROM output WHERE id=?", (output_id,)).fetchone()[0]
            )
        self.refresh_target_statuses(plan_id)

    def output_attempts(self, output_id: int) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM output_attempt WHERE output_id=? ORDER BY number", (output_id,)
            ).fetchall()

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

    def record_plan_failure(self, plan_id: str, error: str) -> None:
        """Keep a failed raw-work resume visible on its plan and intake together."""
        with self.plan_execution(plan_id), self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._active_plan(db, plan_id)
            if plan is None:
                return
            db.execute("UPDATE plan SET error=? WHERE id=?", (error, plan_id))
            db.execute(
                "UPDATE intake SET error=? WHERE run_id=? AND source_id=? AND message_key=?",
                (error, plan["run_id"], plan["source_id"], plan["message_key"]),
            )

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
            self.spool.discard(Path(plan["raw_path"]))
            self.operations.reconcile_manual_operation_for_plan(plan_id)
            return True

    def pause_plan(self, plan_id: str) -> None:
        with self.plan_execution(plan_id), self.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._active_plan(db, plan_id)
            if plan is not None and plan["status"] == "open":
                self._require_independent_plan(db, plan)
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
                self._require_independent_plan(db, plan)
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
                self._require_independent_plan(db, plan)
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
            self.spool.discard(Path(plan["raw_path"]))

    def spool_usage(self) -> tuple[int, int]:
        try:
            return len(self.work_plans()), self.spool.usage_bytes()
        except SpoolError as exc:
            raise WorkspaceError(str(exc)) from exc

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
