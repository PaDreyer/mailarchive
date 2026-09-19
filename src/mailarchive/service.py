from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from mailarchive.credentials import CredentialStore
from mailarchive.imap_client import ImapMailbox, RemoteMessage
from mailarchive.mail_identity import MailTarget, MessageScope, mailbox_namespace
from mailarchive.mail_parser import parse_mail
from mailarchive.mail_sources import MessageSourceRegistry
from mailarchive.models import Account, MailProvider, Rule, Settings
from mailarchive.rules import matching_rules_fingerprint, select_rule
from mailarchive.storage import ArchiveState, ArchiveStorage
from mailarchive.synchronization import SyncSession


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
    """Transient run status, kept out of the persistent activity log."""

    message: str
    active: bool = True


class ArchiveRunBusyError(RuntimeError):
    """An archive run could not start because another operation owns the service."""


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
        return (
            self.already_processed
            + self.skipped_unmatched
            + self.skipped_existing
            + self.skipped_no_attachments
        )

    def add(self, other: AccountRunResult) -> None:
        self.archived += other.archived
        self.already_processed += other.already_processed
        self.unmatched += other.unmatched
        self.skipped_unmatched += other.skipped_unmatched
        self.skipped_existing += other.skipped_existing
        self.skipped_no_attachments += other.skipped_no_attachments
        self.failed += other.failed
        self.checked += other.checked
        self.errors.extend(other.errors)


