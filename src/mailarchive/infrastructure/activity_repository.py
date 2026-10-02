"""SQLite activity projections for real operations and mail outcomes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from mailarchive.application.activity import (
    ActivityDetail,
    ActivityItem,
    ActivityPage,
    MailResult,
    OperationAttempt,
    OutputAttempt,
    OutputResult,
    SourceResult,
)


class SqliteActivityRepository:
    def __init__(
        self, connection: Callable[[], AbstractContextManager[sqlite3.Connection]]
    ) -> None:
        self.connection = connection

    def current(self) -> tuple[ActivityItem, ...]:
        with self.connection() as db:
            db.execute("BEGIN")
            rows = db.execute(
                """SELECT 'operation:' || id AS key, created_at AS occurred_at
                   FROM manual_operation WHERE status IN ('queued', 'running', 'waiting', 'stopping')
                   UNION ALL
                   SELECT 'mail:' || p.id, p.created_at FROM plan p
                   JOIN scan_run r ON r.id=p.run_id
                   WHERE r.kind='automatic' AND p.status IN ('open', 'paused')
                   UNION ALL
                   SELECT 'mail:' || i.id, i.created_at FROM intake i
                   JOIN scan_run r ON r.id=i.run_id
                   WHERE r.kind='automatic' AND i.status IN ('reserved', 'error')
                   ORDER BY occurred_at DESC, key DESC"""
            ).fetchall()
            return tuple(self._item(db, row["key"]) for row in rows)

    def history(self, *, before: tuple[str, str] | None = None, limit: int = 100) -> ActivityPage:
        if limit < 1:
            raise ValueError("Activity page size must be positive.")
        before_time, before_key = before or (None, None)
        with self.connection() as db:
            db.execute("BEGIN")
            rows = db.execute(
                """WITH entries AS (
                     SELECT 'operation:' || id AS key, created_at AS occurred_at
                     FROM manual_operation
                     WHERE status IN ('completed', 'failed', 'stopped', 'interrupted')
                     UNION ALL
                     SELECT 'mail:' || p.id, p.created_at FROM plan p
                     JOIN scan_run r ON r.id=p.run_id
                     WHERE r.kind='automatic' AND p.status IN ('complete', 'aborted')
                     UNION ALL
                     SELECT 'mail:' || i.id, i.created_at FROM intake i
                     JOIN scan_run r ON r.id=i.run_id
                     WHERE r.kind='automatic' AND i.status IN
                       ('unmatched', 'filtered', 'cancelled', 'rejected')
                   )
                   SELECT key, occurred_at FROM entries
                   WHERE ? IS NULL OR occurred_at < ?
                     OR (occurred_at = ? AND key < ?)
                   ORDER BY occurred_at DESC, key DESC LIMIT ?""",
                (before_time, before_time, before_time, before_key, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            items = tuple(self._item(db, row["key"]) for row in visible)
        cursor = (
            (str(visible[-1]["occurred_at"]), str(visible[-1]["key"]))
            if len(rows) > limit and visible
            else None
        )
        return ActivityPage(items, cursor)

    def detail(self, key: str) -> ActivityDetail:
        with self.connection() as db:
            db.execute("BEGIN")
            item = self._item(db, key)
            if key.startswith("operation:"):
                operation_id = key.removeprefix("operation:")
                operation = db.execute(
                    "SELECT error FROM manual_operation WHERE id=?", (operation_id,)
                ).fetchone()
                source_rows = db.execute(
                    "SELECT mos.source_id, s.address, mos.status, mos.error "
                    "FROM manual_operation_source mos JOIN source s ON s.id=mos.source_id "
                    "WHERE mos.operation_id=? ORDER BY mos.position",
                    (operation_id,),
                ).fetchall()
                sources = tuple(
                    SourceResult(row["source_id"], row["address"], row["status"], row["error"])
                    for row in source_rows
                )
                attempt_rows = db.execute(
                    "SELECT * FROM manual_operation_attempt WHERE operation_id=? ORDER BY number",
                    (operation_id,),
                ).fetchall()
                attempts = tuple(self._operation_attempt(row) for row in attempt_rows)
                plan_rows = db.execute(
                    "SELECT p.id FROM plan p JOIN scan_run r ON r.id=p.run_id "
                    "WHERE r.operation_id=? ORDER BY p.created_at, p.id",
                    (operation_id,),
                ).fetchall()
                intake_rows = db.execute(
                    "SELECT i.id FROM intake i JOIN scan_run r ON r.id=i.run_id "
                    "WHERE r.operation_id=? AND i.status!='accepted' "
                    "ORDER BY i.created_at, i.id",
                    (operation_id,),
                ).fetchall()
                mail = tuple(
                    [self._plan_mail(db, row["id"]) for row in plan_rows]
                    + [self._intake_mail(db, row["id"]) for row in intake_rows]
                )
                error = operation["error"] if operation else None
            elif key.startswith("mail:"):
                mail_id = key.removeprefix("mail:")
                exists = db.execute("SELECT 1 FROM plan WHERE id=?", (mail_id,)).fetchone()
                mail = (self._plan_mail(db, mail_id) if exists else self._intake_mail(db, mail_id),)
                sources = ()
                attempts = ()
                error = mail[0].error
            else:
                raise ValueError("The selected activity key is invalid.")
            return ActivityDetail(item, mail, error, sources, attempts)

    @staticmethod
    def _operation_attempt(row) -> OperationAttempt:
        saved = json.loads(row["sources_json"] or "[]")
        sources = tuple(
            SourceResult(item["source_id"], item["address"], item["status"], item["error"])
            for item in saved
        )
        return OperationAttempt(
            row["number"],
            row["started_at"],
            row["finished_at"],
            row["status"],
            row["error"],
            sources,
        )

    def _item(self, db, key: str) -> ActivityItem:
        if key.startswith("operation:"):
            operation_id = key.removeprefix("operation:")
            row = db.execute(
                "SELECT * FROM manual_operation WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise ValueError("The selected activity is unavailable.")
            counts = db.execute(
                """SELECT
                     ((SELECT count(*) FROM plan p JOIN scan_run r ON r.id=p.run_id
                      WHERE r.operation_id=?) +
                     (SELECT count(*) FROM intake i JOIN scan_run r ON r.id=i.run_id
                      WHERE r.operation_id=? AND i.status!='accepted')) AS mail_count,
                     (SELECT count(*) FROM output o JOIN plan p ON p.id=o.plan_id
                      JOIN scan_run r ON r.id=p.run_id
                      WHERE r.operation_id=? AND o.status='done' AND EXISTS
                        (SELECT 1 FROM output_attempt a WHERE a.output_id=o.id
                         AND a.status='done')) AS done_count,
                     (SELECT count(*) FROM output o JOIN plan p ON p.id=o.plan_id
                      JOIN scan_run r ON r.id=p.run_id
                      WHERE r.operation_id=? AND o.status='done' AND NOT EXISTS
                        (SELECT 1 FROM output_attempt a WHERE a.output_id=o.id))
                        AS archived_count,
                     (SELECT count(*) FROM output o JOIN plan p ON p.id=o.plan_id
                      JOIN scan_run r ON r.id=p.run_id
                      WHERE r.operation_id=? AND o.status='error') AS error_count,
                     (SELECT count(*) FROM output o JOIN plan p ON p.id=o.plan_id
                      JOIN scan_run r ON r.id=p.run_id
                      WHERE r.operation_id=? AND o.status='pending') AS pending_count""",
                (operation_id,) * 6,
            ).fetchone()
            selected = db.execute(
                "SELECT count(*) FROM manual_operation_source WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0]
            summary = (
                f"Apply rule to past mail in {selected} mailbox(es): "
                f"{counts['done_count']} outputs saved, "
                f"{counts['archived_count']} previously archived, "
                f"{counts['error_count']} failed"
            )
            return ActivityItem(
                key,
                "operation",
                row["status"],
                row["created_at"],
                row["finished_at"],
                None,
                None,
                None,
                self._operation_rule_name(row),
                summary,
                can_stop=row["status"] in {"queued", "running", "waiting"},
                can_retry=row["status"] in {"failed", "interrupted", "waiting"},
                mail_count=counts["mail_count"],
                completed_outputs=counts["done_count"],
                previously_archived_outputs=counts["archived_count"],
                failed_outputs=counts["error_count"],
                pending_outputs=counts["pending_count"],
            )
        if not key.startswith("mail:"):
            raise ValueError("The selected activity key is invalid.")
        mail_id = key.removeprefix("mail:")
        plan = db.execute(
            "SELECT p.*, r.operation_id, s.address, m.subject FROM plan p "
            "JOIN scan_run r ON r.id=p.run_id "
            "JOIN source s ON s.id=p.source_id "
            "LEFT JOIN source_message m ON m.source_id=p.source_id "
            "AND m.message_key=p.message_key WHERE p.id=?",
            (mail_id,),
        ).fetchone()
        if plan:
            counts = db.execute(
                "SELECT sum(status='done' AND EXISTS "
                "(SELECT 1 FROM output_attempt a WHERE a.output_id=o.id "
                "AND a.status='done')) AS done_count, "
                "sum(status='done' AND NOT EXISTS "
                "(SELECT 1 FROM output_attempt a WHERE a.output_id=o.id)) AS archived_count, "
                "sum(status='error') AS error_count, "
                "sum(status='pending') AS pending_count FROM output o WHERE plan_id=?",
                (mail_id,),
            ).fetchone()
            done, archived, error, pending = (
                int(counts[name] or 0)
                for name in ("done_count", "archived_count", "error_count", "pending_count")
            )
            rule_name = json.loads(plan["rule_json"])["rule"]["name"]
            return ActivityItem(
                key,
                "mail",
                plan["status"],
                plan["created_at"],
                plan["finished_at"],
                plan["source_id"],
                plan["address"],
                plan["subject"],
                rule_name,
                f"{done} outputs saved, {archived} previously archived, "
                f"{error} failed, {pending} pending",
                can_retry=plan["operation_id"] is None
                and plan["status"] == "open"
                and (error + pending) > 0,
                mail_count=1,
                completed_outputs=done,
                previously_archived_outputs=archived,
                failed_outputs=error,
                pending_outputs=pending,
            )
        intake = db.execute(
            "SELECT i.*, s.address, m.subject FROM intake i "
            "JOIN source s ON s.id=i.source_id "
            "LEFT JOIN source_message m ON m.source_id=i.source_id "
            "AND m.message_key=i.message_key WHERE i.id=? AND i.status!='accepted'",
            (mail_id,),
        ).fetchone()
        if intake is None:
            raise ValueError("The selected activity is unavailable.")
        return ActivityItem(
            key,
            "mail",
            intake["status"],
            intake["created_at"],
            None,
            intake["source_id"],
            intake["address"],
            intake["subject"],
            None,
            intake["error"] or intake["status"],
            mail_count=1,
        )

    @staticmethod
    def _operation_rule_name(row) -> str | None:
        settings = json.loads(row["settings_json"])
        return next(
            (
                rule["name"]
                for rule in settings.get("rules", ())
                if rule.get("id") == row["rule_id"]
            ),
            None,
        )

    def _plan_mail(self, db, plan_id: str) -> MailResult:
        row = db.execute(
            "SELECT p.*, s.address, m.subject FROM plan p "
            "JOIN source s ON s.id=p.source_id "
            "LEFT JOIN source_message m ON m.source_id=p.source_id "
            "AND m.message_key=p.message_key WHERE p.id=?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise ValueError("The selected mail result is unavailable.")
        outputs = db.execute(
            "SELECT o.*, rec.completed_at FROM output o "
            "LEFT JOIN receipt rec ON rec.source_id=? AND rec.message_key=? "
            "AND rec.artifact_key=o.artifact_key AND rec.digest=o.digest "
            "AND rec.requested_path=o.requested_path WHERE o.plan_id=? ORDER BY o.id",
            (row["source_id"], row["message_key"], plan_id),
        ).fetchall()
        return MailResult(
            "mail:" + plan_id,
            row["source_id"],
            row["address"],
            row["subject"],
            row["status"],
            row["received_at"],
            json.loads(row["rule_json"])["rule"]["name"],
            row["error"],
            tuple(self._output(db, output) for output in outputs),
        )

    def _intake_mail(self, db, intake_id: str) -> MailResult:
        row = db.execute(
            "SELECT i.*, s.address, m.subject, m.received_at FROM intake i "
            "JOIN source s ON s.id=i.source_id "
            "LEFT JOIN source_message m ON m.source_id=i.source_id "
            "AND m.message_key=i.message_key WHERE i.id=?",
            (intake_id,),
        ).fetchone()
        if row is None:
            raise ValueError("The selected mail result is unavailable.")
        return MailResult(
            "mail:" + intake_id,
            row["source_id"],
            row["address"],
            row["subject"],
            row["status"],
            row["received_at"],
            None,
            row["error"],
        )

    @staticmethod
    def _output(db, row) -> OutputResult:
        attempt_rows = db.execute(
            "SELECT * FROM output_attempt WHERE output_id=? ORDER BY number", (row["id"],)
        ).fetchall()
        attempts = tuple(
            OutputAttempt(
                attempt["number"],
                attempt["status"],
                attempt["final_path"],
                attempt["started_at"],
                attempt["finished_at"],
                attempt["error"],
            )
            for attempt in attempt_rows
        )
        final_path = str(Path(row["final_path"])) if row["final_path"] else ""
        return OutputResult(
            row["id"],
            row["status"],
            final_path,
            row["requested_path"],
            row["error"],
            row["completed_at"],
            attempts,
            previously_archived=row["status"] == "done" and not attempts,
        )
