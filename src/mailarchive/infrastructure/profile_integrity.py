"""Stored configuration, source references and processing snapshot invariants."""

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from mailarchive.application.errors import WorkspaceError
from mailarchive.domain.configuration import Account, Mailbox, MailProvider, Rule, Settings
from mailarchive.domain.source_identity import mailbox_namespace, source_key


def validate_runtime_references(db: sqlite3.Connection) -> None:
    """Reject relational damage not representable by SQLite foreign keys."""

    validate_config_revision_state(db)
    validate_active_sources(db)
    validate_manual_operations(db)

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
        validate_run_checkpoint(run)
    validate_plan_snapshots(db)


def validate_config_revision_state(db: sqlite3.Connection) -> None:
    revision = db.execute(
        "SELECT count(*) AS total, COALESCE(sum(active), 0) AS active FROM config_revision"
    ).fetchone()
    runtime_state = db.execute(
        """SELECT EXISTS(SELECT 1 FROM source)
        OR EXISTS(SELECT 1 FROM source_scope)
        OR EXISTS(SELECT 1 FROM source_message)
        OR EXISTS(SELECT 1 FROM scan_run)
        OR EXISTS(SELECT 1 FROM manual_operation)
        OR EXISTS(SELECT 1 FROM manual_operation_source)
        OR EXISTS(SELECT 1 FROM manual_operation_attempt)
        OR EXISTS(SELECT 1 FROM intake)
        OR EXISTS(SELECT 1 FROM plan)
        OR EXISTS(SELECT 1 FROM plan_target)
        OR EXISTS(SELECT 1 FROM active_message)
        OR EXISTS(SELECT 1 FROM output)
        OR EXISTS(SELECT 1 FROM output_target)
        OR EXISTS(SELECT 1 FROM output_attempt)
        OR EXISTS(SELECT 1 FROM receipt)
        OR EXISTS(SELECT 1 FROM activity_event)"""
    ).fetchone()[0]
    if (revision["total"] and revision["active"] != 1) or (not revision["total"] and runtime_state):
        raise WorkspaceError("MailArchive profile database failed its integrity check.")
    for row in db.execute("SELECT payload FROM config_revision"):
        settings_from_payload(
            row["payload"], "MailArchive profile database failed its integrity check."
        )


def validate_manual_operations(db: sqlite3.Connection) -> None:
    """A queued selection and its past attempts retain their exact source ownership."""
    rows = db.execute(
        "SELECT m.*, c.payload AS revision_payload FROM manual_operation m "
        "JOIN config_revision c ON c.id=m.config_revision"
    ).fetchall()
    for operation in rows:
        try:
            settings = settings_from_payload(
                operation["settings_json"], "Invalid operation snapshot"
            )
            if operation["settings_json"] != operation["revision_payload"]:
                raise ValueError
            selection = json.loads(operation["selection_json"])
            source_ids = selection["source_ids"]
            owners = {m.id: (a, m) for a in settings.accounts for m in a.mailboxes}
            sources = db.execute(
                "SELECT source_id, position FROM manual_operation_source "
                "WHERE operation_id=? ORDER BY position",
                (operation["id"],),
            ).fetchall()
            if (
                not isinstance(source_ids, list)
                or not source_ids
                or len(set(source_ids)) != len(source_ids)
                or [row["source_id"] for row in sources] != source_ids
                or [row["position"] for row in sources] != list(range(len(source_ids)))
                or not set(source_ids) <= owners.keys()
            ):
                raise ValueError
            selected_rule = next((r for r in settings.rules if r.id == operation["rule_id"]), None)
            if operation["rule_id"] and (selected_rule is None or not selected_rule.enabled):
                raise ValueError
            for source_id in source_ids:
                account, mailbox = owners[source_id]
                if (
                    not account.enabled
                    or not mailbox.enabled
                    or (
                        selected_rule
                        and selected_rule.account_ids is not None
                        and account.id not in selected_rule.account_ids
                    )
                ):
                    raise ValueError
            ZoneInfo(selection["timezone"])
            dates = [
                datetime.fromisoformat(selection[key]) if selection[key] else None
                for key in ("start_utc", "end_utc")
            ]
            if any(value is not None and value.tzinfo is None for value in dates):
                raise ValueError
            if dates[0] is not None and dates[1] is not None and dates[0] >= dates[1]:
                raise ValueError
            _validate_operation_attempts(db, operation, source_ids)
        except (KeyError, TypeError, ValueError, WorkspaceError) as exc:
            raise WorkspaceError("The past-mail operation snapshot is damaged.") from exc


