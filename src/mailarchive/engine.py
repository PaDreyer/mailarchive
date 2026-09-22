"""Durable mail intake and idempotent individual archive outputs."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from mailarchive.imap_client import RemoteMessage
from mailarchive.intake_limits import (
    DISK_RESERVE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_SPOOL_BYTES,
    MessageTooLargeError,
    SpoolCapacityError,
)
from mailarchive.mail_parser import parse_mail
from mailarchive.models import ParsedMail, Rule, SaveMode
from mailarchive.storage import _atomic_write, destination_path, safe_filename
from mailarchive.workspace import WorkspaceStore

DISK_FULL_ERRNOS = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


@dataclass(slots=True)
class StagedMessage:
    path: Path
    digest: str
    mail: ParsedMail
    cleanup: Callable[[Path], None]

    def discard(self) -> None:
        self.cleanup(self.path)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None:
        raise ValueError("The provider did not supply a valid reception time.")
    return value.astimezone(timezone.utc)


def _sender_time(header: str) -> str | None:
    if not header:
        return None
    try:
        value = parsedate_to_datetime(header)
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


class ArchiveEngine:
    def __init__(self, state: WorkspaceStore) -> None:
        self.state = state

    def _write_spool_file(
        self,
        descriptor: int,
        chunks: Iterable[bytes],
        existing_usage: int,
        directory_descriptor: int | None,
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
                if existing_usage + size > MAX_SPOOL_BYTES:
                    raise SpoolCapacityError("The local work queue has reached its 2 GiB capacity.")
                if directory_descriptor is not None and hasattr(os, "fstatvfs"):
                    filesystem = os.fstatvfs(directory_descriptor)
                    free_bytes = filesystem.f_bavail * filesystem.f_frsize
                else:
                    free_bytes = shutil.disk_usage(self.state.spool_dir).free
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

    def _spool(self, chunks: Iterable[bytes]) -> tuple[Path, str]:
        _, usage = self.state.spool_usage()
        if usage >= MAX_SPOOL_BYTES:
            raise SpoolCapacityError("The local work queue has reached its 2 GiB capacity.")
        with self.state._spool_directory_handle() as parent_fd:
            temporary_name = f"intake-{uuid4()}.tmp"
            final_name = f"{uuid4()}.eml"
            temporary = self.state.spool_dir / temporary_name
            final = self.state.spool_dir / final_name
            try:
                if parent_fd is None:
                    descriptor, temporary_path = tempfile.mkstemp(
                        prefix="intake-", suffix=".tmp", dir=self.state.spool_dir
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
                digest = self._write_spool_file(descriptor, chunks, usage, parent_fd)
                if parent_fd is None:
                    os.replace(temporary, final)
                else:
                    os.replace(
                        temporary_name,
                        final_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                published = True
                if parent_fd is not None:
                    os.fsync(parent_fd)
                elif os.name != "nt":
                    safe_parent = os.open(self.state.spool_dir, os.O_RDONLY)
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

    def stage(self, remote: RemoteMessage) -> StagedMessage:
        if remote.raw_size is not None and remote.raw_size > MAX_MESSAGE_BYTES:
            raise MessageTooLargeError("The message exceeds the 256 MiB intake limit.")
        raw_path, raw_hash = self._spool(remote.iter_raw())
        try:
            raw = self.state.read_work_copy(raw_path, MAX_MESSAGE_BYTES)
            return StagedMessage(raw_path, raw_hash, parse_mail(raw), self.state.discard_work_copy)
        except Exception:
            self.state.discard_work_copy(raw_path)
            raise

    def accept_staged(
        self,
        intake_id: str,
        remote: RemoteMessage,
        staged: StagedMessage,
        rule: Rule,
        timezone_name: str,
    ) -> str:
        received = _utc(remote.received_at)
        ZoneInfo(timezone_name)
        try:
            return self.state.accept_plan(
                intake_id,
                staged.path,
                staged.digest,
                received.isoformat(),
                remote.received_origin,
                _sender_time(staged.mail.date_header),
                staged.mail.subject,
                json.dumps({"rule": rule.to_dict(), "timezone": timezone_name}, ensure_ascii=False),
            )
        except Exception:
            staged.discard()
            raise

    def accept(self, intake_id: str, remote: RemoteMessage, rule: Rule, timezone_name: str) -> str:
        staged = self.stage(remote)
        return self.accept_staged(intake_id, remote, staged, rule, timezone_name)

    def _artifacts(self, plan, raw: bytes):
        snapshot = json.loads(plan["rule_json"])
        rule = Rule.from_dict(snapshot["rule"])
        received = datetime.fromisoformat(plan["received_at"])
        local_time = received.astimezone(ZoneInfo(snapshot["timezone"]))
        mail = parse_mail(raw)
        identity = _digest(f"{plan['source_id']}\x00{plan['message_key']}".encode())[:12]
        base = f"{local_time:%Y-%m-%d_%H-%M-%S}_{safe_filename(mail.subject, 'no subject', 60)}_{identity}"
        artifacts = []
        for target in rule.targets:
            folder = destination_path(Path(), target.path, mail_date=local_time)
            if target.save_mode in {SaveMode.EMAIL_ONLY, SaveMode.EMAIL_AND_ATTACHMENTS}:
                artifacts.append((target.id, "email", raw, folder / f"{base}.eml"))
            if target.save_mode in {SaveMode.ATTACHMENTS_ONLY, SaveMode.EMAIL_AND_ATTACHMENTS}:
                attachment_folder = (
                    folder
                    if target.attachments_in_destination
                    else folder / f"Mail_{identity}_Attachments"
                )
                occurrences: dict[tuple[str, str], int] = {}
                for index, attachment in enumerate(mail.attachments, start=1):
                    name = safe_filename(attachment.filename, f"Attachment-{index}", 120)
                    attachment_hash = _digest(attachment.content)
                    group = (name, attachment_hash)
                    occurrences[group] = occurrences.get(group, 0) + 1
                    occurrence = occurrences[group]
                    # Equal attachments remain distinct. Unrelated parts added
                    # earlier do not shift this artifact's identity.
                    artifacts.append(
                        (
                            target.id,
                            f"attachment:{name}:{attachment_hash}:{occurrence}",
                            attachment.content,
                            attachment_folder / f"{attachment_hash[:10]}-{occurrence:02d}_{name}",
                        )
                    )
        return artifacts

    def _free_path(self, requested: Path) -> Path:
        stem, suffix = requested.stem, requested.suffix
        number = 1
        while True:
            candidate = (
                requested if number == 1 else requested.with_name(f"{stem}-{number}{suffix}")
            )
            if not candidate.exists() and not self.state.reserved_output_path(str(candidate)):
                return candidate
            number += 1

    def _ensure_outputs(self, plan, raw: bytes) -> dict[tuple[str, str, str], bytes]:
        content_by_key: dict[tuple[str, str, str], bytes] = {}
        target_counts = {str(row["target_id"]): 0 for row in self.state.plan_targets(plan["id"])}
        for target_id, artifact_key, content, requested in self._artifacts(plan, raw):
            target_counts[target_id] += 1
            digest = _digest(content)
            request_text = str(requested)
            key = artifact_key, digest, request_text
            content_by_key[key] = content
            receipt = self.state.receipt(plan["source_id"], plan["message_key"], *key)
            final_path = (
                str(receipt["final_path"])
                if receipt is not None
                else str(self._free_path(requested))
            )
            self.state.add_output(
                plan["id"],
                plan["source_id"],
                plan["message_key"],
                artifact_key,
                digest,
                request_text,
                final_path,
                target_id,
                status="done" if receipt is not None else "pending",
            )
        for target_id, count in target_counts.items():
            if count == 0:
                self.state.mark_target_no_output(plan["id"], target_id)
        self.state.refresh_target_statuses(plan["id"])
        return content_by_key

    def execute(self, plan_id: str, *, force: bool = False) -> tuple[int, int]:
        with self.state.plan_execution(plan_id):
            return self._execute_locked(plan_id, force=force)

    def _execute_locked(self, plan_id: str, *, force: bool) -> tuple[int, int]:
        plan = next((row for row in self.state.open_plans() if row["id"] == plan_id), None)
        if plan is None:
            return 0, 0
        path = Path(plan["raw_path"])
        try:
            raw = self.state.read_work_copy(path, MAX_MESSAGE_BYTES)
        except RuntimeError as exc:
            raise RuntimeError(
                f"The local working copy for plan {plan_id} is unavailable: {exc}"
            ) from exc
        if _digest(raw) != plan["raw_hash"]:
            raise RuntimeError(f"The local working copy for plan {plan_id} is damaged.")
        self.state.set_plan_error(plan_id, None)
        content_by_key = self._ensure_outputs(plan, raw)
        done = failed = 0
        for output in self.state.outputs(plan_id):
            if output["status"] == "done":
                continue
            if (
                not force
                and output["status"] == "error"
                and output["retry_after"]
                and datetime.fromisoformat(output["retry_after"]) > datetime.now(timezone.utc)
            ):
                continue
            key = output["artifact_key"], output["digest"], output["requested_path"]
            content = content_by_key[key]
            destination = Path(output["final_path"])
            try:
                for _ in range(1000):
                    try:
                        info = destination.lstat()
                    except FileNotFoundError:
                        try:
                            _atomic_write(destination, content)
                            break
                        except FileExistsError:
                            pass
                    else:
                        if (
                            stat.S_ISREG(info.st_mode)
                            and info.st_size == len(content)
                            and _digest(destination.read_bytes()) == output["digest"]
                        ):
                            break
                    destination = self._free_path(Path(output["requested_path"]))
                    self.state.set_output_path(output["id"], str(destination))
                else:
                    raise RuntimeError("Could not choose an unused output filename.")
                self.state.output_done(output["id"], plan)
                done += 1
            except (OSError, RuntimeError) as exc:
                self.state.output_error(output["id"], str(exc))
                failed += 1
        self.state.finish_plan_if_complete(plan_id)
        return done, failed

    def resume_all(self, *, force: bool = False) -> tuple[int, int]:
        done = failed = 0
        for plan in self.state.open_plans():
            try:
                made, errors = self.execute(plan["id"], force=force)
                done += made
                failed += errors
            except (OSError, RuntimeError, ValueError) as exc:
                failed += 1
                # Missing/corrupt work copies stay open and visible.
                with self.state.plan_execution(plan["id"]):
                    self.state.set_plan_error(plan["id"], str(exc))
                    with self.state.connection() as db, db:
                        active = self.state._active_plan(db, plan["id"])
                        if active is not None:
                            db.execute(
                                "UPDATE intake SET error=? "
                                "WHERE run_id=? AND source_id=? AND message_key=?",
                                (
                                    str(exc),
                                    plan["run_id"],
                                    plan["source_id"],
                                    plan["message_key"],
                                ),
                            )
        return done, failed
