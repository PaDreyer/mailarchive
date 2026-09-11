from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from mailarchive.credentials import CredentialStore
from mailarchive.imap_client import ImapMailbox
from mailarchive.mail_parser import parse_mail
from mailarchive.mail_sources import MessageSourceRegistry
from mailarchive.models import Account, Settings
from mailarchive.rules import select_rule
from mailarchive.storage import ArchiveState, ArchiveStorage


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
class AccountRunResult:
    account_id: str
    archived: int = 0
    already_processed: int = 0
    unmatched: int = 0
    skipped_existing: int = 0
    failed: int = 0


class ArchiveService:
    def __init__(
        self,
        credential_store: CredentialStore,
        state: ArchiveState,
        event_handler: Callable[[ServiceEvent], None] | None = None,
        mailbox: ImapMailbox | None = None,
        source_registry: MessageSourceRegistry | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.state = state
        self.event_handler = event_handler or (lambda event: None)
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
        try:
            results: list[AccountRunResult] = []
            accounts = [
                account
                for account in settings.accounts
                if account.enabled and (account_ids is None or account.id in account_ids)
            ]
            if not accounts:
                self._event(EventLevel.INFO, "No active email account is configured.")
                return results
            storage = ArchiveStorage(Path(settings.archive_root))
            for account in accounts:
                results.append(self._run_account(account, settings, storage))
            return results
        finally:
            self._run_lock.release()

    def _run_account(
        self, account: Account, settings: Settings, storage: ArchiveStorage
    ) -> AccountRunResult:
        result = AccountRunResult(account_id=account.id)
        self._event(EventLevel.INFO, f"{account.label}: Check started.", account)
        try:
            processed_by_namespace: dict[str, set[str]] = {}
            initial_scan_by_namespace: dict[str, bool] = {}
            skipped_by_namespace: dict[str, set[str]] = {}

            def should_fetch(source_namespace: str, message_id: str) -> bool:
                if source_namespace not in processed_by_namespace:
                    processed_by_namespace[source_namespace] = self.state.processed_message_ids(
                        account.id,
                        source_namespace,
                        include_skipped=not settings.archive_existing_messages,
                    )
                    initial_scan_by_namespace[
                        source_namespace
                    ] = not self.state.has_completed_initial_scan(account.id, source_namespace)
                processed = processed_by_namespace[source_namespace]
                if message_id in processed:
                    result.already_processed += 1
                    return False
                if (
                    initial_scan_by_namespace[source_namespace]
                    and not settings.archive_existing_messages
                ):
                    skipped_by_namespace.setdefault(source_namespace, set()).add(message_id)
                    result.skipped_existing += 1
                    return False
                return True

            source = self.source_registry.get(account)
            source_namespace, remote_messages = source.fetch_messages(account, should_fetch)
            initial_scan = not self.state.has_completed_initial_scan(account.id, source_namespace)
            for remote in remote_messages:
                try:
                    mail = parse_mail(remote.raw)
                    rule = select_rule(settings.rules, mail)
                    if rule is None:
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
                    result.archived += 1
                except Exception as exc:
                    result.failed += 1
                    self._event(
                        EventLevel.WARNING,
                        f"{account.label}: A message could not be archived: {exc}",
                        account,
                    )
            if initial_scan:
                self.state.complete_initial_scan(
                    account.id,
                    source_namespace,
                    skipped_by_namespace.get(source_namespace, set()),
                )
            if result.failed:
                self._event(
                    EventLevel.ERROR,
                    f"{account.label}: {result.archived} archived, {result.failed} failed.",
                    account,
                )
            elif result.unmatched:
                self._event(
                    EventLevel.WARNING,
                    f"{account.label}: {result.archived} archived, "
                    f"{result.unmatched} without a matching rule.",
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
        except Exception as exc:
            result.failed += 1
            self._event(EventLevel.ERROR, f"{account.label}: Check failed: {exc}", account)
        return result
