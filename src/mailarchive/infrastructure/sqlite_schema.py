"""Versioned SQLite schema and structural validation, independent of execution."""

import sqlite3

APPLICATION_ID = 0x4D415243  # MARC


DATABASE_SCHEMA_VERSION = 1


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
        "last_checked_at",
        "last_error",
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
        "operation_id",
    },
    "manual_operation": {
        "id",
        "rule_id",
        "selection_json",
        "config_revision",
        "settings_json",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error",
    },
    "manual_operation_source": {
        "operation_id",
        "source_id",
        "position",
        "status",
        "error",
    },
    "manual_operation_attempt": {
        "id",
        "operation_id",
        "number",
        "started_at",
        "finished_at",
        "status",
        "error",
        "sources_json",
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
    "output_attempt": {
        "id",
        "output_id",
        "number",
        "status",
        "final_path",
        "started_at",
        "finished_at",
        "error",
    },
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
    "manual_operation": ("id",),
    "manual_operation_source": ("operation_id", "source_id"),
    "manual_operation_attempt": ("id",),
    "intake": ("id",),
    "plan": ("id",),
    "plan_target": ("plan_id", "target_id"),
    "active_message": ("source_id", "message_key"),
    "output": ("id",),
    "output_target": ("output_id", "target_id"),
    "output_attempt": ("id",),
    "receipt": ("source_id", "message_key", "artifact_key", "digest", "requested_path"),
    "activity_event": ("id",),
}


_REQUIRED_INDEXES = {
    "idx_plan_status": ("plan", False, False, ("status",)),
    "idx_intake_run": ("intake", False, False, ("run_id", "status")),
    "idx_scan_run_operation": ("scan_run", False, False, ("operation_id",)),
    "idx_manual_operation_attempt": (
        "manual_operation_attempt",
        False,
        False,
        ("operation_id", "number"),
    ),
    "idx_output_attempt_output": ("output_attempt", False, False, ("output_id", "number")),
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
    "manual_operation_source": {("operation_id", "position")},
    "manual_operation_attempt": {("operation_id", "number")},
    "output_attempt": {("output_id", "number")},
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
        (("operation_id", "manual_operation", "id"),),
    },
    "manual_operation": {(("config_revision", "config_revision", "id"),)},
    "manual_operation_source": {
        (("operation_id", "manual_operation", "id"),),
        (("source_id", "source", "id"),),
    },
    "manual_operation_attempt": {(("operation_id", "manual_operation", "id"),)},
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
    "output_attempt": {(("output_id", "output", "id"),)},
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
    "manual_operation": {
        "check(statusin('queued','running','waiting','stopping','stopped','completed','failed','interrupted'))"
    },
    "manual_operation_source": {
        "check(statusin('queued','running','stopped','completed','failed'))"
    },
    "manual_operation_attempt": {
        "check(statusin('running','completed','failed','stopped','interrupted'))",
        "check(number>=1)",
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
    "output_attempt": {"check(statusin('done','error'))", "check(number>=1)"},
    "activity_event": {"check(levelin('info','success','warning','error'))"},
}


_REQUIRED_TRIGGERS = {
    "validate_active_message_insert",
    "validate_active_message_update",
    "protect_active_intake_delete",
    "protect_active_plan_delete",
}


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
  error TEXT, last_checked_at TEXT, last_error TEXT, PRIMARY KEY(source_id, scope_key));
CREATE TABLE source_message(source_id TEXT NOT NULL REFERENCES source(id), message_key TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, received_at TEXT, received_origin TEXT, sender_at TEXT, subject TEXT,
  terminal_state TEXT CHECK(terminal_state IS NULL OR terminal_state IN
    ('baseline', 'unmatched', 'complete', 'aborted', 'rejected')),
  PRIMARY KEY(source_id, message_key));
CREATE TABLE manual_operation(id TEXT PRIMARY KEY, rule_id TEXT NOT NULL,
  selection_json TEXT NOT NULL, config_revision INTEGER NOT NULL REFERENCES config_revision(id),
  settings_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('queued', 'running', 'waiting', 'stopping', 'stopped', 'completed', 'failed', 'interrupted')),
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT);
CREATE TABLE manual_operation_source(operation_id TEXT NOT NULL REFERENCES manual_operation(id),
  source_id TEXT NOT NULL REFERENCES source(id), position INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'stopped', 'completed', 'failed')),
  error TEXT, PRIMARY KEY(operation_id, source_id), UNIQUE(operation_id, position));
