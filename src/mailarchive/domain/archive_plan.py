"""Deterministic archive artifacts for an accepted raw message and rule snapshot."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mailarchive.domain.archive_paths import destination_path, safe_filename
from mailarchive.domain.configuration import Rule, SaveMode
from mailarchive.domain.mail_parser import legacy_attachments, parse_mail


@dataclass(frozen=True, slots=True)
class PlannedArtifact:
    """Keep the durable requested identity separate from its publication filename."""

    target_id: str
    artifact_key: str
    content: bytes
    digest: str
    requested_path: Path
    publication_path: Path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_outputs(
    raw: bytes,
    rule: Rule,
    *,
    received_at: datetime,
    timezone_name: str,
    source_id: str,
    message_key: str,
    legacy_manifest: bool = False,
) -> list[PlannedArtifact]:
    """Plan every target output without filesystem or database access."""
    if received_at.tzinfo is None:
        raise ValueError("The provider did not supply a valid reception time.")
    local_time = received_at.astimezone(ZoneInfo(timezone_name))
    mail = parse_mail(raw)
    attachments = legacy_attachments(raw) if legacy_manifest else mail.attachments
    identity = _digest(f"{source_id}\x00{message_key}".encode())[:12]
    base = (
        f"{local_time:%Y-%m-%d_%H-%M-%S}_{safe_filename(mail.subject, 'no subject', 60)}_{identity}"
    )
    artifacts: list[PlannedArtifact] = []
    for target in rule.targets:
        folder = destination_path(target.path, mail_date=local_time)
        if target.save_mode in {SaveMode.EMAIL_ONLY, SaveMode.EMAIL_AND_ATTACHMENTS}:
            artifacts.append(
                PlannedArtifact(
                    target.id,
                    "email",
                    raw,
                    _digest(raw),
                    folder / f"{base}.eml",
                    folder / f"{base}.eml",
                )
            )
        if target.save_mode in {SaveMode.ATTACHMENTS_ONLY, SaveMode.EMAIL_AND_ATTACHMENTS}:
            attachment_folder = (
                folder
                if target.attachments_in_destination
                else folder / f"Mail_{identity}_Attachments"
            )
            occurrences: dict[tuple[str, str], int] = {}
            for index, attachment in enumerate(attachments, start=1):
                name = safe_filename(attachment.filename, f"Attachment-{index}", 120)
                publication_name = safe_filename(
                    attachment.filename,
                    f"Attachment-{index}",
                    120,
                    preserve_extension=True,
                )
                attachment_hash = _digest(attachment.content)
                group = (name, attachment_hash)
                occurrences[group] = occurrences.get(group, 0) + 1
                occurrence = occurrences[group]
                artifacts.append(
                    PlannedArtifact(
                        target.id,
                        f"attachment:{name}:{attachment_hash}:{occurrence}",
                        attachment.content,
                        attachment_hash,
                        attachment_folder / f"{attachment_hash[:10]}-{occurrence:02d}_{name}",
                        attachment_folder
                        / f"{attachment_hash[:10]}-{occurrence:02d}_{publication_name}",
                    )
                )
    return artifacts
