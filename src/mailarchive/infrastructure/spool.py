"""Safe retained raw-message storage, independent of database records."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from mailarchive.application.intake_limits import (
    DISK_RESERVE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_SPOOL_BYTES,
    MessageTooLargeError,
    SpoolCapacityError,
)

DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
_CLEANUP_LOCK = threading.Lock()
_PENDING_CLEANUP: dict[str, set[Path]] = {}


class SpoolError(RuntimeError):
    """The retained work directory or a requested raw message is unsafe or unavailable."""


class LocalSpool:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.mkdir(mode=0o700, exist_ok=True)
        self._validate_directory()
        with _CLEANUP_LOCK:
            self._pending_cleanup = _PENDING_CLEANUP.setdefault(str(self.path.absolute()), set())

    def _validate_directory(self) -> os.stat_result:
        try:
            details = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise SpoolError("The MailArchive work directory is unavailable.") from exc
        if not stat.S_ISDIR(details.st_mode):
            raise SpoolError("The MailArchive work directory is not a safe directory.")
        return details

    @contextmanager
    def directory_handle(self) -> Iterator[int | None]:
        """Pin the validated directory against path replacement during an operation."""
        expected = self._validate_directory()
        if not hasattr(os, "O_DIRECTORY"):
            yield None
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise SpoolError("The MailArchive work directory is unavailable.") from exc
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(actual.st_mode)
                or actual.st_dev != expected.st_dev
                or actual.st_ino != expected.st_ino
            ):
                raise SpoolError("The MailArchive work directory changed unexpectedly.")
            yield descriptor
        finally:
            os.close(descriptor)

    def read(self, path: Path, max_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
        path = Path(path)
        if path.parent != self.path or not path.name:
            raise SpoolError("The local working copy path is outside the work directory.")
        with self.directory_handle() as descriptor:
            if descriptor is None:
                try:
                    details = path.stat(follow_symlinks=False)
                except OSError as exc:
                    raise SpoolError("The local working copy is unavailable.") from exc
                if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
                    raise SpoolError("The local working copy is invalid or too large.")
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise SpoolError("The local working copy is unavailable.") from exc
                if len(content) > max_bytes:
                    raise SpoolError("The local working copy is invalid or too large.")
                return content
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                file_descriptor = os.open(path.name, flags, dir_fd=descriptor)
            except OSError as exc:
                raise SpoolError("The local working copy is unavailable.") from exc
            try:
                details = os.fstat(file_descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
                    raise SpoolError("The local working copy is invalid or too large.")
                with os.fdopen(file_descriptor, "rb") as handle:
                    file_descriptor = -1
                    content = handle.read(max_bytes + 1)
                    if len(content) > max_bytes:
                        raise SpoolError("The local working copy is invalid or too large.")
                    return content
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)

    def _write(
        self, descriptor: int, chunks: Iterable[bytes], usage: int, parent_fd: int | None
    ) -> str:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise RuntimeError("The mail provider returned invalid MIME chunks.")
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_MESSAGE_BYTES:
                    raise MessageTooLargeError("The message exceeds the 256 MiB intake limit.")
                if usage + size > MAX_SPOOL_BYTES:
                    raise SpoolCapacityError("The local work queue has reached its 2 GiB capacity.")
                if parent_fd is not None and hasattr(os, "fstatvfs"):
                    filesystem = os.fstatvfs(parent_fd)
                    free_bytes = filesystem.f_bavail * filesystem.f_frsize
                else:
                    free_bytes = shutil.disk_usage(self.path).free
                if free_bytes - len(chunk) < DISK_RESERVE_BYTES:
                    raise SpoolCapacityError(
                        "Not enough local disk space to keep mail and database reserve."
                    )
                handle.write(chunk)
                digest.update(chunk)
            if size == 0:
                raise RuntimeError("The mail provider returned an empty message.")
            handle.flush()
            os.fsync(handle.fileno())
        return digest.hexdigest()

    def stage(self, chunks: Iterable[bytes]) -> tuple[Path, str]:
        usage = self.usage_bytes()
        if usage >= MAX_SPOOL_BYTES:
            raise SpoolCapacityError("The local work queue has reached its 2 GiB capacity.")
        with self.directory_handle() as parent_fd:
            temporary_name = f"intake-{uuid4()}.tmp"
            final_name = f"{uuid4()}.eml"
            temporary = self.path / temporary_name
            final = self.path / final_name
            try:
                if parent_fd is None:
                    descriptor, temporary_path = tempfile.mkstemp(
                        prefix="intake-", suffix=".tmp", dir=self.path
                    )
                    temporary = Path(temporary_path)
                    temporary_name = temporary.name
                else:
                    descriptor = os.open(
                        temporary_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent_fd,
                    )
            except OSError as exc:
                if exc.errno in DISK_FULL_ERRNOS:
                    raise SpoolCapacityError(
                        "Local disk space ran out during mail intake."
                    ) from exc
                raise
            published = False
            try:
                digest = self._write(descriptor, chunks, usage, parent_fd)
                if parent_fd is None:
                    os.replace(temporary, final)
                else:
                    os.replace(
                        temporary_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
                    )
                published = True
                if parent_fd is not None:
                    os.fsync(parent_fd)
                elif os.name != "nt":
                    safe_parent = os.open(self.path, os.O_RDONLY)
                    try:
                        os.fsync(safe_parent)
                    finally:
                        os.close(safe_parent)
                published = False
                return final, digest
            except OSError as exc:
                if exc.errno in DISK_FULL_ERRNOS:
                    raise SpoolCapacityError(
                        "Local disk space ran out during mail intake."
                    ) from exc
                raise
            finally:
                try:
                    if parent_fd is None:
                        temporary.unlink(missing_ok=True)
                    else:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
                if published:
                    try:
                        if parent_fd is None:
                            final.unlink(missing_ok=True)
                        else:
                            os.unlink(final_name, dir_fd=parent_fd)
                    except OSError:
                        pass

    def _unlink(self, path: Path, descriptor: int | None) -> None:
        try:
            if descriptor is None:
                path.unlink(missing_ok=True)
            else:
                os.unlink(path.name, dir_fd=descriptor)
        except FileNotFoundError:
            with _CLEANUP_LOCK:
                self._pending_cleanup.discard(path)
        except OSError:
            with _CLEANUP_LOCK:
                self._pending_cleanup.add(path)
        else:
            with _CLEANUP_LOCK:
                self._pending_cleanup.discard(path)

    def discard(self, path: Path) -> None:
        """Delete obsolete work data without changing the owning database state."""
        path = Path(path)
        if path.parent != self.path or not path.name:
            with _CLEANUP_LOCK:
                self._pending_cleanup.add(path)
            return
        try:
            with self.directory_handle() as descriptor:
                self._unlink(path, descriptor)
        except SpoolError:
            with _CLEANUP_LOCK:
                self._pending_cleanup.add(path)

    def _retry_cleanup(self) -> None:
        with _CLEANUP_LOCK:
            pending = tuple(self._pending_cleanup)
        for path in pending:
            self.discard(path)

    def usage_bytes(self) -> int:
        self._retry_cleanup()
        usage = 0
        with self.directory_handle() as descriptor:
            if descriptor is None:
                for path in self.path.iterdir():
                    if path.suffix not in {".eml", ".tmp"}:
                        continue
                    try:
                        details = path.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode):
                        usage += details.st_size
                return usage
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if Path(entry.name).suffix not in {".eml", ".tmp"}:
                        continue
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode):
                        usage += details.st_size
        return usage

    def cleanup_unreferenced(self, retained_paths: set[str]) -> None:
        """Remove only orphaned regular work files after DB recovery decides retention."""
        with self.directory_handle() as descriptor:
            if descriptor is None:
                for path in self.path.iterdir():
                    if path.suffix not in {".eml", ".tmp"} or str(path) in retained_paths:
                        continue
                    try:
                        details = path.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode):
                        self._unlink(path, None)
                return
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if Path(entry.name).suffix not in {".eml", ".tmp"}:
                        continue
                    path = self.path / entry.name
                    if str(path) in retained_paths:
                        continue
                    try:
                        details = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(details.st_mode):
                        self._unlink(path, descriptor)
