"""Resolve legacy IMAP INBOX spellings without rewriting stored identities."""

import sqlite3

from mailarchive.domain.configuration import MailProvider
from mailarchive.domain.source_identity import folder_scope_key, is_imap_inbox


def scope_condition(
    db: sqlite3.Connection, source_id: str, scope_key: str, *, prefix: str = ""
) -> tuple[str, tuple[str, ...]]:
    source = db.execute("SELECT provider FROM source WHERE id=?", (source_id,)).fetchone()
    if (
        source
        and source["provider"] == MailProvider.GENERIC_IMAP.value
        and is_imap_inbox(scope_key)
    ):
        return f"{prefix}source_id=? AND lower({prefix}scope_key)='inbox'", (source_id,)
    return f"{prefix}source_id=? AND {prefix}scope_key=?", (source_id, scope_key)


def stored_scope_keys(db: sqlite3.Connection, source_id: str, scope_key: str) -> list[str]:
    condition, parameters = scope_condition(db, source_id, scope_key)
    rows = db.execute(
        "SELECT scope_key FROM source_scope WHERE " + condition, parameters
    ).fetchall()
    if rows:
        return [str(row["scope_key"]) for row in rows]
    source = db.execute("SELECT provider FROM source WHERE id=?", (source_id,)).fetchone()
    return [folder_scope_key(MailProvider(source["provider"]), scope_key) if source else scope_key]