class ArchiveService:
    def __init__(
        self,
        credential_store: CredentialStore,
        state: ArchiveState,
        event_handler: Callable[[ServiceEvent], None] | None = None,
        mailbox: ImapMailbox | None = None,
        source_registry: MessageSourceRegistry | None = None,
        progress_handler: Callable[[RunProgress], None] | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.state = state
        self.event_handler = event_handler or (lambda event: None)
        self.progress_handler = progress_handler or (lambda progress: None)
        self.source_registry = source_registry or MessageSourceRegistry(
            credential_store,
            imap_mailbox=mailbox,
        )
        self._run_lock = threading.Lock()

    @contextmanager
    def account_change(self) -> Iterator[None]:
        """Keep account edits and archive-run snapshots mutually exclusive."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError(
                "Email accounts cannot be changed while an archive run is in progress. "
                "Try again after it finishes."
            )
        try:
            yield
        finally:
            self._run_lock.release()

    @contextmanager
    def state_database_change(self) -> Iterator[Callable[[Path], ArchiveState]]:
        """Keep relocation, configuration commit, and rollback exclusive with runs."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError(
                "The SQLite database cannot be changed while an archive run is in progress."
            )
        try:
            yield self._relocate_state_database
        finally:
            self._run_lock.release()

    def relocate_state_database(self, database_path: Path) -> ArchiveState:
        with self.state_database_change() as relocate:
            return relocate(database_path)

    def _relocate_state_database(self, database_path: Path) -> ArchiveState:
        # Only exposed by state_database_change while it holds the run lock.
        self.state = self.state.migrated_to(database_path)
        return self.state

    def _event(self, level: EventLevel, message: str, account: Account | None = None) -> None:
        self.event_handler(
            ServiceEvent(level, message, account.id if account else None, datetime.now())
        )

    def run_once(
        self, settings: Settings, account_ids: set[str] | None = None
    ) -> list[AccountRunResult]:
        if not self._run_lock.acquire(blocking=False):
            message = "The archive service is busy with another run or settings change."
            self._event(EventLevel.WARNING, message)
            raise ArchiveRunBusyError(message)
        results: list[AccountRunResult] = []
        finished = False
        try:
            self.progress_handler(RunProgress("Starting archive run..."))
            settings = deepcopy(settings)
            accounts = [
                account
                for account in settings.accounts
                if account.enabled and (account_ids is None or account.id in account_ids)
            ]
            if not accounts:
                self._event(EventLevel.INFO, "No active email account is configured.")
                finished = True
                return results
            storage = ArchiveStorage(Path(settings.archive_root))
            for account in accounts:
                results.append(self._run_account(account, settings, storage))
            finished = True
            return results
        finally:
            self._run_lock.release()
            if not finished:
                message = "Archive run stopped before completion."
            elif not results:
                message = "No active email account is configured."
            else:
                message = (
                    f"Finished: {sum(result.checked for result in results)} checked, "
                    f"{sum(result.archived for result in results)} archived, "
                    f"{sum(result.skipped for result in results)} skipped, "
                    f"{sum(result.unmatched for result in results)} unmatched, "
                    f"{sum(result.failed for result in results)} failed."
                )
            self.progress_handler(RunProgress(message, active=False))

    def _run_account(
        self, account: Account, settings: Settings, storage: ArchiveStorage
    ) -> AccountRunResult:
        result = AccountRunResult(account_id=account.id)
        self._event(EventLevel.INFO, f"{account.label}: Check started.", account)
        self.state.upgrade_mailbox_history(account)
        processed: dict[str, set[str]] = {}
        unmatched: dict[str, set[str]] = {}
        for mailbox in account.mailboxes:
            if not mailbox.enabled:
                continue
            namespace = mailbox_namespace(account, mailbox)
            self.state.begin_mailbox_check(account.id, namespace)
            initial = not self.state.has_completed_initial_scan(account.id, namespace)
            try:
                targets = self.source_registry.targets(account, mailbox)
                if not targets:
                    raise RuntimeError("The mailbox has no selectable folders.")
            except Exception as exc:
                result.failed += 1
                result.errors.append(str(exc))
                self.state.finish_mailbox_check(account.id, namespace, error=str(exc))
                self._event(
                    EventLevel.ERROR,
                    f"{account.label}: {mailbox.address}: Could not load mailbox folders: {exc}",
                    account,
                )
                continue
            mailbox_errors: list[str] = []
            recheck_claimed: set[str] = set()
            for target in targets:
                target_result = self._run_target(
                    target, settings, storage, initial, processed, unmatched, recheck_claimed
                )
                mailbox_errors.extend(target_result.errors)
                result.add(target_result)
            if not mailbox_errors:
                self.state.complete_initial_scan(account.id, namespace, set())
            self.state.finish_mailbox_check(
                account.id, namespace, error="; ".join(mailbox_errors) if mailbox_errors else None
            )
        if result.skipped_no_attachments:
            self._event(
                EventLevel.INFO,
                f"{account.label}: {result.skipped_no_attachments} email(s) skipped: "
                "no attachments found for the matching Attachments only rule.",
                account,
            )
        if not result.failed:
            if result.unmatched:
                self._event(
                    EventLevel.WARNING,
                    f"{account.label}: {result.archived} archived, "
                    f"{result.unmatched} without a matching rule in this check.",
                    account,
                )
            elif result.skipped_existing:
                self._event(
                    EventLevel.SUCCESS,
                    f"{account.label}: Ready for new mail; "
                    f"{result.skipped_existing} existing email(s) skipped.",
                    account,
                )
            else:
                self._event(
                    EventLevel.SUCCESS,
                    f"{account.label}: {result.archived} new email(s) archived.",
                    account,
                )
        return result

    def _run_target(
        self,
        target: MailTarget,
        settings: Settings,
        storage: ArchiveStorage,
        mailbox_initial: bool,
        processed_by_namespace: dict[str, set[str]],
        unmatched_by_namespace: dict[str, set[str]],
        recheck_claimed: set[str],
    ) -> AccountRunResult:
        account = target.account
        result = AccountRunResult(account_id=account.id)
        try:
            self._warn_imap_history_upgrade(target)
            processing = _TargetProcessing(
                state=self.state,
                target=target,
                storage=storage,
                rules=deepcopy(settings.rules),
                result=result,
                mailbox_initial=mailbox_initial,
                processed_by_namespace=processed_by_namespace,
                unmatched_by_namespace=unmatched_by_namespace,
                recheck_claimed=recheck_claimed,
                event_handler=self.event_handler,
                progress_handler=self.progress_handler,
            )
            self.progress_handler(
                RunProgress(f"{target.label}: Connecting and loading the message list...")
            )
            sync = processing.sync_session()
            source = self.source_registry.get(account)
            scope, messages = source.fetch_messages(target, processing.should_fetch, sync=sync)
            initial_scan = processing.initial_scan(scope.processing_namespace)
            processing.archive_messages(messages, scope.processing_namespace)
            if not result.failed:
                processing.complete_scan(scope, sync, initial_scan=initial_scan)
            else:
                self._event(
                    EventLevel.ERROR,
                    f"{target.label}: {result.archived} archived, {result.failed} failed.",
                    account,
                )
        except Exception as exc:
            result.failed += 1
            result.errors.append(str(exc))
            self._event(EventLevel.ERROR, f"{target.label}: Check failed: {exc}", account)
        return result

    def _warn_imap_history_upgrade(self, target: MailTarget) -> None:
        account = target.account
        if (
            account.provider != MailProvider.GENERIC_IMAP
            or not self.state.needs_imap_namespace_upgrade(account.id)
        ):
            return
        if target.mailbox.archive_existing_messages:
            level = EventLevel.WARNING
            message = (
                "One-time recheck after an IMAP history upgrade. "
                "Old records do not identify the mailbox folder. Archiving existing "
                "mail is enabled, so existing messages may be archived again."
            )
        else:
            level = EventLevel.INFO
            message = (
                "IMAP history upgrade: old records do not identify the mailbox folder. "
                "Archiving existing mail is disabled. This check establishes a new "
                "starting point without downloading existing messages; "
                "later checks archive newly received mail."
            )
        self._event(level, f"{target.label}: {message}", account)


