from __future__ import annotations

import ctypes
import errno
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from mailarchive.models import DateFolderPosition
from mailarchive.workspace import WorkspaceStore

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


def destination_path(
    root: Path,
    destination: str,
    date_folder_position: DateFolderPosition = DateFolderPosition.NONE,
    *,
    mail_date: datetime | None = None,
) -> Path:
    """Resolve a full user path without changing its components.

    Double braces escape literal braces. A missing date keeps placeholder text for
    previews; accepted plans always pass the provider reception date.
    """
    from string import Formatter

    if not destination or destination != destination.strip():
        raise ValueError("Enter a full destination path without outer whitespace.")
    values = {
        "year": f"{mail_date.year:04d}" if mail_date else "YYYY",
        "month": f"{mail_date.month:02d}" if mail_date else "MM",
    }
    try:
        for _, field, spec, conversion in Formatter().parse(destination):
            if field not in {None, "year", "month"} or spec or conversion:
                raise ValueError("Use only {year}, {month}, {{ or }} in destination paths.")
        expanded = destination.format(**values)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid destination template: {exc}") from exc
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise ValueError("Enter a full destination path.")
    return candidate


def _publish_file(source: Path, destination: Path) -> None:
    """Atomically publish a complete file without replacing an existing name."""
    if os.name == "nt":
        # On Windows, os.rename refuses an existing destination (unlike POSIX).
        os.rename(source, destination)
        return
    if sys.platform == "linux":
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            # AT_FDCWD=-100, RENAME_NOREPLACE=1. Also supports filesystems such
            # as FAT that cannot publish a file using a hard link.
            if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
                return
            error = ctypes.get_errno()
            if error not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise OSError(error, os.strerror(error), destination)
    # Older kernels/libcs and other POSIX systems: link is atomic and exclusive.
    # If neither operation is supported, fail safely instead of reserving an
    # empty destination or using a rename that could overwrite an existing file.
    os.link(source, destination)


def _atomic_write(path: Path, content: bytes) -> None:
    """The final filename is visible only once all content has been written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="archiv-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_file(temporary_path, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


ArchiveState = WorkspaceStore
