"""One processing path for automatic discovery and explicit historical selections."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

from mailarchive.credentials import CredentialStore
from mailarchive.engine import ArchiveEngine, _sender_time, _utc
from mailarchive.imap_client import ImapMailbox, RemoteMessage, RemoteMessageUnavailable
from mailarchive.intake_limits import IntakeCapacityError, MessageTooLargeError
from mailarchive.mail_identity import MailTarget, MessageScope
from mailarchive.mail_sources import (
    MessageSourceRegistry,
    ProviderHttpError,
    ScanWideProviderError,
)
from mailarchive.models import Account, Mailbox, MailProvider, Settings
from mailarchive.rules import select_rule
from mailarchive.synchronization import RangePagination, SyncSession
from mailarchive.workspace import RunNotActiveError, WorkspaceStore


class EventLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class ServiceEvent:
    level: EventLevel
    message: str
    account_id: str | None = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass(slots=True)
class RunProgress:
    message: str
    active: bool = True


class ArchiveRunBusyError(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class AccountRunResult:
    account_id: str
    archived: int = 0
    already_processed: int = 0
    unmatched: int = 0
    skipped_unmatched: int = 0
    skipped_existing: int = 0
    skipped_no_attachments: int = 0
    failed: int = 0
    checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return self.already_processed + self.skipped_existing + self.skipped_no_attachments

    def add(self, other: AccountRunResult) -> None:
        for name in (
            "archived",
            "already_processed",
            "unmatched",
            "skipped_unmatched",
            "skipped_existing",
            "skipped_no_attachments",
            "failed",
            "checked",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.errors.extend(other.errors)


def _scope_key(account: Account, mailbox: Mailbox, target: MailTarget) -> str:
    if account.provider == MailProvider.GMAIL_API:
        return "gmail-mailbox"
    return target.folder


def _message_key(account: Account, scope: MessageScope, remote_id: str) -> str:
    if account.provider == MailProvider.GENERIC_IMAP:
        return scope.processing_namespace + "\0" + remote_id
    return remote_id


def _selected_targets(
    account: Account, mailbox: Mailbox, targets: list[MailTarget], selected: set[str] | None
) -> list[MailTarget]:
    if selected is None:
        return targets
    if account.provider == MailProvider.GMAIL_API:
        if not selected:
            return targets
        if not selected <= set(mailbox.folders):
            raise ValueError("The selected Gmail labels are not configured for this mailbox.")
        return [
            MailTarget(
                account,
                mailbox,
                "",
                tuple(folder for folder in mailbox.folders if folder in selected),
            )
        ]
    return [target for target in targets if target.folder in selected]


class ArchiveService:
    def __init__(
        self,
        credential_store: CredentialStore,
        state: WorkspaceStore,
        event_handler: Callable[[ServiceEvent], None] | None = None,
        mailbox: ImapMailbox | None = None,
        source_registry: MessageSourceRegistry | None = None,
        progress_handler: Callable[[RunProgress], None] | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.state = state
        self.engine = ArchiveEngine(state)
        self.event_handler = event_handler or (lambda event: None)
        self.progress_handler = progress_handler or (lambda progress: None)
        self.source_registry = source_registry or MessageSourceRegistry(
            credential_store, imap_mailbox=mailbox
        )
        self._run_lock = threading.Lock()
        self.active_range_run_id: str | None = None
        self._retried_automatic_messages: set[tuple[str, str]] = set()

    @contextmanager
    def account_change(self) -> Iterator[None]:
        if not self._run_lock.acquire(blocking=False):
            raise ArchiveRunBusyError("MailArchive is processing another operation.")
        try:
            yield
        finally:
            self._run_lock.release()

    def _event(self, level: EventLevel, message: str, account: Account | None = None) -> None:
        self.event_handler(ServiceEvent(level, message, account.id if account else None))

    def run_once(
        self,
        settings: Settings,
        account_ids: set[str] | None = None,
        *,
        force_retry: bool = False,
    ) -> list[AccountRunResult]:
        """Establish each scope's baseline, then process newly discovered source IDs."""
        return self._run(settings, "automatic", account_ids=account_ids, force_retry=force_retry)

    def run_range(
        self,
        settings: Settings,
        source_ids: set[str],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        folders: dict[str, set[str]] | None = None,
        timezone_name: str | None = None,
    ) -> list[AccountRunResult]:
        """Apply current rules to a provider search; automatic cursors stay untouched."""
        start = _utc(start) if start else None
        end = _utc(end) if end else None
        if start and end and start >= end:
            raise ValueError("The end of the reception range must follow its start.")
        timezone_name = timezone_name or settings.archive_timezone
        ZoneInfo(timezone_name)
        return self._run(
            settings,
            "manual",
            source_ids=source_ids,
            start=start,
            end=end,
            folders=folders,
            range_timezone=timezone_name,
        )

    def _run(
        self,
        settings: Settings,
        kind: str,
        *,
        account_ids: set[str] | None = None,
        source_ids: set[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        folders: dict[str, set[str]] | None = None,
        range_timezone: str | None = None,
        force_retry: bool = False,
    ) -> list[AccountRunResult]:
        if not self._run_lock.acquire(blocking=False):
            raise ArchiveRunBusyError("MailArchive is already processing a run.")
        results: dict[str, AccountRunResult] = {}
        try:
            self.progress_handler(RunProgress("Processing mail..."))
            live_settings = settings
            claimed_revision = settings.config_revision
            settings = deepcopy(settings)
            config_revision = self.state.prepare_run_settings(settings)
            if live_settings.config_revision == claimed_revision:
                live_settings.config_revision = config_revision
            if kind == "automatic":
                done, failed = self.engine.resume_all()
                if done or failed:
                    self._event(
                        EventLevel.INFO, f"Open work resumed: {done} outputs, {failed} errors."
                    )
                if failed:
                    self._report_open_work_errors()
                self._retried_automatic_messages.clear()
                blocked_sources = self._resume_automatic_intakes(
                    settings,
                    results,
                    selected_account_ids=account_ids,
                    force_retry=force_retry,
                )
            else:
                blocked_sources = set()
            for account in settings.accounts:
                if not account.enabled or (
                    account_ids is not None and account.id not in account_ids
                ):
                    continue
                result = results.setdefault(account.id, AccountRunResult(account.id))
                for mailbox in account.mailboxes:
                    if not mailbox.enabled or (
                        source_ids is not None and mailbox.id not in source_ids
                    ):
                        continue
                    if mailbox.id in blocked_sources:
                        continue
                    try:
                        self._run_mailbox(
                            account,
                            mailbox,
                            settings,
                            kind,
                            result,
                            start=start,
                            end=end,
                            folders=folders,
                            range_timezone=range_timezone,
                            config_revision=config_revision,
                            force_retry=force_retry,
                        )
                    except Exception as exc:
                        result.failed += 1
                        result.errors.append(str(exc))
                        self._event(
                            EventLevel.ERROR, f"{account.label}: {mailbox.address}: {exc}", account
                        )
            return list(results.values())
        finally:
            self._retried_automatic_messages.clear()
            self._run_lock.release()
            self.progress_handler(RunProgress("Mail processing finished.", active=False))

    def _run_mailbox(
        self,
        account: Account,
        mailbox: Mailbox,
        settings: Settings,
        kind: str,
        result: AccountRunResult,
        *,
        start: datetime | None,
        end: datetime | None,
        folders: dict[str, set[str]] | None,
        range_timezone: str | None,
        config_revision: int,
        force_retry: bool,
    ) -> None:
        source = self.source_registry.get(account)
        targets = source.targets(account, mailbox)
        if (
            kind == "automatic"
            and account.provider != MailProvider.GMAIL_API
            and not mailbox.folders
        ):
            self.state.prepare_scope_discovery(
                mailbox.id, {_scope_key(account, mailbox, target) for target in targets}
            )
        if not targets:
            raise RuntimeError("The mailbox has no selectable folders.")
        if folders and mailbox.id in folders:
            targets = _selected_targets(account, mailbox, targets, folders[mailbox.id])
        selection = {
            "folders": (
                list(targets[0].selected_folders or mailbox.folders)
                if account.provider == MailProvider.GMAIL_API and targets
                else [target.folder for target in targets]
            ),
            "start_utc": start.isoformat() if start else None,
            "end_utc": end.isoformat() if end else None,
            "timezone": range_timezone if kind == "manual" else None,
        }
        run_id = self.state.start_run(mailbox.id, kind, selection, settings, config_revision)
        if kind == "manual":
            self.active_range_run_id = run_id
        errors: list[str] = []
        previous_failures = result.failed
        for target in targets:
            if self.state.run_status(run_id) == "cancelled":
                break
            try:
                self._run_target(
                    account,
                    mailbox,
                    target,
                    source,
                    run_id,
                    kind,
                    settings,
                    result,
                    force_retry=force_retry,
                    start=start,
                    end=end,
                )
            except RunCancelled:
                break
            except Exception as exc:
                errors.append(f"{target.folder}: {exc}")
                result.failed += 1
                result.errors.append(str(exc))
                self._event(EventLevel.ERROR, f"{target.label}: {exc}", account)
                if isinstance(exc, (IntakeCapacityError, ScanWideProviderError)) or (
                    isinstance(exc, ProviderHttpError) and exc.scan_wide
                ):
                    break
        unresolved = self.state.unresolved_intakes(run_id)
        if unresolved and result.failed == previous_failures:
            result.failed += len(unresolved)
        if unresolved and not errors:
            errors.append("Some message downloads remain unresolved; this run can be resumed.")
        self.state.finish_run(run_id, error="; ".join(errors) if errors else None)
        if kind == "manual":
            self.active_range_run_id = None

    def _run_target(
        self,
        account: Account,
        mailbox: Mailbox,
        target: MailTarget,
        source,
        run_id: str,
        kind: str,
        settings: Settings,
        result: AccountRunResult,
        *,
        force_retry: bool,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        scope_key = _scope_key(account, mailbox, target)
        previous = self.state.scope(mailbox.id, scope_key) if kind == "automatic" else None
        if previous and previous["status"] == "paused":
            raise RuntimeError(previous["error"] or "This source folder is paused.")
        baseline = kind == "automatic" and (previous is None or not previous["baseline_done"])
        if account.provider == MailProvider.GMAIL_API and kind == "automatic" and not baseline:
            self._baseline_new_gmail_labels(account, mailbox, target, source, result)
        reserved: dict[str, str] = {}
        processed: set[str] = set()

        def should_fetch(scope: MessageScope, remote_id: str) -> bool:
            if self.state.run_status(run_id) == "cancelled":
                raise RunCancelled("The range run was cancelled.")
            return self._reserve_or_cancel(
                account,
                mailbox,
                scope,
                remote_id,
                run_id,
                kind,
                scope_key,
                baseline,
                result,
                reserved,
                force_retry,
            )

        if kind == "automatic":
            sync = SyncSession(
                cursor_for=lambda namespace: (
                    str(previous["cursor"])
                    if previous
                    and previous["synchronization_namespace"] == namespace
                    and previous["cursor"]
                    else None
                ),
                recheck_ids_for=lambda _namespace: self.state.pending_rechecks(
                    mailbox.id, scope_key, force_retry=force_retry
                ),
                baseline=baseline,
            )
            scope, messages = source.fetch_messages(target, should_fetch, sync=sync)
            self._check_imap_scope(account, mailbox, scope_key, previous, scope, messages)
        else:
            sync = None
            checkpoint = self.state.range_target_checkpoint(run_id, scope_key)
            if checkpoint["complete"]:
                return
            range_sync = self._range_pagination(run_id, scope_key, checkpoint)
            scope, messages = self._range_messages(
                source, target, should_fetch, start, end, range_sync
            )

        try:
            for remote in messages:
                self._raise_if_cancelled(run_id)
                intake_id = reserved.get(remote.id)
                if intake_id is None:
                    continue
                processed.add(remote.id)
                self._handle_remote(
                    remote, intake_id, account, mailbox.id, kind, result, start, end
                )
            self._raise_if_cancelled(run_id)
            self._release_discarded(mailbox.id, scope_key, sync, processed)
            missing = self._mark_missing(
                reserved, processed, "The provider did not return this message."
            )
            result.failed += missing
            if missing:
                detail = (
                    f"{target.label}: {missing} reserved provider message(s) were not returned."
                )
                result.errors.append(detail)
                self._event(EventLevel.ERROR, detail, account)
            if sync is not None:
                self._finish_target_scope(mailbox.id, scope_key, scope, sync)
                if baseline and account.provider == MailProvider.GMAIL_API:
                    self._mark_gmail_labels_baselined(mailbox.id, mailbox.folders, scope, sync)
            else:
                if range_sync.namespace is None:
                    range_sync.start(scope.processing_namespace)
                range_sync.finish()
        except RunCancelled:
            self._close_messages(messages)
            raise
        except Exception as exc:
            self._close_messages(messages)
            self._raise_if_cancelled(run_id)
            self._release_discarded(mailbox.id, scope_key, sync, processed)
            self._mark_missing(
                reserved,
                processed,
                f"The provider scan stopped before download: {exc}",
            )
            raise

    def _range_pagination(
        self, run_id: str, scope_key: str, checkpoint: dict[str, object]
    ) -> RangePagination:
        def save(namespace: str, token: str | None, complete: bool, force: bool) -> bool:
            return self.state.update_range_target_checkpoint(
                run_id,
                scope_key,
                namespace,
                token,
                complete,
                force=force,
            )

        namespace = checkpoint["namespace"]
        token = checkpoint["token"]
        return RangePagination(
            namespace if isinstance(namespace, str) else None,
            token if isinstance(token, str) else None,
            save,
        )

    @staticmethod
    def _range_messages(source, target, should_fetch, start, end, range_sync):
        if hasattr(source, "search_messages"):
            return source.search_messages(target, should_fetch, start, end, range_sync=range_sync)
        return source.fetch_messages(target, should_fetch, sync=None)

    def _raise_if_cancelled(self, run_id: str) -> None:
        if self.state.run_status(run_id) == "cancelled":
            raise RunCancelled("The range run was cancelled.")

    def _release_discarded(
        self,
        source_id: str,
        scope_key: str,
        sync: SyncSession | None,
        processed: set[str],
    ) -> None:
        if sync is None:
            return
        self.state.discard_pending(source_id, scope_key, sync.discarded_ids)
        processed.update(sync.discarded_ids)

    @staticmethod
    def _close_messages(messages) -> None:
        close = getattr(messages, "close", None)
        if close is not None:
            close()

    def _check_imap_scope(
        self, account: Account, mailbox: Mailbox, scope_key: str, previous, scope, messages
    ) -> None:
        if (
            previous
            and previous["baseline_done"]
            and account.provider == MailProvider.GENERIC_IMAP
            and previous["processing_namespace"] != scope.processing_namespace
        ):
            error = (
                "IMAP UIDVALIDITY changed for this folder. Choose a new baseline or "
                "run an explicit range; old UID receipts cannot be reused safely."
            )
            self._close_messages(messages)
            self.state.pause_scope(mailbox.id, scope_key, error)
            raise RuntimeError(error)

    def _mark_missing(self, reserved: dict[str, str], processed: set[str], error: str) -> int:
        missing = [
            intake_id for remote_id, intake_id in reserved.items() if remote_id not in processed
        ]
        for intake_id in missing:
            if not self.state.mark_intake_error(intake_id, error):
                raise RunCancelled("The range run was cancelled.")
        return len(missing)

    def _mark_gmail_labels_baselined(
        self, source_id: str, labels: list[str], scope: MessageScope, sync: SyncSession
    ) -> None:
        for label in labels or ["*"]:
            self.state.finish_scope(
                source_id,
                "gmail-label:" + label,
                scope.processing_namespace,
                scope.synchronization_namespace,
                sync.next_cursor or "",
            )

    def _baseline_new_gmail_labels(
        self,
        account: Account,
        mailbox: Mailbox,
        target: MailTarget,
        source,
        result: AccountRunResult,
    ) -> None:
        if self.state.scope(mailbox.id, "gmail-label:*"):
            return
        for label in mailbox.folders or ["*"]:
            if self.state.scope(mailbox.id, "gmail-label:" + label):
                continue
            selected = () if label == "*" else (label,)
            baseline_target = MailTarget(account, mailbox, "", selected)
            sync = SyncSession(
                cursor_for=lambda _namespace: None, recheck_ids_for=lambda _namespace: set()
            )

            def skip_existing(scope: MessageScope, remote_id: str) -> bool:
                self.state.baseline_message(mailbox.id, _message_key(account, scope, remote_id))
                result.checked += 1
                result.skipped_existing += 1
                return False

            messages = None
            try:
                scope, messages = source.fetch_messages(baseline_target, skip_existing, sync=sync)
                for _ in messages:
                    pass
                if sync.next_cursor is None:
                    raise RuntimeError("Gmail did not complete the new label baseline.")
                self._mark_gmail_labels_baselined(
                    mailbox.id, [label] if label != "*" else [], scope, sync
                )
            except Exception as exc:
                if messages is not None:
                    self._close_messages(messages)
                self._event(
                    EventLevel.ERROR, f"Could not baseline Gmail label {label}: {exc}", account
                )
                raise RuntimeError(f"Could not baseline Gmail label {label}: {exc}") from exc

    def _reserve_candidate(
        self,
        account: Account,
        mailbox: Mailbox,
        scope: MessageScope,
        remote_id: str,
        run_id: str,
        kind: str,
        scope_key: str,
        baseline: bool,
        result: AccountRunResult,
        reserved: dict[str, str],
        force_retry: bool,
    ) -> bool:
        key = _message_key(account, scope, remote_id)
        if kind == "automatic" and (mailbox.id, key) in self._retried_automatic_messages:
            return False
        result.checked += 1
        if baseline:
            self.state.baseline_message(mailbox.id, key)
            result.skipped_existing += 1
            return False
        intake_id = self.state.reserve(
            mailbox.id,
            key,
            run_id,
            automatic=kind == "automatic",
            scope_key=scope_key,
            remote_id=remote_id,
            force_retry=force_retry,
        )
        if intake_id is None:
            if kind == "automatic" and self.state.intake_retry_deferred(mailbox.id, key):
                return False
            result.already_processed += 1
            return False
        reserved[remote_id] = intake_id
        return True

    def _reserve_or_cancel(
        self,
        account: Account,
        mailbox: Mailbox,
        scope: MessageScope,
        remote_id: str,
        run_id: str,
        kind: str,
        scope_key: str,
        baseline: bool,
        result: AccountRunResult,
        reserved: dict[str, str],
        force_retry: bool,
    ) -> bool:
        try:
            return self._reserve_candidate(
                account,
                mailbox,
                scope,
                remote_id,
                run_id,
                kind,
                scope_key,
                baseline,
                result,
                reserved,
                force_retry,
            )
        except RunNotActiveError as exc:
            raise RunCancelled("The range run was cancelled.") from exc

    def _finish_target_scope(
        self, source_id: str, scope_key: str, scope: MessageScope, sync: SyncSession
    ) -> None:
        if sync.next_cursor is None:
            raise RuntimeError("The mail provider did not complete its discovery cursor.")
        self.state.finish_scope(
            source_id,
            scope_key,
            scope.processing_namespace,
            scope.synchronization_namespace,
            sync.next_cursor,
        )

    def _handle_remote(
        self,
        remote: RemoteMessage,
        intake_id: str,
        account: Account,
        source_id: str,
        kind: str,
        result: AccountRunResult,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        staged = None
        received = None
        try:
            if remote.error is not None:
                raise remote.error
            received = _utc(remote.received_at)
            if kind == "manual" and ((start and received < start) or (end and received >= end)):
                self.state.mark_filtered(
                    intake_id,
                    received_at=received.isoformat(),
                    received_origin=remote.received_origin,
                )
                return
            snapshot = self.state.intake_snapshot(intake_id)
            staged = self.engine.stage(remote)
            mail = staged.mail
            owner_id = next(
                (
                    old_account.id
                    for old_account in snapshot.accounts
                    for old_mailbox in old_account.mailboxes
                    if old_mailbox.id == source_id
                ),
                account.id,
            )
            rule = select_rule(snapshot.rules, mail, account_id=owner_id)
            if rule is None:
                self.state.mark_unmatched(
                    intake_id,
                    received_at=received.isoformat(),
                    received_origin=remote.received_origin,
                    sender_at=_sender_time(mail.date_header),
                    subject=mail.subject,
                )
                result.unmatched += 1
                staged.discard()
                staged = None
                return
            plan_id = self.engine.accept_staged(
                intake_id, remote, staged, rule, snapshot.archive_timezone
            )
            staged = None
            self._execute_accepted_plan(plan_id, mail.subject, account, result)
        except IntakeCapacityError as exc:
            self._handle_capacity_failure(remote, intake_id, account, result, staged, received, exc)
        except RemoteMessageUnavailable as exc:
            if staged is not None:
                staged.discard()
            if not self.state.mark_intake_error(
                intake_id,
                str(exc),
                received_at=received.isoformat() if received is not None else None,
                received_origin=remote.received_origin if received is not None else None,
            ):
                raise RunCancelled("The range run was cancelled.") from exc
            result.failed += 1
            detail = f"{remote.id}: {exc}"
            result.errors.append(detail)
            self._event(EventLevel.ERROR, f"Could not accept message {detail}", account)
        except Exception as exc:
            if staged is not None:
                staged.discard()
            if not self.state.mark_intake_error(
                intake_id,
                str(exc),
                received_at=received.isoformat() if received is not None else None,
                received_origin=remote.received_origin if received is not None else None,
            ):
                raise RunCancelled("The range run was cancelled.") from exc
            if isinstance(exc, ScanWideProviderError):
                raise
            result.failed += 1
            result.errors.append(f"{remote.id}: {exc}")
            self._event(EventLevel.ERROR, f"Could not accept message {remote.id}: {exc}", account)
        finally:
            remote.release_resources()

    def _execute_accepted_plan(
        self, plan_id: str, subject: str, account: Account, result: AccountRunResult
    ) -> None:
        try:
            done, failed = self.engine.execute(plan_id)
        except Exception as exc:
            self.state.set_plan_error(plan_id, str(exc))
            result.failed += 1
            detail = f"{subject}: {exc}"
            result.errors.append(detail)
            self._event(EventLevel.ERROR, detail, account)
            return
        result.archived += int(done > 0)
        result.failed += failed
        if failed:
            target_errors = [
                f"{target['path']}: {target['error']}"
                for target in self.state.plan_targets(plan_id)
                if target["status"] == "error"
            ]
            detail = (
                f"{subject}: " + "; ".join(target_errors)
                if target_errors
                else f"{subject}: {failed} archive outputs failed."
            )
            result.errors.append(detail)
            self._event(EventLevel.ERROR, detail, account)
        if not done and not failed:
            if self.state.outputs(plan_id):
                result.already_processed += 1
            else:
                result.skipped_no_attachments += 1

    def _handle_capacity_failure(
        self,
        remote: RemoteMessage,
        intake_id: str,
        account: Account,
        result: AccountRunResult,
        staged,
        received: datetime | None,
        error: IntakeCapacityError,
    ) -> None:
        if staged is not None:
            staged.discard()
        received_at = received.isoformat() if received is not None else None
        received_origin = remote.received_origin if received is not None else None
        if isinstance(error, MessageTooLargeError):
            self.state.reject_intake(
                intake_id,
                str(error),
                received_at=received_at,
                received_origin=received_origin,
            )
            result.failed += 1
            detail = f"{remote.id}: {error}"
            result.errors.append(detail)
            self._event(EventLevel.ERROR, f"Message rejected: {detail}", account)
            return
        if not self.state.mark_intake_error(
            intake_id,
            str(error),
            received_at=received_at,
            received_origin=received_origin,
        ):
            raise RunCancelled("The range run was cancelled.") from error
        raise error

    def _resume_automatic_intakes(
        self,
        settings: Settings,
        results: dict[str, AccountRunResult],
        *,
        selected_account_ids: set[str] | None,
        force_retry: bool,
    ) -> set[str]:
        """Retry stable provider IDs before applying the current discovery filters."""
        blocked_sources: set[str] = set()
        for intake in self.state.pending_automatic_intakes(due_only=not force_retry):
            source_id = str(intake["source_id"])
            if source_id in blocked_sources:
                continue
            if self._automatic_intake_is_covered(
                settings, intake, selected_account_ids=selected_account_ids
            ):
                continue
            account = None
            try:
                snapshot = self.state.run_settings_snapshot(str(intake["run_id"]))
                owner = next(
                    (
                        (candidate, mailbox)
                        for candidate in snapshot.accounts
                        for mailbox in candidate.mailboxes
                        if mailbox.id == intake["source_id"]
                    ),
                    None,
                )
                if owner is None:
                    raise RuntimeError(
                        "The unfinished intake's source is missing from its saved snapshot."
                    )

                account, mailbox = owner
                result = results.setdefault(account.id, AccountRunResult(account.id))
                message_key = str(intake["message_key"])
                self._retried_automatic_messages.add((mailbox.id, message_key))
                processing_namespace = (
                    message_key.rsplit("\0", 1)[0]
                    if account.provider == MailProvider.GENERIC_IMAP
                    else MailTarget(account, mailbox, "").mailbox_namespace
                )
                target = MailTarget(
                    account,
                    mailbox,
                    "" if account.provider == MailProvider.GMAIL_API else str(intake["scope_key"]),
                    tuple(mailbox.folders),
                )
                source = self.source_registry.get(account)
                remote = source.fetch_message(
                    target, str(intake["remote_id"]), processing_namespace
                )
                if remote is None:
                    detail = (
                        f"Message {intake['remote_id']} is no longer available before "
                        "intake completed."
                    )
                    if self.state.mark_intake_error(str(intake["id"]), detail):
                        result.failed += 1
                        result.errors.append(detail)
                        self._event(EventLevel.ERROR, detail, account)
                    continue
                result.checked += 1
                self._handle_remote(
                    remote,
                    str(intake["id"]),
                    account,
                    mailbox.id,
                    "automatic",
                    result,
                    None,
                    None,
                )
            except IntakeCapacityError as exc:
                blocked_sources.add(source_id)
                result = (
                    results.setdefault(account.id, AccountRunResult(account.id))
                    if account is not None
                    else None
                )
                if result is not None:
                    result.failed += 1
                    result.errors.append(str(exc))
                break
            except Exception as exc:
                scan_wide = isinstance(exc, ScanWideProviderError) or (
                    isinstance(exc, ProviderHttpError) and exc.scan_wide
                )
                already_recorded = isinstance(exc, ScanWideProviderError)
                if already_recorded or self.state.mark_intake_error(str(intake["id"]), str(exc)):
                    if account is not None:
                        result = results.setdefault(account.id, AccountRunResult(account.id))
                        result.failed += 1
                        result.errors.append(str(exc))
                    self._event(
                        EventLevel.ERROR,
                        f"Could not resume message {intake['remote_id']}: {exc}",
                        account,
                    )
                if scan_wide:
                    blocked_sources.add(source_id)
        return blocked_sources

    def _automatic_intake_is_covered(
        self, settings: Settings, intake, *, selected_account_ids: set[str] | None
    ) -> bool:
        """Whether the normal current scan will reconcile this unfinished ID."""
        for account in settings.accounts:
            if not account.enabled:
                continue
            if selected_account_ids is not None and account.id not in selected_account_ids:
                continue
            for mailbox in account.mailboxes:
                if mailbox.id != intake["source_id"] or not mailbox.enabled:
                    continue
                scope = self.state.scope(mailbox.id, str(intake["scope_key"]))
                if scope is None or not scope["baseline_done"] or scope["status"] != "active":
                    return False
                if account.provider == MailProvider.GMAIL_API:
                    snapshot = Settings.from_dict(json.loads(intake["settings_json"]))
                    saved_mailbox = next(
                        (
                            saved
                            for saved_account in snapshot.accounts
                            for saved in saved_account.mailboxes
                            if saved.id == intake["source_id"]
                        ),
                        None,
                    )
                    return saved_mailbox is not None and set(saved_mailbox.folders) == set(
                        mailbox.folders
                    )
                if not mailbox.folders:
                    return True
                return str(intake["scope_key"]) in mailbox.folders
        return False

    def _report_open_work_errors(self) -> None:
        for plan in self.state.open_plans():
            if plan["error"]:
                self._event(EventLevel.ERROR, f"Open plan {plan['id']}: {plan['error']}")
            for target in self.state.plan_targets(plan["id"]):
                if target["status"] == "error":
                    self._event(
                        EventLevel.ERROR,
                        f"Open plan {plan['id']}, destination {target['path']}: {target['error']}",
                    )

    def resume_open(self) -> tuple[int, int]:
        with self.account_change():
            return self.engine.resume_all(force=True)

    def has_automatic_work(self) -> bool:
        return self.state.automatic_work_due()

    def resume_range_run(self, run_id: str) -> AccountRunResult:
        """Restart an interrupted provider search with its saved rule selection."""
        with self.account_change():
            run = next(
                (item for item in self.state.incomplete_manual_runs() if item["id"] == run_id), None
            )
            if run is None:
                raise ValueError("The selected range run is not resumable.")
            settings = self.state.run_settings_snapshot(run_id)
            owner = next(
                (
                    (account, mailbox)
                    for account in settings.accounts
                    for mailbox in account.mailboxes
                    if mailbox.id == run["source_id"]
                ),
                None,
            )
            if owner is None:
                raise RuntimeError("The range run's source is missing from its saved snapshot.")
            account, mailbox = owner
            selection = json.loads(run["selection_json"])
            start = (
                datetime.fromisoformat(selection["start_utc"]) if selection["start_utc"] else None
            )
            end = datetime.fromisoformat(selection["end_utc"]) if selection["end_utc"] else None
            source = self.source_registry.get(account)
            result = AccountRunResult(account.id)
            self.state.restart_run(run_id)
            self.active_range_run_id = run_id
            errors = []
            try:
                targets = self._saved_range_targets(account, mailbox, source, selection["folders"])
                for target in targets:
                    if self.state.run_status(run_id) == "cancelled":
                        break
                    try:
                        self._run_target(
                            account,
                            mailbox,
                            target,
                            source,
                            run_id,
                            "manual",
                            settings,
                            result,
                            force_retry=False,
                            start=start,
                            end=end,
                        )
                    except RunCancelled:
                        break
                    except Exception as exc:
                        errors.append(f"{target.folder}: {exc}")
                        result.failed += 1
                        result.errors.append(str(exc))
                        if isinstance(exc, (IntakeCapacityError, ScanWideProviderError)) or (
                            isinstance(exc, ProviderHttpError) and exc.scan_wide
                        ):
                            break
            except Exception as exc:
                errors.append(str(exc))
                result.failed += 1
                result.errors.append(str(exc))
            finally:
                unresolved = self.state.unresolved_intakes(run_id)
                if unresolved and not result.failed:
                    result.failed += len(unresolved)
                if unresolved and not errors:
                    errors.append(
                        "Some message downloads remain unresolved; this run can be resumed."
                    )
                self.state.finish_run(run_id, error="; ".join(errors) if errors else None)
                self.active_range_run_id = None
            return result

    @staticmethod
    def _saved_range_targets(
        account: Account, mailbox: Mailbox, source, folders
    ) -> list[MailTarget]:
        if (
            not isinstance(folders, list)
            or not all(isinstance(folder, str) and folder for folder in folders)
            or (account.provider != MailProvider.GMAIL_API and not folders)
        ):
            raise ValueError("The saved range folder selection is invalid.")
        selected = set(folders)
        if account.provider == MailProvider.GMAIL_API:
            return _selected_targets(account, mailbox, source.targets(account, mailbox), selected)
        return [MailTarget(account, mailbox, folder, tuple(folders)) for folder in folders]

    def abort_plan(self, plan_id: str) -> None:
        with self.account_change():
            self.state.abort_plan(plan_id)

    def pause_plan(self, plan_id: str) -> None:
        with self.account_change():
            self.state.pause_plan(plan_id)

    def resume_plan(self, plan_id: str) -> tuple[int, int]:
        with self.account_change():
            self.state.resume_plan(plan_id)
            try:
                return self.engine.execute(plan_id, force=True)
            except (OSError, RuntimeError, ValueError) as exc:
                self.state.set_plan_error(plan_id, str(exc))
                self._event(EventLevel.ERROR, f"Could not resume plan {plan_id}: {exc}")
                return 0, 1

    def reset_scope_baseline(self, source_id: str, folder: str) -> int:
        with self.account_change():
            return self.state.reset_scope_baseline(source_id, folder)

    def cancel_run(self, run_id: str) -> None:
        self.state.cancel_run(run_id)

    def cancel_intake(self, intake_id: str) -> None:
        with self.account_change():
            self.state.cancel_intake(intake_id)