CREATE TABLE manual_operation_attempt(id INTEGER PRIMARY KEY,
  operation_id TEXT NOT NULL REFERENCES manual_operation(id),
  number INTEGER NOT NULL CHECK(number >= 1), started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'stopped', 'interrupted')),
  error TEXT, sources_json TEXT, UNIQUE(operation_id, number));
CREATE TABLE scan_run(id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id),
  kind TEXT NOT NULL CHECK(kind IN ('automatic', 'manual')), selection_json TEXT NOT NULL,
  config_revision INTEGER NOT NULL REFERENCES config_revision(id),
  settings_json TEXT NOT NULL, operation_id TEXT REFERENCES manual_operation(id),
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
CREATE TABLE output_attempt(id INTEGER PRIMARY KEY, output_id INTEGER NOT NULL REFERENCES output(id),
  number INTEGER NOT NULL CHECK(number >= 1),
  status TEXT NOT NULL CHECK(status IN ('done', 'error')),
  final_path TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  error TEXT, UNIQUE(output_id, number));
CREATE TABLE receipt(source_id TEXT NOT NULL, message_key TEXT NOT NULL, artifact_key TEXT NOT NULL,
  digest TEXT NOT NULL, requested_path TEXT NOT NULL, final_path TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY(source_id, message_key, artifact_key, digest, requested_path));
CREATE TABLE activity_event(id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('info', 'success', 'warning', 'error')),
  message TEXT NOT NULL, account_id TEXT);
CREATE INDEX idx_plan_status ON plan(status);
CREATE INDEX idx_intake_run ON intake(run_id, status);
CREATE INDEX idx_scan_run_operation ON scan_run(operation_id);
CREATE INDEX idx_manual_operation_attempt ON manual_operation_attempt(operation_id, number);
CREATE INDEX idx_output_attempt_output ON output_attempt(output_id, number);
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


def validate_schema(db: sqlite3.Connection, *, error_type: type[RuntimeError]) -> None:
    found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not _REQUIRED_SCHEMA_COLUMNS.keys() <= found:
        raise error_type("MailArchive profile database is incomplete.")

    for table, required_columns in _REQUIRED_SCHEMA_COLUMNS.items():
        table_info = db.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row[1] for row in table_info}
        primary_key = tuple(row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5])
        if not required_columns <= columns or primary_key != _REQUIRED_PRIMARY_KEYS[table]:
            raise error_type("MailArchive profile database is incomplete.")

    for name, (table, unique, partial, columns) in _REQUIRED_INDEXES.items():
        indexes = {row[1]: row for row in db.execute(f"PRAGMA index_list({table})")}
        index = indexes.get(name)
        sql_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        normalized_sql = "".join(str(sql_row[0]).lower().split()) if sql_row and sql_row[0] else ""
        predicate = normalized_sql.partition("where")[2] or None
        if (
            index is None
            or bool(index[2]) != unique
            or bool(index[4]) != partial
            or tuple(row[2] for row in db.execute(f"PRAGMA index_info({name})")) != columns
            or predicate != _REQUIRED_INDEX_PREDICATES.get(name)
        ):
            raise error_type("MailArchive profile database is incomplete.")

    for table, required_keys in _REQUIRED_UNIQUE_KEYS.items():
        unique_keys = {
            tuple(row[2] for row in db.execute(f"PRAGMA index_info({index[1]})"))
            for index in db.execute(f"PRAGMA index_list({table})")
            if index[2]
        }
        if not required_keys <= unique_keys:
            raise error_type("MailArchive profile database is incomplete.")

    for table, required_keys in _REQUIRED_FOREIGN_KEYS.items():
        groups: dict[int, list[tuple[int, str, str, str]]] = {}
        for row in db.execute(f"PRAGMA foreign_key_list({table})"):
            groups.setdefault(row[0], []).append((row[1], row[3], row[2], row[4]))
        foreign_keys = {
            tuple(
                (source, target_table, target) for _, source, target_table, target in sorted(group)
            )
            for group in groups.values()
        }
        if not required_keys <= foreign_keys:
            raise error_type("MailArchive profile database is incomplete.")

    for table, required_checks in _REQUIRED_CHECKS.items():
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        normalized_sql = "".join(str(row[0]).lower().split()) if row and row[0] else ""
        if any(required_check not in normalized_sql for required_check in required_checks):
            raise error_type("MailArchive profile database is incomplete.")

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
            for row in canonical.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
            if row[0] in _REQUIRED_TRIGGERS and row[1]
        }
    finally:
        canonical.close()
    if required_triggers.keys() != _REQUIRED_TRIGGERS or any(
        actual_triggers.get(name) != sql for name, sql in required_triggers.items()
    ):
        raise error_type("MailArchive profile database is incomplete.")
