"""Intake invariants shared by transactions that transfer ownership to delivery."""

import sqlite3

from mailarchive.application.errors import RunNotActiveError, WorkspaceError


def require_active_intake(db: sqlite3.Connection, intake_id: str) -> sqlite3.Row:
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