def _validate_operation_attempts(db, operation, source_ids: list[str]) -> None:
    attempts = db.execute(
        "SELECT * FROM manual_operation_attempt WHERE operation_id=? ORDER BY number",
        (operation["id"],),
    ).fetchall()
    if [row["number"] for row in attempts] != list(range(1, len(attempts) + 1)):
        raise ValueError
    running = [row for row in attempts if row["status"] == "running"]
    if len(running) > 1 or (running and operation["status"] not in {"running", "stopping"}):
        raise ValueError
    for attempt in attempts:
        snapshot = json.loads(attempt["sources_json"] or "[]")
        if not isinstance(snapshot, list):
            raise ValueError
        if attempt["status"] == "running":
            if attempt["finished_at"] is not None or snapshot:
                raise ValueError
            continue
        if not attempt["finished_at"] or [s["source_id"] for s in snapshot] != source_ids:
            raise ValueError
        for source in snapshot:
            if (
                source["status"] not in {"queued", "running", "stopped", "completed", "failed"}
                or not isinstance(source["address"], str)
                or (source["error"] is not None and not isinstance(source["error"], str))
            ):
                raise ValueError


def settings_from_payload(value: object, error: str) -> Settings:
    try:
        if not isinstance(value, str):
            raise ValueError
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError
        # Pre-feature profiles and immutable run snapshots have no pause flag.
        payload.setdefault("automatic_monitoring_paused", False)
        settings = Settings.from_dict(payload)
        if payload != settings.to_dict():
            raise ValueError
        return settings
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkspaceError(error) from exc


def validate_active_sources(db: sqlite3.Connection) -> None:
    revision = db.execute("SELECT payload FROM config_revision WHERE active=1 LIMIT 1").fetchone()
    if revision is None:
        return
    settings = settings_from_payload(
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


def run_settings(run: sqlite3.Row | dict) -> Settings:
    settings = settings_from_payload(run["settings_json"], "The archive run snapshot is damaged.")
    keys = set(run.keys())
    if "revision_payload" in keys:
        settings_from_payload(run["revision_payload"], "The archive run snapshot is damaged.")
        if run["settings_json"] != run["revision_payload"]:
            raise WorkspaceError("The archive run snapshot is damaged.")
    return settings


def range_context(run: sqlite3.Row | dict) -> tuple[Account, Mailbox, list[str]]:
    try:
        selection = json.loads(run["selection_json"])
        settings = run_settings(run)
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


def range_scope_keys(run: sqlite3.Row | dict) -> set[str]:
    account, _, folders = range_context(run)
    return {"gmail-mailbox"} if account.provider == MailProvider.GMAIL_API else set(folders)


def range_namespace_matches(
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


def validate_run_checkpoint(run: sqlite3.Row | dict) -> dict:
    run_settings(run)
    checkpoint = checkpoint_data(run["checkpoint"])
    targets = checkpoint.get("range_targets", {})
    if not isinstance(targets, dict) or (targets and run["kind"] != "manual"):
        raise WorkspaceError("The archive run checkpoint is damaged.")
    context = range_context(run) if run["kind"] == "manual" else None
    expected = (
        ({"gmail-mailbox"} if context[0].provider == MailProvider.GMAIL_API else set(context[2]))
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
                and not range_namespace_matches(context[0], context[1], scope_key, namespace)
            )
        ):
            raise WorkspaceError("The archive run checkpoint is damaged.")
    return checkpoint


def validate_plan_snapshot(db: sqlite3.Connection, plan: sqlite3.Row) -> None:
    try:
        snapshot = json.loads(plan["rule_json"])
        if not isinstance(snapshot, dict) or set(snapshot) != {"rule", "timezone"}:
            raise ValueError
        if not isinstance(snapshot["rule"], dict) or not isinstance(snapshot["timezone"], str):
            raise ValueError
        rule = Rule.from_dict(snapshot["rule"])
        Settings(rules=[rule], archive_timezone=snapshot["timezone"]).validate()
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


def validate_plan_snapshots(db: sqlite3.Connection) -> None:
    for plan in db.execute("SELECT id, rule_json FROM plan"):
        validate_plan_snapshot(db, plan)


def checkpoint_data(value: str | None) -> dict:
    if not value:
        return {}
    try:
        checkpoint = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("The archive run checkpoint is damaged.") from exc
    if not isinstance(checkpoint, dict):
        raise WorkspaceError("The archive run checkpoint is damaged.")
    return checkpoint
