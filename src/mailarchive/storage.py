from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from mailarchive.models import ParsedMail, Rule, SaveMode


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_filename(value: str, fallback: str = "File", max_length: int = 100) -> str:
    cleaned = _INVALID_FILENAME.sub("_", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:max_length].rstrip(" .") or fallback


def destination_path(root: Path, destination: str) -> Path:
    if destination.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", destination):
        raise ValueError("The destination folder must be inside the archive folder.")
    normalized = destination.replace("\\", "/").strip(" /")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts:
        raise ValueError("The destination folder cannot be empty.")
    if any(part == ".." for part in parts):
        raise ValueError("The destination folder must be inside the archive folder.")
    candidate = root.joinpath(*(safe_filename(part, "Folder") for part in parts))
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError("The destination folder must be inside the archive folder.")
    return candidate


def _mail_datetime(mail: ParsedMail) -> datetime:
    if mail.date_header:
        try:
            parsed = parsedate_to_datetime(mail.date_header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone()
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now().astimezone()


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="archiv-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@dataclass(slots=True)
class ArchiveResult:
    files: list[Path]
    destination: Path


class ArchiveStorage:
    def __init__(self, archive_root: Path) -> None:
        self.archive_root = archive_root

    def archive(self, mail: ParsedMail, rule: Rule) -> ArchiveResult:
        target = destination_path(self.archive_root, rule.destination)
        target.mkdir(parents=True, exist_ok=True)
        timestamp = _mail_datetime(mail).strftime("%Y-%m-%d_%H-%M-%S")
        subject = safe_filename(mail.subject, "no subject", 80)
        digest = hashlib.sha256(mail.raw).hexdigest()[:10]
        base_name = f"{timestamp}_{subject}_{digest}"
        written: list[Path] = []

        if rule.save_mode in {SaveMode.EMAIL_ONLY, SaveMode.EMAIL_AND_ATTACHMENTS}:
            eml_path = target / f"{base_name}.eml"
            _atomic_write(eml_path, mail.raw)
            written.append(eml_path)

        if rule.save_mode in {SaveMode.ATTACHMENTS_ONLY, SaveMode.EMAIL_AND_ATTACHMENTS}:
            if mail.attachments:
                attachment_dir = target / f"{base_name}_Attachments"
                attachment_dir.mkdir(parents=True, exist_ok=True)
                used_names: set[str] = set()
                for index, attachment in enumerate(mail.attachments, start=1):
                    original = safe_filename(attachment.filename, f"Attachment-{index}", 120)
                    candidate = original
                    suffix = Path(original).suffix
                    stem = Path(original).stem
                    counter = 2
                    while candidate.casefold() in used_names:
                        candidate = f"{stem}-{counter}{suffix}"
                        counter += 1
                    used_names.add(candidate.casefold())
                    attachment_path = attachment_dir / candidate
                    _atomic_write(attachment_path, attachment.content)
                    written.append(attachment_path)

        return ArchiveResult(files=written, destination=target)


class ArchiveState:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_message (
                        account_id TEXT NOT NULL,
                        source_namespace TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        archived_at TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        rule_name TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        files_json TEXT NOT NULL,
                        PRIMARY KEY (account_id, source_namespace, message_id)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_processed_message_archived_at "
                    "ON processed_message(archived_at DESC)"
                )
                legacy_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'processed_mail'"
                ).fetchone()
                if legacy_table:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO processed_message (
                            account_id, source_namespace, message_id, archived_at,
                            subject, rule_name, destination, files_json
                        )
                        SELECT account_id, 'imap:' || uid_validity, uid, archived_at,
                               subject, rule_name, destination, files_json
                        FROM processed_mail
                        """
                    )

    def was_processed(self, account_id: str, source_namespace: str, message_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_message "
                "WHERE account_id = ? AND source_namespace = ? AND message_id = ?",
                (account_id, source_namespace, message_id),
            ).fetchone()
        return row is not None

    def processed_message_ids(self, account_id: str, source_namespace: str) -> set[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT message_id FROM processed_message "
                "WHERE account_id = ? AND source_namespace = ?",
                (account_id, source_namespace),
            ).fetchall()
        return {str(row["message_id"]) for row in rows}

    def record(
        self,
        account_id: str,
        source_namespace: str,
        message_id: str,
        mail: ParsedMail,
        rule: Rule,
        result: ArchiveResult,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO processed_message (
                        account_id, source_namespace, message_id, archived_at, subject,
                        rule_name, destination, files_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        source_namespace,
                        message_id,
                        datetime.now(timezone.utc).isoformat(),
                        mail.subject,
                        rule.name,
                        str(result.destination),
                        json.dumps([str(path) for path in result.files], ensure_ascii=False),
                    ),
                )

    def recent(self, limit: int = 100) -> list[dict[str, str]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT archived_at, subject, rule_name, destination
                FROM processed_message ORDER BY archived_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
