"""One processing path for automatic discovery and explicit historical selections."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from mailarchive.application.account_status import (
    AccountAction,
    AccountBlocker,
    AccountStatus,
    AccountStatusService,
)
from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation, ProcessingStopped
from mailarchive.application.credential_port import CredentialError
from mailarchive.application.engine import ArchiveEngine, _sender_time, _utc
from mailarchive.application.errors import RunNotActiveError
from mailarchive.application.events import EventLevel, ServiceEvent
from mailarchive.application.intake_admission import (
    IntakeOwnerDeferred,
    automatic_intake_owner_matches,
)
from mailarchive.application.intake_limits import IntakeCapacityError, MessageTooLargeError
from mailarchive.application.processing_ports import (
    ConfigurationPort,
    DeliveryPort,
    DiscoveryPort,
    OperationPort,
    Record,
)
from mailarchive.application.source_port import (
    RemoteAccess,
    RemoteMessage,
    RemoteMessageNamespaceChanged,
    RemoteMessageOutsideScope,
    RemoteMessageUnavailable,
    ScanWideProviderError,
    SourceRegistry,
    is_scan_wide_error,
)
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    Settings,
)
from mailarchive.domain.rules import (
    rule_may_match_headers,
    select_rule,
)
from mailarchive.domain.source_identity import MailTarget, MessageScope, folder_scope_key


class ArchiveRunBusyError(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


def _notify_account_finished(
    account_id: str, cancellation: Cancellation, callback: Callable[[str], None] | None
) -> None:
    cancellation.checkpoint()
    if callback is not None:
        callback(account_id)


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
    _credentials_failed: bool = field(default=False, init=False, repr=False)
    _deferred_sources: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def skipped(self) -> int:
        return self.already_processed + self.skipped_existing + self.skipped_no_attachments

    def _record_credential_failure(self, error: Exception) -> None:
        """Abort this account's remaining remote work only for protected-store failures."""
        self._credentials_failed = self._credentials_failed or isinstance(error, CredentialError)

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
        self._credentials_failed = self._credentials_failed or other._credentials_failed
        self._deferred_sources.update(other._deferred_sources)


def _scope_key(account: Account, mailbox: Mailbox, target: MailTarget) -> str:
    if account.provider == MailProvider.GMAIL_API:
        return "gmail-mailbox"
    return folder_scope_key(account.provider, target.folder)


def _message_key(account: Account, scope: MessageScope, remote_id: str) -> str:
    if account.provider == MailProvider.GENERIC_IMAP:
        return scope.processing_namespace + "\0" + remote_id
    return remote_id


def _scope_is_paused(scope: Record | None) -> bool:
    return scope is not None and scope["status"] == "paused"


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
    selected_keys = {folder_scope_key(account.provider, folder) for folder in selected}
    selected_targets = [
        target for target in targets if _scope_key(account, mailbox, target) in selected_keys
    ]
    selected_folders = tuple(target.folder for target in selected_targets)
    return [
        MailTarget(target.account, target.mailbox, target.folder, selected_folders)
        for target in selected_targets
    ]