@dataclass(slots=True)
class _TargetProcessing:
    """Keep filtering, archiving, and checkpoint state in one technical scope."""

    state: ArchiveState
    target: MailTarget
    storage: ArchiveStorage
    rules: list[Rule]
    result: AccountRunResult
    mailbox_initial: bool
    processed_by_namespace: dict[str, set[str]]
    unmatched_by_namespace: dict[str, set[str]]
    recheck_claimed: set[str]
    event_handler: Callable[[ServiceEvent], None]
    progress_handler: Callable[[RunProgress], None]
    rules_fingerprint: str = field(init=False)
    identity: str = field(init=False)
    initial_scan_by_namespace: dict[str, bool] = field(default_factory=dict)
    skipped_by_namespace: dict[str, set[str]] = field(default_factory=dict)
    last_progress_at: float = float("-inf")

    def __post_init__(self) -> None:
        account, mailbox = self.target.account, self.target.mailbox
        # Filtering and evaluation share this run's rule snapshot.
        self.rules_fingerprint = matching_rules_fingerprint(self.rules, account.id)
        self.identity = repr(
            (
                account.provider.value,
                account.auth_mode.value,
                account.username,
                account.client_id,
                account.tenant_id,
                mailbox.address.strip().casefold(),
                tuple(sorted(mailbox.folders)),
            )
        )

    def initial_scan(self, namespace: str) -> bool:
        account = self.target.account
        return self.mailbox_initial or (
            account.provider == MailProvider.GENERIC_IMAP
            and not self.state.has_completed_initial_scan(account.id, namespace)
        )

    def should_fetch(self, scope: MessageScope, message_id: str) -> bool:
        account, mailbox = self.target.account, self.target.mailbox
        namespace = scope.processing_namespace
        self.result.checked += 1
        if namespace not in self.processed_by_namespace:
            self.processed_by_namespace[namespace] = self.state.processed_message_ids(
                account.id, namespace, include_skipped=not mailbox.archive_existing_messages
            )
            self.unmatched_by_namespace[namespace] = self.state.unmatched_message_ids(
                account.id, namespace, self.rules_fingerprint
            )
            self.initial_scan_by_namespace[namespace] = self.initial_scan(namespace)
        self.initial_scan_by_namespace.setdefault(namespace, self.mailbox_initial)
        processed = self.processed_by_namespace[namespace]
        if message_id in processed:
            self.result.already_processed += 1
        elif message_id in self.unmatched_by_namespace[namespace]:
            self.result.skipped_unmatched += 1
        elif self.initial_scan_by_namespace[namespace] and not mailbox.archive_existing_messages:
            self.skipped_by_namespace.setdefault(namespace, set()).add(message_id)
            processed.add(message_id)
            self.result.skipped_existing += 1
        else:
            self.report_progress(f"Downloading email {self.result.checked}")
            return True
        self.report_progress("Checking messages")
        return False

    def report_progress(self, phase: str) -> None:
        now = time.monotonic()
        if now - self.last_progress_at < 0.25:
            return
        self.last_progress_at = now
        result = self.result
        self.progress_handler(
            RunProgress(
                f"{self.target.label}: {phase} — {result.checked} checked, "
                f"{result.archived} archived, {result.skipped} skipped, "
                f"{result.unmatched} unmatched, {result.failed} failed."
            )
        )

    def archive_messages(self, messages: Iterator[RemoteMessage], namespace: str) -> None:
        for remote in messages:
            try:
                self.report_progress("Archiving messages")
                self._archive_message(remote, namespace)
            except Exception as exc:
                self.result.failed += 1
                self.result.errors.append(str(exc))
                self._event(
                    EventLevel.WARNING,
                    f"{self.target.label}: A message could not be archived: {exc}",
                )

    def _archive_message(self, remote: RemoteMessage, namespace: str) -> None:
        account = self.target.account
        mail = parse_mail(remote.raw)
        rule = select_rule(self.rules, mail, account_id=account.id)
        if rule is None:
            self.state.record_unmatched(account.id, namespace, remote.id, self.rules_fingerprint)
            self.unmatched_by_namespace.setdefault(namespace, set()).add(remote.id)
            self.result.unmatched += 1
            return
        archive_result = self.storage.archive(mail, rule)
        self.state.record(account.id, namespace, remote.id, mail, rule, archive_result)
        self.processed_by_namespace.setdefault(namespace, set()).add(remote.id)
        if archive_result.files:
            self.result.archived += 1
        else:
            self.result.skipped_no_attachments += 1

    def complete_scan(self, scope: MessageScope, sync: SyncSession, *, initial_scan: bool) -> None:
        if sync.next_cursor is None:
            raise RuntimeError("The mail provider did not complete synchronization with a cursor.")
        self.state.complete_scan(
            self.target.account.id,
            scope.processing_namespace,
            skipped_message_ids=(
                self.skipped_by_namespace.get(scope.processing_namespace, set())
                if initial_scan
                else None
            ),
            cursor=sync.next_cursor,
            identity=self.identity,
            discarded_ids=sync.discarded_ids,
            present_ids=sync.present_ids,
            synchronization_namespace=scope.synchronization_namespace,
            initialize=self.target.account.provider == MailProvider.GENERIC_IMAP,
        )

    def sync_session(self) -> SyncSession:
        return SyncSession(
            cursor_for=self._cursor,
            recheck_ids_for=self._recheck_ids,
            report_reset=self._report_reset,
        )

    def _cursor(self, namespace: str) -> str | None:
        return self.state.sync_cursor(self.target.account.id, namespace, self.identity)

    def _recheck_ids(self, namespace: str) -> set[str]:
        account = self.target.account
        if account.provider == MailProvider.MICROSOFT_GRAPH:
            if namespace in self.recheck_claimed:
                return set()
            self.recheck_claimed.add(namespace)
        return self.state.recheck_message_ids(
            account.id,
            namespace,
            self.rules_fingerprint,
            include_existing=self.target.mailbox.archive_existing_messages,
        )

    def _report_reset(self) -> None:
        self._event(
            EventLevel.INFO,
            f"{self.target.label}: Synchronization token expired; "
            "rechecking the message list with existing processing history.",
        )

    def _event(self, level: EventLevel, message: str) -> None:
        self.event_handler(ServiceEvent(level, message, self.target.account.id, datetime.now()))
