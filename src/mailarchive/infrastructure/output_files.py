"""Exclusive atomic publication of a complete archive artifact."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

from mailarchive.domain.archive_paths import MAX_FILENAME_BYTES


class LocalOutputFiles:
    """Probe and publish destination files without following a saved path symlink."""

    def filename_limit(self, directory: Path) -> int:
        if not hasattr(os, "pathconf"):
            return MAX_FILENAME_BYTES
        for candidate in (directory, *directory.parents):
            try:
                limit = os.pathconf(candidate, "PC_NAME_MAX")
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (ValueError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in {errno.EINVAL, errno.ENOSYS}:
                    raise
                return MAX_FILENAME_BYTES
            return min(limit, MAX_FILENAME_BYTES) if limit > 0 else MAX_FILENAME_BYTES
        return MAX_FILENAME_BYTES

    def occupied(self, path: Path) -> bool:
        try:
            path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True

    def matches(self, path: Path, expected_digest: str, size: int) -> bool:
        """Recognize an already-published pending output using a bounded read."""
        if size < 0:
            raise ValueError("The expected output size cannot be negative.")
        try:
            before = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            return False
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return False
            raise
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_size != size
                or actual.st_dev != before.st_dev
                or actual.st_ino != before.st_ino
            ):
                return False
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                return False
            return digest.hexdigest() == expected_digest

    def publish(self, path: Path, content: bytes) -> None:
        _atomic_write(path, content)


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
