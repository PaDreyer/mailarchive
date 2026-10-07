"""Verify exclusive ownership before opening or cleaning a profile's work files."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mailarchive.application.errors import WorkspaceError
from mailarchive.application.intake_limits import MAX_MESSAGE_BYTES
from mailarchive.infrastructure.output_files import LocalOutputFiles
from mailarchive.infrastructure.profile_integrity import validate_runtime_references
from mailarchive.infrastructure.sqlite_schema import (
    APPLICATION_ID,
    DATABASE_SCHEMA_VERSION,
    validate_schema,
)

_LOCK_NAME = ".mailarchive-work.lock"
_OPEN_LOCK = threading.RLock()
_SQLITE_HEADER = b"SQLite format 3\0"


def _archive_digests(database_path: Path) -> dict[str, list[tuple[Path, str]]]:
    """Read provenance only from a verified profile, without changing its data."""
    if not database_path.exists():
        return {}
    try:
        db = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if (
                db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or db.execute("PRAGMA user_version").fetchone()[0] != DATABASE_SCHEMA_VERSION
            ):
                return {}
            validate_schema(db, error_type=WorkspaceError)
            validate_runtime_references(db)
            rows = db.execute(
                "SELECT final_path, digest FROM receipt "
                "UNION SELECT final_path, digest FROM output WHERE final_path!=''"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.OperationalError:
        # Keep temporary access failures visible to the profile port; they are
        # not evidence that an archived sibling is a competing database.
        raise
    except (sqlite3.DatabaseError, WorkspaceError):
        return {}
    digests: dict[str, list[tuple[Path, str]]] = {}
    for row in rows:
        path = Path(row["final_path"])
        if path.is_absolute():
            digests.setdefault(os.path.normcase(path.name), []).append((path, row["digest"]))
    return digests


def _is_published_archive(
    path: Path, size: int, digests: dict[str, list[tuple[Path, str]]]
) -> bool:
    if size > MAX_MESSAGE_BYTES or path.is_symlink():
        return False
    output_files = LocalOutputFiles()
    candidates = digests.get(os.path.normcase(path.name), ())
    # Prefer exact paths, then resolve only possible aliases for this sibling's
    # name. Unrelated archive destinations need no filesystem access at startup.
    for saved, digest in sorted(candidates, key=lambda item: item[0] != path):
        try:
            if saved.parent.resolve() == path.parent and output_files.matches(path, digest, size):
                return True
        except (OSError, RuntimeError):
            continue
    return False


@contextmanager
def exclusive_profile_directory(database_path: Path) -> Iterator[None]:
    """Serialize ownership discovery and cleanup, including independent processes."""
    with _OPEN_LOCK:
        lock_path = database_path.parent / _LOCK_NAME
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            with os.fdopen(descriptor, "r+b") as handle:
                if lock_path.is_symlink() or not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise WorkspaceError("The profile work ownership lock is unsafe.")
                if os.name == "nt":
                    import msvcrt

                    if handle.seek(0, os.SEEK_END) == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    verify_work_owner(database_path)
                    yield
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise WorkspaceError("The profile work ownership could not be verified.") from exc


def verify_work_owner(database_path: Path) -> None:
    """Inspect contents rather than suffixes; unknown SQLite siblings are ambiguous."""
    digests = None
    for candidate in database_path.parent.iterdir():
        if candidate.name == _LOCK_NAME or candidate.resolve() == database_path:
            continue
        # A symlink to another directory's database uses that directory's work
        # files after canonical path resolution, and cannot own this directory.
        if candidate.is_symlink() and candidate.resolve().parent != database_path.parent:
            continue
        details = candidate.stat()
        if not stat.S_ISREG(details.st_mode):
            continue
        with candidate.open("rb") as handle:
            header = handle.read(100)
            if header.startswith(_SQLITE_HEADER) or (
                len(header) >= 72 and int.from_bytes(header[68:72], "big") == APPLICATION_ID
            ):
                if digests is None:
                    digests = _archive_digests(database_path)
                if _is_published_archive(candidate, details.st_size, digests):
                    continue
                raise WorkspaceError(
                    "Choose a different folder for each database's work files. "
                    "Another SQLite database already occupies this profile folder."
                )
    work = database_path.parent / "work"
    if work.is_symlink():
        raise WorkspaceError("The MailArchive work directory is not a safe directory.")
    if not database_path.exists() and work.exists() and (not work.is_dir() or any(work.iterdir())):
        raise WorkspaceError("The new database folder already contains mail work files.")
