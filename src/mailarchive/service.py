from __future__ import annotations

import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from mailarchive.credentials import CredentialStore
from mailarchive.imap_client import ImapMailbox
from mailarchive.mail_identity import MailTarget, MessageScope, mailbox_namespace
from mailarchive.mail_parser import parse_mail
from mailarchive.mail_sources import MessageSourceRegistry
from mailarchive.models import Account, MailProvider, Settings
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


@dataclass(slots=True)
class AccountRunResult:
    account_id: str
    archived: int = 0
    already_processed: int = 0
    unmatched: int = 0
    skipped_unmatched: int = 0
    skipped_existing: int = 0
    failed: int = 0
    checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return self.already_processed + self.skipped_unmatched + self.skipped_existing


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

    def relocate_state_database(self, database_path: Path) -> ArchiveState:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError(
                "The SQLite database cannot be changed while an archive run is in progress."
            )
        try:
            self.state = self.state.migrated_to(database_path)
            return self.state
        finally:
            self._run_lock.release()

    def _event(self, level: EventLevel, message: str, account: Account | None = None) -> None:
        self.event_handler(
            ServiceEvent(level, message, account.id if account else None, datetime.now())
        )

    def run_once(
        self, settings: Settings, account_ids: set[str] | None = None
    ) -> list[AccountRunResult]:
        if not self._run_lock.acquire(blocking=False):
            self._event(EventLevel.WARNING, "An archive run is already in progress.")
            return []
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
                result.errors.extend(target_result.errors)
                for name in (
                    "archived",
                    "already_processed",
                    "unmatched",
                    "skipped_unmatched",
                    "skipped_existing",
                    "failed",
                    "checked",
                ):
                    setattr(result, name, getattr(result, name) + getattr(target_result, name))
            if not mailbox_errors:
                self.state.complete_initial_scan(account.id, namespace, set())
            self.state.finish_mailbox_check(
                account.id, namespace, error="; ".join(mailbox_errors) if mailbox_errors else None
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
        mailbox = target.mailbox
        result = AccountRunResult(account_id=account.id)
        try:
            upgrading_imap = (
                account.provider == MailProvider.GENERIC_IMAP
                and self.state.needs_imap_namespace_upgrade(account.id)
            )
            if upgrading_imap:
                if mailbox.archive_existing_messages:
                    level = EventLevel.WARNING
                    upgrade_message = (
                        "One-time recheck after an IMAP history upgrade. "
                        "Old records do not identify the mailbox folder. Archiving existing "
                        "mail is enabled, so existing messages may be archived again."
                    )
                else:
                    level = EventLevel.INFO
                    upgrade_message = (
                        "IMAP history upgrade: old records do not identify the mailbox folder. "
                        "Archiving existing mail is disabled. This check establishes a new "
                        "starting point without downloading existing messages; "
                        "later checks archive newly received mail."
                    )
                self._event(
                    level,
                    f"{target.label}: {upgrade_message}",
                    account,
                )
            # Use the same rule snapshot for download filtering and message evaluation.
            rules = deepcopy(settings.rules)
            rules_fingerprint = matching_rules_fingerprint(rules, account.id)
            initial_scan_by_namespace: dict[str, bool] = {}
            skipped_by_namespace: dict[str, set[str]] = {}
            last_progress_at = float("-inf")

            def report_progress(phase: str) -> None:
                nonlocal last_progress_at
                now = time.monotonic()
                if now - last_progress_at >= 0.25:
                    last_progress_at = now
                    self.progress_handler(
                        RunProgress(
                            f"{target.label}: {phase} — {result.checked} checked, "
                            f"{result.archived} archived, {result.skipped} skipped, "
                            f"{result.unmatched} unmatched, {result.failed} failed."
                        )
                    )

            def should_fetch(scope: MessageScope, message_id: str) -> bool:
                source_namespace = scope.processing_namespace
                result.checked += 1
                if source_namespace not in processed_by_namespace:
                    processed_by_namespace[source_namespace] = self.state.processed_message_ids(
                        account.id,
                        source_namespace,
                        include_skipped=not mailbox.archive_existing_messages,
                    )
                    unmatched_by_namespace[source_namespace] = self.state.unmatched_message_ids(
                        account.id, source_namespace, rules_fingerprint
                    )
                    initial_scan_by_namespace[source_namespace] = mailbox_initial or (
                        account.provider == MailProvider.GENERIC_IMAP
                        and not self.state.has_completed_initial_scan(account.id, source_namespace)
                    )
                initial_scan_by_namespace.setdefault(source_namespace, mailbox_initial)
                processed = processed_by_namespace[source_namespace]
                if message_id in processed:
                    result.already_processed += 1
                    report_progress("Checking messages")
                    return False
                if message_id in unmatched_by_namespace[source_namespace]:
                    result.skipped_unmatched += 1
                    report_progress("Checking messages")
                    return False
                if (
                    initial_scan_by_namespace[source_namespace]
                    and not mailbox.archive_existing_messages
                ):
                    skipped_by_namespace.setdefault(source_namespace, set()).add(message_id)
                    processed.add(message_id)
                    result.skipped_existing += 1
                    report_progress("Checking messages")
                    return False
                report_progress(f"Downloading email {result.checked}")
                return True

            self.progress_handler(
                RunProgress(f"{target.label}: Connecting and loading the message list...")
            )
            identity = repr(
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

            def recheck_ids(namespace: str) -> set[str]:
                if account.provider == MailProvider.MICROSOFT_GRAPH:
                    if namespace in recheck_claimed:
                        return set()
                    recheck_claimed.add(namespace)
                return self.state.recheck_message_ids(
                    account.id,
                    namespace,
                    rules_fingerprint,
                    include_existing=mailbox.archive_existing_messages,
                )

            sync = SyncSession(
                cursor_for=lambda namespace: self.state.sync_cursor(
                    account.id, namespace, identity
                ),
                recheck_ids_for=recheck_ids,
                report_reset=lambda: self._event(
                    EventLevel.INFO,
                    f"{target.label}: Synchronization token expired; "
                    "rechecking the message list with existing processing history.",
                    account,
                ),
            )
            source = self.source_registry.get(account)
            scope, remote_messages = source.fetch_messages(target, should_fetch, sync=sync)
            source_namespace = scope.processing_namespace
            initial_scan = mailbox_initial or (
                account.provider == MailProvider.GENERIC_IMAP
                and not self.state.has_completed_initial_scan(account.id, source_namespace)
            )
            for remote in remote_messages:
                try:
                    report_progress("Archiving messages")
                    mail = parse_mail(remote.raw)
                    rule = select_rule(rules, mail, account_id=account.id)
                    if rule is None:
                        self.state.record_unmatched(
                            account.id, source_namespace, remote.id, rules_fingerprint
                        )
                        unmatched_by_namespace.setdefault(source_namespace, set()).add(remote.id)
                        result.unmatched += 1
                        continue
                    archive_result = storage.archive(mail, rule)
                    self.state.record(
                        account.id,
                        source_namespace,
                        remote.id,
                        mail,
                        rule,
                        archive_result,
                    )
                    processed_by_namespace.setdefault(source_namespace, set()).add(remote.id)
                    result.archived += 1
                except Exception as exc:
                    result.failed += 1
                    result.errors.append(str(exc))
                    self._event(
                        EventLevel.WARNING,
                        f"{target.label}: A message could not be archived: {exc}",
                        account,
                    )
            if not result.failed:
                if sync.next_cursor is None:
                    raise RuntimeError(
                        "The mail provider did not complete synchronization with a cursor."
                    )
                self.state.complete_scan(
                    account.id,
                    source_namespace,
                    skipped_message_ids=(
                        skipped_by_namespace.get(source_namespace, set()) if initial_scan else None
                    ),
                    cursor=sync.next_cursor,
                    identity=identity,
                    discarded_ids=sync.discarded_ids,
                    present_ids=sync.present_ids,
                    synchronization_namespace=scope.synchronization_namespace,
                    initialize=account.provider == MailProvider.GENERIC_IMAP,
                )
            if result.failed:
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