class ArchiveService:
    def __init__(
        self,
        configuration: ConfigurationPort,
        discovery: DiscoveryPort,
        operations: OperationPort,
        delivery: DeliveryPort,
        engine: ArchiveEngine,
        source_registry: SourceRegistry,
        *,
        event_handler: Callable[[ServiceEvent], None] | None = None,
        progress_handler: Callable[[str], None] | None = None,
        account_statuses: AccountStatusService | None = None,
    ) -> None:
        self.configuration = configuration
        self.discovery = discovery
        self.operations = operations
        self.delivery = delivery
        self.engine = engine
        self.source_registry = source_registry
        self.account_statuses = account_statuses or AccountStatusService()
        self.event_handler = event_handler or (lambda event: None)
        self.progress_handler = progress_handler or (lambda progress: None)
        self._run_lock = threading.Lock()
        self._shutdown_requested = threading.Event()
        self.engine.should_stop = self._shutdown_requested.is_set
        self.active_range_run_id: str | None = None
        self._retried_automatic_messages: set[tuple[str, str]] = set()

    def account_status(
        self, account: Account, settings: Settings, *, inspect: bool = False
    ) -> AccountStatus:
        return self.account_statuses.resolve(account, settings.rules, inspect=inspect)

    def require_account_action(
        self, account: Account, settings: Settings, action: AccountAction, *, inspect: bool = False
    ) -> None:
        status = self.account_status(account, settings, inspect=inspect)
        if not status.allows(action):
            detail = (
                "The account was removed from this profile."
                if AccountBlocker.REMOVED in status.blockers
                else status.authorization.detail
            ) or (
                "Authorize the account before accessing mail."
                if not status.allows(AccountAction.RETRY_REMOTE)
                else "Enable the account, a mailbox, and an applicable rule before accessing mail."
            )
            raise ValueError(f"{account.label}: {detail}")

    def _remote_access(
        self, account: Account, settings: Settings, cancellation: Cancellation
    ) -> RemoteAccess:
        revision = -1

        def require_access() -> None:
            nonlocal revision
            current = self.account_statuses.revision
            if revision != current:
                self.require_account_action(
                    account, settings, AccountAction.RETRY_REMOTE, inspect=True
                )
                # A concurrent credential change must be seen at the next checkpoint.
                revision = current

        return RemoteAccess(cancellation.requested, lambda: cancellation.message, require_access)

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()

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
        cancellation: Cancellation = NO_CANCELLATION,
        excluded_source_ids: frozenset[str] = frozenset(),
        on_account_finished: Callable[[str], None] | None = None,
    ) -> list[AccountRunResult]:
        """Establish each scope's baseline, then process newly discovered source IDs."""
        return self._run(
            settings,
            "automatic",
            account_ids=account_ids,
            force_retry=force_retry,
            cancellation=cancellation,
            excluded_source_ids=excluded_source_ids,
            on_account_finished=on_account_finished,
        )

    def run_range(
        self,
        settings: Settings,
        source_ids: set[str],
        *,
        rule_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        folders: dict[str, set[str]] | None = None,
        timezone_name: str | None = None,
    ) -> list[AccountRunResult]:
        """Apply current rules, or one selected rule, without moving automatic cursors."""
        operation_id = self.prepare_range_operation(
            settings,
            source_ids,
            rule_id=rule_id,
            start=start,
            end=end,
            folders=folders,
            timezone_name=timezone_name,
        )
        return self.run_range_operation(operation_id)

    def prepare_range_operation(
        self,
        settings: Settings,
        source_ids: set[str],
        *,
        rule_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        folders: dict[str, set[str]] | None = None,
        timezone_name: str | None = None,
    ) -> str:
        """Validate and freeze the entire selected search before queueing it."""
        if rule_id is not None:
            rule = next((item for item in settings.rules if item.id == rule_id), None)
            if rule is None or not rule.enabled:
                raise ValueError("Select an enabled rule for the past-mail run.")
            permitted_sources = {
                mailbox.id
                for account in settings.accounts
                if rule.account_ids is None or account.id in rule.account_ids
                for mailbox in account.mailboxes
            }
            if not source_ids or not source_ids <= permitted_sources:
                raise ValueError("The selected rule does not apply to every selected mailbox.")
        start = _utc(start) if start else None
        end = _utc(end) if end else None
        if start and end and start >= end:
            raise ValueError("The end of the reception range must follow its start.")
        timezone_name = timezone_name or settings.archive_timezone
        ZoneInfo(timezone_name)
        selected = [
            mailbox.id
            for account in settings.accounts
            if account.enabled
            for mailbox in account.mailboxes
            if mailbox.enabled and mailbox.id in source_ids
        ]
        if len(selected) != len(source_ids) or not selected:
            raise ValueError("Select at least one enabled mailbox for the past-mail run.")
        for account in settings.accounts:
            if any(mailbox.id in source_ids for mailbox in account.mailboxes):
                self.engine.require_rules(
                    self._candidate_rules(settings, rule_id), account_id=account.id
                )
                self.require_account_action(account, settings, AccountAction.READ_PAST_MAIL)
        selection = {
            "source_ids": selected,
            "start_utc": start.isoformat() if start else None,
            "end_utc": end.isoformat() if end else None,
            "timezone": timezone_name,
            "folders": {key: sorted(value) for key, value in (folders or {}).items()},
        }
        frozen = deepcopy(settings)
        return self.operations.create_manual_operation(frozen, selected, rule_id or "", selection)

    def run_range_operation(self, operation_id: str) -> list[AccountRunResult]:
        operation = self.operations.manual_operation(operation_id)
        if operation is None:
            raise ValueError("The selected past-mail operation is unavailable.")
        if not self.operations.claim_manual_operation(operation_id):
            current = self.operations.manual_operation(operation_id)
            if current and current["status"] == "stopping":
                self.operations.finalize_stop_manual_operation(operation_id)

                return []
            raise ValueError("The selected past-mail operation cannot run.")
        selection = json.loads(operation["selection_json"])
        settings = Settings.from_dict(json.loads(operation["settings_json"]))
        settings.config_revision = int(operation["config_revision"])
        start = datetime.fromisoformat(selection["start_utc"]) if selection["start_utc"] else None
        end = datetime.fromisoformat(selection["end_utc"]) if selection["end_utc"] else None
        folders = {key: set(value) for key, value in selection["folders"].items()}
        cancellation = Cancellation(
            lambda: (
                self._shutdown_requested.is_set()
                or not self.operations.manual_operation_accepts_work(operation_id)
            ),
            "The past-mail operation was stopped.",
        )
        try:
            resumed_done = resumed_failed = 0
            resumed_results: dict[str, AccountRunResult] = {}
            for plan in self.operations.manual_open_plans(operation_id):
                if not self.operations.manual_operation_accepts_work(operation_id):
                    break
                done, failed, failure = self._retry_saved_plan(
                    plan["id"], cancellation=cancellation
                )
                resumed_done += done
                resumed_failed += failed
                self._record_resumed_plan(resumed_results, dict(plan, error=failure), done, failed)
            if resumed_done or resumed_failed:
                self._event(
                    EventLevel.ERROR if resumed_failed else EventLevel.SUCCESS,
                    f"Past-mail output retry: {resumed_done} completed, {resumed_failed} failed.",
                )
            current_results = self._run(
                settings,
                "manual",
                source_ids=set(selection["source_ids"]),
                start=start,
                end=end,
                folders=folders,
                range_timezone=selection["timezone"],
                selected_rule_id=operation["rule_id"] or None,
                operation_id=operation_id,
                cancellation=cancellation,
            )
            for result in current_results:
                resumed_results.setdefault(
                    result.account_id, AccountRunResult(result.account_id)
                ).add(result)
            return list(resumed_results.values())
        except ProcessingStopped as exc:
            if self.operations.manual_operation_accepts_work(operation_id):
                self.operations.finish_manual_operation(operation_id, str(exc))
                raise
            return []
        except Exception as exc:
            if self.operations.manual_operation_accepts_work(operation_id):
                self.operations.finish_manual_operation(operation_id, str(exc))
            raise
        finally:
            current = self.operations.manual_operation(operation_id)
            if current and current["status"] == "stopping":
                self.operations.finalize_stop_manual_operation(operation_id)

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
        selected_rule_id: str | None = None,
        operation_id: str | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
        excluded_source_ids: frozenset[str] = frozenset(),
        on_account_finished: Callable[[str], None] | None = None,
    ) -> list[AccountRunResult]:
        if not self._run_lock.acquire(blocking=False):
            raise ArchiveRunBusyError("MailArchive is already processing a run.")
        parent = cancellation
        cancellation = Cancellation(
            lambda: self._shutdown_requested.is_set() or parent.requested(),
            lambda: parent.message if parent.requested() else "Mail processing is shutting down.",
        )
        results: dict[str, AccountRunResult] = {}
        try:
            self.progress_handler("Processing mail.")
            cancellation.checkpoint()
            live_settings = settings
            claimed_revision = settings.config_revision
            settings = deepcopy(settings)
            config_revision = self.configuration.prepare_run_settings(settings)
            if live_settings.config_revision == claimed_revision:
                live_settings.config_revision = config_revision
            if kind == "automatic":
                done, failed = self.engine.resume_all(
                    force=force_retry,
                    cancellation=cancellation,
                    excluded_source_ids=excluded_source_ids,
                    on_plan_finished=lambda plan, made, errors: self._record_resumed_plan(
                        results, plan, made, errors
                    ),
                )
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
                    cancellation=cancellation,
                    excluded_source_ids=excluded_source_ids,
                )
            else:
                blocked_sources = set()
            for account in self._accounts_for_run(settings, kind, account_ids, source_ids):
                cancellation.checkpoint()
                result = results.setdefault(account.id, AccountRunResult(account.id))
                for mailbox in account.mailboxes:
                    cancellation.checkpoint()
                    if result._credentials_failed:
                        break
                    if mailbox.id in excluded_source_ids:
                        continue
                    if operation_id and not self.operations.manual_operation_accepts_work(
                        operation_id
                    ):
                        break
                    if not mailbox.enabled or (
                        source_ids is not None and mailbox.id not in source_ids
                    ):
                        continue
                    if mailbox.id in blocked_sources:
                        continue
                    self._process_selected_mailbox(
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
                        selected_rule_id=selected_rule_id,
                        operation_id=operation_id,
                        cancellation=cancellation,
                    )
                _notify_account_finished(account.id, cancellation, on_account_finished)
            if operation_id and self.operations.manual_operation_accepts_work(operation_id):
                self.operations.finish_manual_operation(operation_id)
            return list(results.values())
        finally:
            self._retried_automatic_messages.clear()
            self._run_lock.release()

    def _record_resumed_plan(
        self,
        results: dict[str, AccountRunResult],
        plan: Record,
        done: int,
        failed: int,
    ) -> None:
        """Count retained local work even when its account is paused or removed."""
        if not done and not failed:
            return
        snapshot = self.operations.run_settings_snapshot(str(plan["run_id"]))
        account = next(
            (
                account
                for account in snapshot.accounts
                if any(mailbox.id == plan["source_id"] for mailbox in account.mailboxes)
            ),
            None,
        )
        if account is None:
            raise RuntimeError("The archive plan's account is missing from its saved snapshot.")
        result = results.setdefault(account.id, AccountRunResult(account.id))
        result.archived += int(done > 0)
        result.failed += failed
        if failed:
            details = [
                f"{target['path']}: {target['error']}"
                for target in self.delivery.plan_targets(str(plan["id"]))
                if target["status"] == "error"
            ]
            if plan["error"]:
                details.insert(0, str(plan["error"]))
            result.errors.extend(details or [str(plan["error"] or "Local archive work failed.")])

    def _accounts_for_run(
        self,
        settings: Settings,
        kind: str,
        account_ids: set[str] | None,
        source_ids: set[str] | None,
    ) -> Iterator[Account]:
        for account in settings.accounts:
            if account_ids is not None and account.id not in account_ids:
                continue
            if source_ids is not None and not any(m.id in source_ids for m in account.mailboxes):
                continue
            if kind == "automatic":
                if self.account_status(account, settings, inspect=True).allows(
                    AccountAction.CHECK_MAIL
                ):
                    yield account
            elif account.enabled:
                yield account

    def _process_selected_mailbox(
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
        selected_rule_id: str | None,
        operation_id: str | None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        try:
            error_start = len(result.errors)
            if operation_id:
                self.operations.mark_operation_source(operation_id, mailbox.id, "running")
            existing = (
                self.operations.manual_run_for_source(operation_id, mailbox.id)
                if operation_id
                else None
            )
            if existing and existing["status"] == "completed":
                self.operations.mark_operation_source(operation_id, mailbox.id, "completed")
                return
            run_id = self._run_mailbox(
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
                selected_rule_id=selected_rule_id,
                operation_id=operation_id,
                cancellation=cancellation,
                existing_run=existing,
            )
            if operation_id:
                run_status = self.operations.run_status(run_id)
                if run_status == "cancelled":
                    new_errors = result.errors[error_start:]
                    self.operations.mark_operation_source(
                        operation_id,
                        mailbox.id,
                        "failed" if new_errors else "stopped",
                        "; ".join(new_errors) if new_errors else None,
                    )
                else:
                    run = self.operations.manual_run_for_source(operation_id, mailbox.id)
                    self.operations.mark_operation_source(
                        operation_id,
                        mailbox.id,
                        "completed" if run_status == "completed" else "failed",
                        str(run["error"]) if run and run["error"] else None,
                    )
        except ProcessingStopped:
            raise
        except Exception as exc:
            cancellation.checkpoint()
            result._record_credential_failure(exc)
            if operation_id and self.operations.manual_operation_accepts_work(operation_id):
                self.operations.mark_operation_source(operation_id, mailbox.id, "failed", str(exc))
            result.failed += 1
            result.errors.append(str(exc))
            self._event(EventLevel.ERROR, f"{account.label}: {mailbox.address}: {exc}", account)

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
        selected_rule_id: str | None,
        operation_id: str | None = None,
        existing_run: Record | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> str:
        cancellation.checkpoint()
        self.engine.require_rules(
            self._candidate_rules(settings, selected_rule_id), account_id=account.id
        )
        self.require_account_action(account, settings, AccountAction.RETRY_REMOTE, inspect=True)
        remote_access = self._remote_access(account, settings, cancellation)
        source = self.source_registry.get(account)
        if existing_run is not None:
            saved_selection = json.loads(existing_run["selection_json"])
            targets = self._saved_range_targets(
                account, mailbox, source, saved_selection["folders"], cancellation=remote_access
            )
        else:
            targets = source.targets(account, mailbox, cancellation=remote_access)
        cancellation.checkpoint()
        if (
            kind == "automatic"
            and account.provider != MailProvider.GMAIL_API
            and not mailbox.folders
        ):
            self.discovery.prepare_scope_discovery(
                mailbox.id,
                {_scope_key(account, mailbox, target) for target in targets},
                settings=settings,
            )
        if not targets:
            raise RuntimeError("The mailbox has no selectable folders.")
        if existing_run is None and folders and mailbox.id in folders:
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
        if selected_rule_id is not None:
            selection["rule_id"] = selected_rule_id
        existing_run_id = str(existing_run["id"]) if existing_run is not None else None
        if existing_run_id is not None:
            self.operations.restart_run(existing_run_id)
        run_id = existing_run_id or self.operations.start_run(
            mailbox.id, kind, selection, settings, config_revision, operation_id=operation_id
        )
        errors: list[str] = []
        previous_failures = result.failed
        with self._scan_lifecycle(run_id, manual=kind == "manual"):
            for target in targets:
                cancellation.checkpoint()
                if self._shutdown_requested.is_set():
                    self.operations.interrupt_run(run_id)
                    break
                if self.operations.run_status(run_id) == "cancelled":
                    break
                if not self._scan_selected_target(
                    account,
                    mailbox,
                    target,
                    source,
                    run_id,
                    kind,
                    settings,
                    result,
                    errors,
                    force_retry=force_retry,
                    start=start,
                    end=end,
                    selected_rule_id=selected_rule_id,
                    cancellation=cancellation,
                ):
                    break
            unresolved = self.discovery.unresolved_intakes(run_id)
            if unresolved and result.failed == previous_failures:
                result.failed += len(unresolved)
            if unresolved and not errors:
                errors.append("Some message downloads remain unresolved; this run can be resumed.")
            if self.operations.run_status(run_id) == "running":
                if not errors and mailbox.id in result._deferred_sources:
                    self.operations.interrupt_run(
                        run_id,
                        "Another unfinished archive owns mail in this selection. Retry after it settles.",
                    )
                else:
                    self.operations.finish_run(run_id, error="; ".join(errors) if errors else None)
        return run_id

    @contextmanager
    def _scan_lifecycle(self, run_id: str, *, manual: bool) -> Iterator[None]:
        """Keep an opened scan resumable when processing or finalization aborts."""
        if manual:
            self.active_range_run_id = run_id
        try:
            yield
        except Exception as exc:
            self.operations.interrupt_run(run_id, str(exc))
            raise
        finally:
            if manual:
                self.active_range_run_id = None

    def _scan_selected_target(
        self,
        account: Account,
        mailbox: Mailbox,
        target: MailTarget,
        source,
        run_id: str,
        kind: str,
        settings: Settings,
        result: AccountRunResult,
        errors: list[str],
        *,
        force_retry: bool,
        start: datetime | None,
        end: datetime | None,
        selected_rule_id: str | None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> bool:
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
                selected_rule_id=selected_rule_id,
                cancellation=cancellation,
            )
        except RunCancelled as cancelled:
            if cancelled.__cause__ is not None:
                detail = str(cancelled.__cause__)
                result.failed += 1
                result.errors.append(detail)
                errors.append(detail)
            if self._shutdown_requested.is_set():
                self.operations.interrupt_run(run_id)
            return False
        except ProcessingStopped:
            raise
        except Exception as exc:
            cancellation.checkpoint()
            result._record_credential_failure(exc)
            errors.append(f"{target.folder}: {exc}")
            result.failed += 1
            result.errors.append(str(exc))
            if isinstance(exc, RemoteMessageNamespaceChanged):
                self._pause_target_namespace(run_id, account, mailbox, target, exc)
            if kind == "automatic":
                self.discovery.record_scope_check_error(
                    mailbox.id, _scope_key(account, mailbox, target), str(exc)
                )
            self._event(EventLevel.ERROR, f"{target.label}: {exc}", account)
            if (
                isinstance(exc, IntakeCapacityError)
                or result._credentials_failed
                or is_scan_wide_error(exc)
                or not self.account_status(account, settings, inspect=True).allows(
                    AccountAction.RETRY_REMOTE
                )
            ):
                return False
        return True

    def _pause_target_namespace(self, run_id, account, mailbox, target, error) -> None:
        scope_key = _scope_key(account, mailbox, target)
        for intake in self.discovery.unresolved_intakes(run_id):
            if folder_scope_key(account.provider, intake["scope_key"]) == scope_key:
                self.discovery.pause_intake_scope(str(intake["id"]), str(error))
        self.discovery.pause_scope(
            mailbox.id,
            scope_key,
            str(error),
            processing_namespace=error.previous_namespace,
        )

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
        selected_rule_id: str | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        cancellation.checkpoint()
        scope_key = _scope_key(account, mailbox, target)
        previous = self.discovery.scope(mailbox.id, scope_key) if kind == "automatic" else None
        if _scope_is_paused(previous):
            raise RuntimeError(previous["error"] or "This source folder is paused.")
        baseline = kind == "automatic" and (previous is None or not previous["baseline_done"])
        # A retained identity with an unfinished baseline represents an explicit
        # reset. Its confirmed skip choice survives failures and application restarts.
        resetting = previous is not None and previous["processing_namespace"] is not None
        skip_existing = baseline and (resetting or not mailbox.archive_existing_messages)
        new_label_ids: set[str] = set()
        new_labels: list[str] = []
        if account.provider == MailProvider.GMAIL_API and kind == "automatic" and not baseline:
            new_label_ids, new_labels = self._baseline_new_gmail_labels(
                account, mailbox, target, source, settings, result, cancellation=cancellation
            )
        reserved: dict[str, str] = {}
        processed: set[str] = set()
        remote_access = self._remote_access(account, settings, cancellation)
        retained_namespaces = (
            self.discovery.retained_imap_namespaces(target)
            if account.provider == MailProvider.GENERIC_IMAP
            and account.auth_mode == AuthMode.PASSWORD
            and (previous is None or previous["processing_namespace"] is None)
            else frozenset()
        )

        def should_fetch(scope: MessageScope, remote_id: str) -> bool:
            remote_access.checkpoint()
            if self.operations.run_status(run_id) == "cancelled":
                raise RunCancelled("The range run was cancelled.")
            return self._reserve_or_cancel(
                account,
                mailbox,
                scope,
                remote_id,
                run_id,
                kind,
                scope_key,
                skip_existing,
                result,
                reserved,
                force_retry,
                sync if kind == "automatic" else range_sync,
                selected_rule_id,
            )

        range_sync = None
        if kind == "automatic":
            self.require_account_action(account, settings, AccountAction.RETRY_REMOTE, inspect=True)
            sync = SyncSession(
                cursor_for=lambda namespace: (
                    str(previous["cursor"])
                    if previous
                    and previous["synchronization_namespace"] == namespace
                    and previous["cursor"]
                    else None
                ),
                recheck_ids_for=lambda _namespace: (
                    self.discovery.pending_rechecks(mailbox.id, scope_key, force_retry=force_retry)
                    | new_label_ids
                ),
                baseline=skip_existing,
                resume_namespace=previous["processing_namespace"] if previous else None,
                retained_namespaces=retained_namespaces,
            )
            scope, messages = source.fetch_messages(
                target, should_fetch, sync=sync, cancellation=remote_access
            )
            self._check_imap_scope(account, mailbox, scope_key, previous, scope, messages)
        else:
            sync = None
            checkpoint = self.operations.range_target_checkpoint(run_id, scope_key)
            if checkpoint["complete"]:
                return
            self.require_account_action(account, settings, AccountAction.RETRY_REMOTE, inspect=True)
            range_sync = self._range_pagination(run_id, scope_key, checkpoint)
            range_sync.retained_namespaces = retained_namespaces
            scope, messages = self._range_messages(
                source, target, should_fetch, start, end, range_sync, cancellation=remote_access
            )

        try:
            for remote in messages:
                try:
                    self._raise_if_cancelled(run_id, cancellation)
                except (RunCancelled, ProcessingStopped):
                    remote.release_resources()
                    raise
                if isinstance(remote.error, RemoteMessageOutsideScope):
                    self._release_outside_scope(run_id, scope_key, remote.id)
                    processed.add(remote.id)
                    remote.release_resources()
                    continue
                intake_id = reserved.get(remote.id)
                if intake_id is None:
                    continue
                processed.add(remote.id)
                self._handle_remote(
                    remote,
                    intake_id,
                    account,
                    mailbox.id,
                    kind,
                    result,
                    start,
                    end,
                    settings=settings,
                    selected_rule_id=selected_rule_id,
                    cancellation=cancellation,
                )
            self._complete_target_scan(
                account,
                mailbox,
                target,
                run_id,
                result,
                scope_key,
                scope,
                reserved,
                processed,
                sync,
                range_sync,
                baseline,
                new_labels,
                cancellation=cancellation,
            )
        except (RunCancelled, ProcessingStopped):
            self._close_messages(messages)
            raise
        except Exception as exc:
            self._close_messages(messages)
            try:
                self._raise_if_cancelled(run_id, cancellation)
            except (RunCancelled, ProcessingStopped) as cancelled:
                raise cancelled from exc
            self._release_discarded(mailbox.id, scope_key, sync, processed, run_id, scope)
            self._mark_missing(
                reserved,
                processed,
                f"The provider scan stopped before download: {exc}",
            )
            raise

    def _release_outside_scope(self, run_id: str, scope_key: str, remote_id: str) -> None:
        """Release earlier manual reservations without reserving newly moved-out mail."""
        for intake in self.discovery.unresolved_intakes(run_id):
            if intake["scope_key"] == scope_key and intake["remote_id"] == remote_id:
                self.discovery.release_intake(str(intake["id"]))

    def _complete_target_scan(
        self,
        account: Account,
        mailbox: Mailbox,
        target: MailTarget,
        run_id: str,
        result: AccountRunResult,
        scope_key: str,
        scope: MessageScope,
        reserved: dict[str, str],
        processed: set[str],
        sync: SyncSession | None,
        range_sync: RangePagination | None,
        baseline: bool,
        new_labels: list[str],
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        self._raise_if_cancelled(run_id, cancellation)
        self._release_discarded(mailbox.id, scope_key, sync, processed, run_id, scope)
        missing = self._mark_missing(
            reserved, processed, "The provider did not return this message."
        )
        result.failed += missing
        if missing:
            detail = f"{target.label}: {missing} reserved provider message(s) were not returned."
            result.errors.append(detail)
            self._event(EventLevel.ERROR, detail, account)
        if sync is not None:
            if sync.discovery_deferred:
                return
            self._finish_target_scope(mailbox.id, scope_key, scope, sync)
            if account.provider == MailProvider.GMAIL_API:
                self._finish_gmail_label_baselines(mailbox, scope, sync, baseline, new_labels)
        else:
            if range_sync is not None and range_sync.namespace is None:
                range_sync.start(scope.processing_namespace)
            if range_sync is not None:
                range_sync.finish()

    def _range_pagination(
        self, run_id: str, scope_key: str, checkpoint: dict[str, object]
    ) -> RangePagination:
        def save(namespace: str, token: str | None, complete: bool, force: bool) -> bool:
            return self.operations.update_range_target_checkpoint(
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
    def _range_messages(
        source, target, should_fetch, start, end, range_sync, cancellation=NO_CANCELLATION
    ):
        if hasattr(source, "search_messages"):
            return source.search_messages(
                target, should_fetch, start, end, range_sync=range_sync, cancellation=cancellation
            )
        return source.fetch_messages(target, should_fetch, sync=None, cancellation=cancellation)

    def _raise_if_cancelled(
        self, run_id: str, cancellation: Cancellation = NO_CANCELLATION
    ) -> None:
        cancellation.checkpoint()
        if self._shutdown_requested.is_set():
            raise RunCancelled("Mail processing is shutting down.")
        if self.operations.run_status(run_id) == "cancelled":
            raise RunCancelled("The range run was cancelled.")

    def _release_discarded(
        self,
        source_id: str,
        scope_key: str,
        sync: SyncSession | None,
        processed: set[str],
        run_id: str,
        scope: MessageScope,
    ) -> None:
        if sync is None:
            return
        self.discovery.discard_pending(
            source_id,
            scope_key,
            sync.discarded_ids,
            run_id=run_id,
            processing_namespace=scope.processing_namespace,
        )
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
            self.discovery.pause_scope(mailbox.id, scope_key, error)
            raise RuntimeError(error)

    def _mark_missing(self, reserved: dict[str, str], processed: set[str], error: str) -> int:
        missing = [
            intake_id for remote_id, intake_id in reserved.items() if remote_id not in processed
        ]
        for intake_id in missing:
            if not self.discovery.mark_intake_error(intake_id, error):
                raise RunCancelled("The range run was cancelled.")
        return len(missing)

    def _mark_gmail_labels_baselined(
        self, source_id: str, labels: list[str], scope: MessageScope, sync: SyncSession
    ) -> None:
        for label in labels or ["*"]:
            self.discovery.finish_scope(
                source_id,
                "gmail-label:" + label,
                scope.processing_namespace,
                scope.synchronization_namespace,
                sync.next_cursor or "",
            )

    def _finish_gmail_label_baselines(
        self,
        mailbox: Mailbox,
        scope: MessageScope,
        sync: SyncSession,
        baseline: bool,
        new_labels: list[str],
    ) -> None:
        if baseline or new_labels:
            labels = mailbox.folders if baseline else new_labels
            self._mark_gmail_labels_baselined(mailbox.id, labels, scope, sync)

    def _baseline_new_gmail_labels(
        self,
        account: Account,
        mailbox: Mailbox,
        target: MailTarget,
        source,
        settings: Settings,
        result: AccountRunResult,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> tuple[set[str], list[str]]:
        if self.discovery.scope(mailbox.id, "gmail-label:*"):
            return set(), []
        recheck_ids: set[str] = set()
        pending_labels: list[str] = []
        remote_access = self._remote_access(account, settings, cancellation)
        for label in mailbox.folders or ["*"]:
            cancellation.checkpoint()
            if self.discovery.scope(mailbox.id, "gmail-label:" + label):
                continue
            selected = () if label == "*" else (label,)
            baseline_target = MailTarget(account, mailbox, "", selected)
            sync = SyncSession(
                cursor_for=lambda _namespace: None,
                recheck_ids_for=lambda _namespace: set(),
                baseline=True,
            )

            def collect_existing(scope: MessageScope, remote_id: str) -> bool:
                remote_access.checkpoint()
                result.checked += 1
                if mailbox.archive_existing_messages:
                    recheck_ids.add(remote_id)
                else:
                    self.discovery.baseline_message(
                        mailbox.id, _message_key(account, scope, remote_id)
                    )
                    result.skipped_existing += 1
                return False

            messages = None
            try:
                self.require_account_action(
                    account, settings, AccountAction.RETRY_REMOTE, inspect=True
                )
                scope, messages = source.fetch_messages(
                    baseline_target, collect_existing, sync=sync, cancellation=remote_access
                )
                for _ in messages:
                    pass
                cancellation.checkpoint()
                if sync.next_cursor is None:
                    raise RuntimeError("Gmail did not complete the new label baseline.")
                if mailbox.archive_existing_messages:
                    pending_labels.append(label)
                else:
                    self._mark_gmail_labels_baselined(
                        mailbox.id, [label] if label != "*" else [], scope, sync
                    )
            except ProcessingStopped:
                if messages is not None:
                    self._close_messages(messages)
                raise
            except Exception as exc:
                if messages is not None:
                    self._close_messages(messages)
                cancellation.checkpoint()
                self._event(
                    EventLevel.ERROR, f"Could not baseline Gmail label {label}: {exc}", account
                )
                if isinstance(exc, CredentialError):
                    raise
                raise RuntimeError(f"Could not baseline Gmail label {label}: {exc}") from exc
        return recheck_ids, pending_labels

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
        selected_rule_id: str | None,
    ) -> bool:
        key = _message_key(account, scope, remote_id)
        if kind == "automatic" and (mailbox.id, key) in self._retried_automatic_messages:
            return False
        result.checked += 1
        if baseline:
            self.discovery.baseline_message(mailbox.id, key)
            result.skipped_existing += 1
            return False
        intake_id = self.discovery.reserve(
            mailbox.id,
            key,
            run_id,
            automatic=kind == "automatic",
            scope_key=scope_key,
            remote_id=remote_id,
            force_retry=force_retry,
        )
        if intake_id is None:
            if kind == "automatic" and self.discovery.intake_retry_deferred(mailbox.id, key):
                return False
            result.already_processed += 1
            return False
        snapshot = self.discovery.intake_snapshot(intake_id)
        try:
            self.engine.require_rules(
                self._candidate_rules(snapshot, selected_rule_id), account_id=account.id
            )
        except ValueError as exc:
            self.discovery.mark_intake_error(intake_id, str(exc))
            raise
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
        progress: SyncSession | RangePagination,
        selected_rule_id: str | None,
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
                selected_rule_id,
            )
        except IntakeOwnerDeferred:
            progress.discovery_deferred = True
            if isinstance(progress, RangePagination):
                result._deferred_sources.add(mailbox.id)
            return False
        except RunNotActiveError as exc:
            raise RunCancelled("The range run was cancelled.") from exc

    def _finish_target_scope(
        self, source_id: str, scope_key: str, scope: MessageScope, sync: SyncSession
    ) -> None:
        if sync.next_cursor is None:
            raise RuntimeError("The mail provider did not complete its discovery cursor.")
        self.discovery.finish_scope(
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
        *,
        settings: Settings,
        selected_rule_id: str | None = None,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        staged = None
        received = None
        try:
            cancellation.checkpoint()
            if remote.error is not None:
                raise remote.error
            received = _utc(remote.received_at)
            if kind == "manual" and ((start and received < start) or (end and received >= end)):
                self.discovery.mark_filtered(
                    intake_id,
                    received_at=received.isoformat(),
                    received_origin=remote.received_origin,
                )
                return
            snapshot = self.discovery.intake_snapshot(intake_id)
            owner_id = next(
                (
                    old_account.id
                    for old_account in snapshot.accounts
                    for old_mailbox in old_account.mailboxes
                    if old_mailbox.id == source_id
                ),
                account.id,
            )
            rules = self._candidate_rules(snapshot, selected_rule_id)
            if self._reject_from_headers(
                remote, intake_id, received, rules, owner_id, cancellation
            ):
                result.unmatched += 1
                return
            staged = self.engine.stage(remote, cancellation=cancellation)
            mail = staged.mail
            rule = select_rule(rules, mail, account_id=owner_id)
            cancellation.checkpoint()
            if rule is None:
                self.discovery.mark_unmatched(
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
            self._execute_accepted_plan(
                plan_id, mail.subject, account, result, cancellation=cancellation
            )
        except RemoteMessageOutsideScope:
            self._discard_staged(staged)
            cancellation.checkpoint()
            self.discovery.release_intake(intake_id)
        except ProcessingStopped:
            self._discard_staged(staged)
            raise
        except IntakeCapacityError as exc:
            self._discard_staged(staged)
            cancellation.checkpoint()
            self._handle_capacity_failure(remote, intake_id, account, result, received, exc)
        except RemoteMessageNamespaceChanged as exc:
            self._discard_staged(staged)
            cancellation.checkpoint()
            self.discovery.pause_intake_scope(intake_id, str(exc))
            if not self.discovery.mark_intake_error(
                intake_id,
                str(exc),
                received_at=received.isoformat() if received is not None else None,
                received_origin=remote.received_origin if received is not None else None,
            ):
                raise RunCancelled("The range run was cancelled.") from exc
            raise
        except RemoteMessageUnavailable as exc:
            self._discard_staged(staged)
            cancellation.checkpoint()
            if not self.discovery.mark_intake_error(
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
            self._discard_staged(staged)
            cancellation.checkpoint()
            if not self.discovery.mark_intake_error(
                intake_id,
                str(exc),
                received_at=received.isoformat() if received is not None else None,
                received_origin=remote.received_origin if received is not None else None,
            ):
                raise RunCancelled("The range run was cancelled.") from exc
            result._record_credential_failure(exc)
            if (
                result._credentials_failed
                or isinstance(exc, ScanWideProviderError)
                or not self.account_status(account, settings, inspect=True).allows(
                    AccountAction.RETRY_REMOTE
                )
            ):
                raise
            result.failed += 1
            result.errors.append(f"{remote.id}: {exc}")
            self._event(EventLevel.ERROR, f"Could not accept message {remote.id}: {exc}", account)
        finally:
            remote.release_resources()

    @staticmethod
    def _candidate_rules(snapshot: Settings, selected_rule_id: str | None) -> list[Rule]:
        if selected_rule_id is None:
            return snapshot.rules
        rules = [rule for rule in snapshot.rules if rule.id == selected_rule_id]
        if not rules:
            raise RuntimeError("The selected rule is missing from the run snapshot.")
        return rules

    def _reject_from_headers(
        self,
        remote: RemoteMessage,
        intake_id: str,
        received: datetime,
        rules: list[Rule],
        account_id: str,
        cancellation: Cancellation,
    ) -> bool:
        headers = remote.headers
        if headers is None or any(
            rule_may_match_headers(rule, headers, account_id) for rule in rules
        ):
            return False
        cancellation.checkpoint()
        self.discovery.mark_unmatched(
            intake_id,
            received_at=received.isoformat(),
            received_origin=remote.received_origin,
            sender_at=_sender_time(headers.date_header or ""),
            subject=headers.subject if headers.subject is not None else "(no subject)",
        )
        return True

    @staticmethod
    def _discard_staged(staged) -> None:
        if staged is not None:
            staged.discard()

    def _execute_accepted_plan(
        self,
        plan_id: str,
        subject: str,
        account: Account,
        result: AccountRunResult,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> None:
        parent = cancellation
        cancellation = Cancellation(
            lambda: self._shutdown_requested.is_set() or parent.requested(),
            lambda: parent.message if parent.requested() else "Mail processing is shutting down.",
        )
        try:
            done, failed = self.engine.execute(plan_id, cancellation=cancellation)
        except ProcessingStopped:
            raise
        except RunNotActiveError:
            cancellation.checkpoint()
            raise
        except Exception as exc:
            cancellation.checkpoint()
            self.delivery.set_plan_error(plan_id, str(exc))
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
                for target in self.delivery.plan_targets(plan_id)
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
            if self.delivery.outputs(plan_id):
                result.already_processed += 1
            else:
                result.skipped_no_attachments += 1

    def _handle_capacity_failure(
        self,
        remote: RemoteMessage,
        intake_id: str,
        account: Account,
        result: AccountRunResult,
        received: datetime | None,
        error: IntakeCapacityError,
    ) -> None:
        received_at = received.isoformat() if received is not None else None
        received_origin = remote.received_origin if received is not None else None
        if isinstance(error, MessageTooLargeError):
            self.discovery.reject_intake(
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
        if not self.discovery.mark_intake_error(
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
        cancellation: Cancellation = NO_CANCELLATION,
        excluded_source_ids: frozenset[str] = frozenset(),
    ) -> set[str]:
        """Retry stable provider IDs before applying the current discovery filters."""
        blocked_sources: set[str] = set()
        for intake in self.discovery.pending_automatic_intakes(
            due_only=not force_retry, excluded_source_ids=excluded_source_ids
        ):
            cancellation.checkpoint()
            if self._shutdown_requested.is_set():
                break
            source_id = str(intake["source_id"])
            if (
                source_id in blocked_sources
                or _scope_is_paused(self.discovery.scope(source_id, str(intake["scope_key"])))
                or self._automatic_intake_is_covered(
                    settings, intake, selected_account_ids=selected_account_ids
                )
            ):
                continue
            account = None
            remote = None
            try:
                snapshot = self.operations.run_settings_snapshot(str(intake["run_id"]))
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
                current_result = results.get(account.id)
                if (
                    current_result is not None and current_result._credentials_failed
                ) or not self.account_status(account, settings, inspect=True).allows(
                    AccountAction.RETRY_REMOTE
                ):
                    continue
                self.engine.require_rules(snapshot.rules, account_id=account.id)
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
                    target,
                    str(intake["remote_id"]),
                    processing_namespace,
                    cancellation=self._remote_access(account, settings, cancellation),
                )
                if remote is None:
                    cancellation.checkpoint()
                    detail = (
                        f"Message {intake['remote_id']} is no longer available before "
                        "intake completed."
                    )
                    if self.discovery.mark_intake_error(str(intake["id"]), detail):
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
                    settings=settings,
                    cancellation=cancellation,
                )
            except ProcessingStopped as stopped:
                self.operations.interrupt_run(str(intake["run_id"]), str(stopped))
                raise
            except IntakeCapacityError as exc:
                cancellation.checkpoint()
                self._record_automatic_retry_error(str(intake["id"]), remote, exc)
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
                cancellation.checkpoint()
                scan_wide = is_scan_wide_error(exc)
                if self._record_or_release_retry_error(
                    intake,
                    remote,
                    exc,
                    (source_id, str(intake["message_key"])),
                ):
                    if account is not None:
                        result = results.setdefault(account.id, AccountRunResult(account.id))
                        result._record_credential_failure(exc)
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

    def _record_or_release_retry_error(
        self,
        intake: Record,
        remote: RemoteMessage | None,
        error: Exception,
        retry_key: tuple[str, str],
    ) -> bool:
        intake_id = str(intake["id"])
        if isinstance(error, RemoteMessageNamespaceChanged):
            self.discovery.pause_intake_scope(intake_id, str(error))
        if isinstance(error, RemoteMessageOutsideScope):
            self.discovery.release_intake(intake_id)
            # Current discovery may select folders that the frozen retry did not.
            self._retried_automatic_messages.discard(retry_key)
            return False
        return self._record_automatic_retry_error(intake_id, remote, error)

    def _record_automatic_retry_error(
        self, intake_id: str, remote: RemoteMessage | None, error: Exception
    ) -> bool:
        """Fetch errors belong here; _handle_remote persists processing errors itself."""
        return remote is not None or self.discovery.mark_intake_error(intake_id, str(error))

    def _automatic_intake_is_covered(
        self, settings: Settings, intake, *, selected_account_ids: set[str] | None
    ) -> bool:
        """Whether the normal current scan will reconcile this unfinished ID."""
        for account in settings.accounts:
            if not self.account_status(account, settings, inspect=True).allows(
                AccountAction.CHECK_MAIL
            ):
                continue
            if selected_account_ids is not None and account.id not in selected_account_ids:
                continue
            for mailbox in account.mailboxes:
                if mailbox.id != intake["source_id"] or not mailbox.enabled:
                    continue
                scope = self.discovery.scope(mailbox.id, str(intake["scope_key"]))
                if scope is None or not scope["baseline_done"] or scope["status"] != "active":
                    return False
                snapshot = Settings.from_dict(json.loads(intake["settings_json"]))
                if not automatic_intake_owner_matches(snapshot, account, mailbox):
                    return False
                if account.provider == MailProvider.GMAIL_API:
                    return True
                if not mailbox.folders:
                    return True
                return str(intake["scope_key"]) in {
                    folder_scope_key(account.provider, folder) for folder in mailbox.folders
                }
        return False

    def _report_open_work_errors(self) -> None:
        for plan in self.delivery.open_plans():
            if plan["error"]:
                self._event(EventLevel.ERROR, f"Open plan {plan['id']}: {plan['error']}")
            for target in self.delivery.plan_targets(plan["id"]):
                if target["status"] == "error":
                    self._event(
                        EventLevel.ERROR,
                        f"Open plan {plan['id']}, destination {target['path']}: {target['error']}",
                    )

    def resume_open(self) -> tuple[int, int]:
        with self.account_change():
            return self.engine.resume_all(force=True)

    def retry_activity(self, key: str) -> tuple[int, int]:
        """Retry an automatic accepted mail plan through its durable output record."""
        if not key.startswith("mail:"):
            raise ValueError("Select an accepted mail result to retry.")
        plan_id = key.removeprefix("mail:")
        with self.account_change():
            if not self.can_retry_mail(plan_id):
                raise ValueError("Retry manual mail through its past-mail operation.")
            done, failed, _error = self._retry_saved_plan(plan_id)
            if failed:
                self._event(
                    EventLevel.ERROR,
                    f"Archive retry failed: {failed} failure(s). See Archive activity for details.",
                )
            return done, failed

    def _retry_saved_plan(
        self, plan_id: str, *, cancellation: Cancellation = NO_CANCELLATION
    ) -> tuple[int, int, str | None]:
        """Retain local preparation failures on the same plan as background retries."""
        parent = cancellation
        cancellation = Cancellation(
            lambda: self._shutdown_requested.is_set() or parent.requested(),
            lambda: parent.message if parent.requested() else "Mail processing is shutting down.",
        )
        try:
            done, failed = self.engine.execute(plan_id, force=True, cancellation=cancellation)
            return done, failed, None
        except RunNotActiveError:
            cancellation.checkpoint()
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            cancellation.checkpoint()
            self.delivery.record_plan_failure(plan_id, str(exc))
            return 0, 1, str(exc)

    def can_retry_mail(self, plan_id: str) -> bool:
        plan = next((item for item in self.delivery.work_plans() if item["id"] == plan_id), None)
        return bool(
            plan
            and plan["status"] == "open"
            and self.operations.run_operation_id(str(plan["run_id"])) is None
        )

    def retry_waiting_operation_outputs(self, operation_id: str) -> tuple[int, int]:
        """Retry saved accepted outputs without repeating provider scans."""
        with self.account_change():
            operation = self.operations.manual_operation(operation_id)
            if operation is None or operation["status"] != "waiting":
                return 0, 0
            cancellation = Cancellation(
                lambda: not self.operations.manual_operation_accepts_outputs(operation_id),
                "The past-mail operation was stopped.",
            )
            done = failed = 0
            for plan in self.operations.manual_open_plans(operation_id):
                if not self.operations.manual_operation_accepts_outputs(operation_id):
                    break
                made, errors, _error = self._retry_saved_plan(plan["id"], cancellation=cancellation)
                done += made
                failed += errors
            return done, failed

    def automatic_source_intervals(self, settings: Settings) -> dict[str, tuple[str, int]]:
        """Freeze the account and polling interval for every source a check may use."""
        sources = {
            mailbox.id: (account.id, (account.poll_minutes or settings.default_poll_minutes) * 60)
            for account in settings.accounts
            if self.account_status(account, settings).allows(AccountAction.CHECK_MAIL)
            for mailbox in account.mailboxes
            if mailbox.enabled
        }
        for row in self.delivery.automatic_work_sources():
            source_id = str(row["source_id"])
            if source_id in sources:
                continue
            saved = Settings.from_dict(json.loads(row["settings_json"]))
            for account in saved.accounts:
                if any(mailbox.id == source_id for mailbox in account.mailboxes):
                    sources[source_id] = (
                        account.id,
                        (account.poll_minutes or saved.default_poll_minutes) * 60,
                    )
                    break
        return sources

    def has_automatic_work(
        self, settings: Settings, *, excluded_source_ids: frozenset[str] = frozenset()
    ) -> bool:
        paused_intakes: set[str] = set()
        for intake in self.discovery.pending_automatic_intakes(
            excluded_source_ids=excluded_source_ids
        ):
            source_id = str(intake["source_id"])
            if _scope_is_paused(self.discovery.scope(source_id, str(intake["scope_key"]))):
                paused_intakes.add(str(intake["id"]))
                continue
            saved = Settings.from_dict(json.loads(intake["settings_json"]))
            if not any(
                self.account_status(account, settings, inspect=True).allows(
                    AccountAction.RETRY_REMOTE
                )
                and any(mailbox.id == source_id for mailbox in account.mailboxes)
                for account in saved.accounts
            ):
                paused_intakes.add(str(intake["id"]))
        return self.delivery.automatic_work_due(
            excluded_source_ids=excluded_source_ids,
            paused_intake_ids=frozenset(paused_intakes),
        )

    def resume_range_run(self, run_id: str) -> AccountRunResult:
        """Restart an interrupted provider search with its saved rule selection."""
        operation_id = self.operations.run_operation_id(run_id)
        if operation_id is not None:
            results = self.run_range_operation(operation_id)
            return results[0] if results else AccountRunResult("")
        with self.account_change():
            run = next(
                (item for item in self.operations.incomplete_manual_runs() if item["id"] == run_id),
                None,
            )
            if run is None:
                raise ValueError("The selected range run is not resumable.")
            settings = self.operations.run_settings_snapshot(run_id)
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
            self.engine.require_rules(
                self._candidate_rules(settings, json.loads(run["selection_json"]).get("rule_id")),
                account_id=account.id,
            )
            self.require_account_action(account, settings, AccountAction.RETRY_REMOTE, inspect=True)
            selection = json.loads(run["selection_json"])
            start = (
                datetime.fromisoformat(selection["start_utc"]) if selection["start_utc"] else None
            )
            end = datetime.fromisoformat(selection["end_utc"]) if selection["end_utc"] else None
            source = self.source_registry.get(account)
            result = AccountRunResult(account.id)
            self.operations.restart_run(run_id)
            errors = []
            with self._scan_lifecycle(run_id, manual=True):
                try:
                    targets = self._saved_range_targets(
                        account, mailbox, source, selection["folders"]
                    )
                    for target in targets:
                        if self.operations.run_status(run_id) == "cancelled":
                            break
                        if not self._scan_selected_target(
                            account,
                            mailbox,
                            target,
                            source,
                            run_id,
                            "manual",
                            settings,
                            result,
                            errors,
                            force_retry=False,
                            start=start,
                            end=end,
                            selected_rule_id=selection.get("rule_id"),
                        ):
                            break
                except Exception as exc:
                    errors.append(str(exc))
                    result.failed += 1
                    result.errors.append(str(exc))
                finally:
                    unresolved = self.discovery.unresolved_intakes(run_id)
                    if unresolved and not result.failed:
                        result.failed += len(unresolved)
                    if unresolved and not errors:
                        errors.append(
                            "Some message downloads remain unresolved; this run can be resumed."
                        )
                    self.operations.finish_run(run_id, error="; ".join(errors) if errors else None)
            return result

    @staticmethod
    def _saved_range_targets(
        account: Account,
        mailbox: Mailbox,
        source,
        folders,
        *,
        cancellation: Cancellation = NO_CANCELLATION,
    ) -> list[MailTarget]:
        if (
            not isinstance(folders, list)
            or not all(isinstance(folder, str) and folder for folder in folders)
            or (account.provider != MailProvider.GMAIL_API and not folders)
        ):
            raise ValueError("The saved range folder selection is invalid.")
        selected = set(folders)
        if account.provider == MailProvider.GMAIL_API:
            return _selected_targets(
                account,
                mailbox,
                source.targets(account, mailbox, cancellation=cancellation),
                selected,
            )
        return [MailTarget(account, mailbox, folder, tuple(folders)) for folder in folders]

    def _require_independent_plan(self, plan_id: str) -> None:
        plan = next(
            (item for item in self.delivery.work_plans() if item["id"] == plan_id),
            None,
        )
        if plan is not None and self.operations.run_operation_id(str(plan["run_id"])):
            raise ValueError("Manage manual mail through its past-mail operation.")

    def abort_plan(self, plan_id: str) -> None:
        with self.account_change():
            self._require_independent_plan(plan_id)
            self.delivery.abort_plan(plan_id)

    def pause_plan(self, plan_id: str) -> None:
        with self.account_change():
            self._require_independent_plan(plan_id)
            self.delivery.pause_plan(plan_id)

    def resume_plan(self, plan_id: str) -> tuple[int, int]:
        with self.account_change():
            self._require_independent_plan(plan_id)
            self.delivery.resume_plan(plan_id)
            done, failed, error = self._retry_saved_plan(plan_id)
            if error is not None:
                self._event(EventLevel.ERROR, f"Could not resume plan {plan_id}: {error}")
            return done, failed

    def reset_scope_baseline(self, source_id: str, folder: str) -> int:
        with self.account_change():
            return self.discovery.reset_scope_baseline(source_id, folder)

    def cancel_run(self, run_id: str) -> None:
        with self.account_change():
            if self.operations.run_operation_id(run_id) is not None:
                raise ValueError("Stop the past-mail operation to cancel its scan.")
            self.operations.cancel_run(run_id)

    def cancel_intake(self, intake_id: str) -> None:
        with self.account_change():
            if self.discovery.intake_operation_id(intake_id) is not None:
                raise ValueError("Stop the past-mail operation to cancel its intake.")
            self.discovery.cancel_intake(intake_id)
