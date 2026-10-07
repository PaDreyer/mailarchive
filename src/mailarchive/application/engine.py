"""Durable mail intake and idempotent individual archive outputs."""

from __future__ import annotations

import errno
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mailarchive.application.archive_destinations import ArchiveDestinationPolicy
from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation, ProcessingStopped
from mailarchive.application.errors import RunNotActiveError
from mailarchive.application.intake_limits import MAX_MESSAGE_BYTES, MessageTooLargeError
from mailarchive.application.processing_ports import (
    DeliveryPort,
    OperationPort,
    OutputFilesPort,
    PlanExecution,
    Record,
    SpoolPort,
)
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.archive_paths import bounded_filename
from mailarchive.domain.archive_plan import PlannedArtifact, plan_outputs
from mailarchive.domain.configuration import ParsedMail, Rule
from mailarchive.domain.mail_parser import parse_mail


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
    def __init__(
        self,
        delivery: DeliveryPort,
        operations: OperationPort,
        spool: SpoolPort,
        plan_execution: PlanExecution,
        output_files: OutputFilesPort,
        *,
        destination_policy: ArchiveDestinationPolicy | None = None,
    ) -> None:
        self.delivery = delivery
        self.operations = operations
        self.spool = spool
        self.plan_execution = plan_execution
        self.output_files = output_files
        self.destination_policy = destination_policy
        self.should_stop: Callable[[], bool] = lambda: False

    def stage(
        self, remote: RemoteMessage, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> StagedMessage:
        cancellation.checkpoint()
        if remote.raw_size is not None and remote.raw_size > MAX_MESSAGE_BYTES:
            raise MessageTooLargeError("The message exceeds the 256 MiB intake limit.")
        raw_path, raw_hash = self.spool.stage(cancellation.chunks(remote.iter_raw()))
        try:
            cancellation.checkpoint()
            raw = self.spool.read(raw_path, MAX_MESSAGE_BYTES)
            return StagedMessage(raw_path, raw_hash, parse_mail(raw), self.spool.discard)
        except Exception:
            self.spool.discard(raw_path)
            raise

    def require_rules(self, rules: list[Rule], *, account_id: str | None = None) -> None:
        if self.destination_policy is not None:
            for rule in rules:
                if rule.enabled and (
                    account_id is None or rule.account_ids is None or account_id in rule.account_ids
                ):
                    self.destination_policy.require_rule(rule)

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
            if self.destination_policy is not None:
                self.destination_policy.require_rule(
                    rule, mail_date=received.astimezone(ZoneInfo(timezone_name))
                )
            return self.delivery.accept_plan(
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

    def _free_path(self, requested: Path) -> Path:
        limit = self.output_files.filename_limit(requested.parent)
        number = 1
        while True:
            candidate = requested.with_name(
                bounded_filename(requested.name, max_bytes=limit, number=number)
            )
            if not self.output_files.occupied(candidate) and not self.delivery.reserved_output_path(
                str(candidate)
            ):
                return candidate
            number += 1

    def _ensure_outputs(self, plan, raw: bytes) -> dict[tuple[str, str, str], PlannedArtifact]:
        artifacts_by_key: dict[tuple[str, str, str], PlannedArtifact] = {}
        target_counts = {str(row["target_id"]): 0 for row in self.delivery.plan_targets(plan["id"])}
        snapshot = json.loads(plan["rule_json"])
        rule = Rule.from_dict(snapshot["rule"])
        options = dict(
            received_at=datetime.fromisoformat(plan["received_at"]),
            timezone_name=snapshot["timezone"],
            source_id=plan["source_id"],
            message_key=plan["message_key"],
        )
        planned = plan_outputs(raw, rule, **options)
        saved_keys = {
            (output["artifact_key"], output["digest"], output["requested_path"])
            for output in self.delivery.outputs(plan["id"])
            if output["status"] != "done"
        }
        current_keys = {
            (artifact.artifact_key, artifact.digest, str(artifact.requested_path))
            for artifact in planned
        }
        if saved_keys - current_keys:
            for artifact in plan_outputs(raw, rule, legacy_manifest=True, **options):
                key = artifact.artifact_key, artifact.digest, str(artifact.requested_path)
                if key in saved_keys:
                    artifacts_by_key[key] = artifact
        for artifact in planned:
            target_counts[artifact.target_id] += 1
            request_text = str(artifact.requested_path)
            artifact_key, digest = artifact.artifact_key, artifact.digest
            key = artifact_key, digest, request_text
            artifacts_by_key[key] = artifact
            receipt = self.delivery.receipt(plan["source_id"], plan["message_key"], *key)
            final_path = str(receipt["final_path"]) if receipt is not None else ""
            self.delivery.add_output(
                plan["id"],
                plan["source_id"],
                plan["message_key"],
                artifact_key,
                digest,
                request_text,
                final_path,
                artifact.target_id,
                status="done" if receipt is not None else "pending",
            )
        for target_id, count in target_counts.items():
            if count == 0:
                self.delivery.mark_target_no_output(plan["id"], target_id)
        self.delivery.refresh_target_statuses(plan["id"])
        return artifacts_by_key

    def execute(
        self,
        plan_id: str,
        *,
        force: bool = False,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[int, int]:
        cancellation.checkpoint()
        with self.plan_execution(plan_id):
            return self._execute_locked(plan_id, force=force, cancellation=cancellation)

    def _execute_locked(
        self, plan_id: str, *, force: bool, cancellation: Cancellation
    ) -> tuple[int, int]:
        plan = next((row for row in self.delivery.open_plans() if row["id"] == plan_id), None)
        if plan is None:
            return 0, 0
        path = Path(plan["raw_path"])
        try:
            raw = self.spool.read(path, MAX_MESSAGE_BYTES)
        except RuntimeError as exc:
            raise RuntimeError(
                f"The local working copy for plan {plan_id} is unavailable: {exc}"
            ) from exc
        if _digest(raw) != plan["raw_hash"]:
            raise RuntimeError(f"The local working copy for plan {plan_id} is damaged.")
        self.delivery.set_plan_error(plan_id, None)
        if self.should_stop() or not self.operations.plan_operation_active(plan_id, explicit=force):
            return 0, 0
        cancellation.checkpoint()
        artifacts_by_key = self._ensure_outputs(plan, raw)
        done = failed = 0
        for output in self.delivery.outputs(plan_id):
            cancellation.checkpoint()
            if self.should_stop() or not self.operations.plan_operation_active(
                plan_id, explicit=force
            ):
                break
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
            artifact = artifacts_by_key[key]
            started_at = datetime.now(timezone.utc).isoformat()
            try:
                self._publish_output(output, artifact, cancellation)
                self.delivery.output_done(output["id"], plan, started_at=started_at)
                done += 1
            except (OSError, RuntimeError, ValueError) as exc:
                self.delivery.output_error(output["id"], str(exc), started_at=started_at)
                failed += 1
        self.delivery.finish_plan_if_complete(plan_id)
        return done, failed

    def _publish_output(
        self, output, artifact: PlannedArtifact, cancellation: Cancellation
    ) -> None:
        preferred = artifact.publication_path
        if self.destination_policy is not None:
            self.destination_policy.require_path(preferred.parent)
        # Allocate only inside this output's error boundary. An unavailable
        # destination must not prevent registration or publication of its peers.
        if not output["final_path"]:
            destination = self._free_path(preferred)
            self.delivery.set_output_path(output["id"], str(destination))
        else:
            destination = Path(output["final_path"])
        limit = self.output_files.filename_limit(preferred.parent)
        extension = Path(bounded_filename(preferred.name, max_bytes=limit)).suffix
        for _ in range(1000):
            cancellation.checkpoint()
            if self.destination_policy is not None:
                self.destination_policy.require_path(destination.parent)
            try:
                if not self.output_files.occupied(destination):
                    if destination.suffix != extension:
                        destination = self._free_path(preferred)
                        self.delivery.set_output_path(output["id"], str(destination))
                    try:
                        cancellation.checkpoint()
                        self.output_files.publish(destination, artifact.content)
                        return
                    except FileExistsError:
                        pass
                elif self.output_files.matches(
                    destination, output["digest"], len(artifact.content)
                ):
                    return
            except OSError as exc:
                if exc.errno != errno.ENAMETOOLONG:
                    raise
                replacement = self._free_path(preferred)
                if replacement == destination:
                    raise
                destination = replacement
                self.delivery.set_output_path(output["id"], str(destination))
                continue
            destination = self._free_path(preferred)
            self.delivery.set_output_path(output["id"], str(destination))
        raise RuntimeError("Could not choose an unused output filename.")

    def resume_all(
        self,
        *,
        force: bool = False,
        cancellation: Cancellation = NO_CANCELLATION,
        excluded_source_ids: frozenset[str] = frozenset(),
        on_plan_finished: Callable[[Record, int, int], None] | None = None,
    ) -> tuple[int, int]:
        done = failed = 0
        for plan in self.delivery.auto_resumable_plans(
            excluded_source_ids=excluded_source_ids, force=force
        ):
            cancellation.checkpoint()
            if self.should_stop():
                break
            failure = None
            try:
                made, errors = self.execute(plan["id"], force=force, cancellation=cancellation)
            except RunNotActiveError as exc:
                cancellation.checkpoint()
                if self.should_stop():
                    raise ProcessingStopped("Mail processing is shutting down.") from exc
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                cancellation.checkpoint()
                if self.should_stop():
                    raise ProcessingStopped("Mail processing is shutting down.") from exc
                made, errors = 0, 1
                failure = str(exc)
                # Missing/corrupt work copies stay open and visible.
                with self.plan_execution(plan["id"]):
                    self.delivery.record_plan_failure(plan["id"], str(exc))
            done += made
            failed += errors
            if on_plan_finished is not None:
                on_plan_finished(dict(plan, error=failure), made, errors)
        return done, failed
