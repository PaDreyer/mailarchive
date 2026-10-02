"""User actions and the lifetime of the currently selected profile."""

from __future__ import annotations

import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    account_credential_lock,
    store_account_credentials,
)
from mailarchive.application.background import BackgroundResult, BackgroundTasks
from mailarchive.application.credential_port import CredentialStore
from mailarchive.application.events import EventLevel, RunProgress, ServiceEvent
from mailarchive.application.profile import ProfileManager
from mailarchive.domain.configuration import Rule, Settings

T = TypeVar("T")


class MailArchiveApplication:
    def __init__(
        self,
        profiles: ProfileManager,
        credentials: CredentialStore,
        *,
        authorize: Callable,
        configure_startup: Callable[[bool], None],
        update_check: Callable,
        service_account_reader: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._profiles = profiles
        self._credentials = credentials
        self._authorize = authorize
        self._configure_startup = configure_startup
        self._update_check = update_check
        self._service_account_reader = service_account_reader
        self._lock = threading.RLock()
        self._background = BackgroundTasks()
        self._authorizations: dict[str, threading.Event] = {}
        self._on_event: Callable[[ServiceEvent], None] = lambda event: None
        self._on_progress: Callable[[RunProgress], None] = lambda progress: None
        self._started = False
        self._closing = False
        self._switching = False
        self._recovery_thread: threading.Thread | None = None
        self._context = profiles.open(profiles.path, self._receive_event, self._receive_progress)

    @property
    def settings(self) -> Settings:
        with self._lock:
            return deepcopy(self._context.settings)

    @property
    def database_path(self) -> Path:
        return self._context.database_path

    @property
    def authorizing_account_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._authorizations)

    def set_observers(
        self,
        on_event: Callable[[ServiceEvent], None],
        on_progress: Callable[[RunProgress], None],
    ) -> None:
        self._on_event = on_event
        self._on_progress = on_progress

    def _receive_event(self, event: ServiceEvent) -> None:
        self._on_event(event)

    def _receive_progress(self, progress: RunProgress) -> None:
        self._on_progress(progress)

    def _ensure_available(self) -> None:
        if self._closing or self._switching:
            raise RuntimeError("MailArchive is closing or changing profiles.")

    def start(self) -> None:
        with self._lock:
            self._ensure_available()
            if self._started:
                return
            try:
                self._configure_startup(self._context.settings.start_at_login)
            except Exception as exc:
                self._context.report(
                    ServiceEvent(EventLevel.WARNING, f"Could not configure start at login: {exc}")
                )
            self._context.execution.start()
            self._started = True

    def close(self, *, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closing = True
            for cancelled in self._authorizations.values():
                cancelled.set()
            context = self._context
            recovery = self._recovery_thread
        stopped = context.execution.shutdown(timeout=timeout)
        tasks_finished = self._background.close(timeout)
        if recovery and recovery.is_alive():
            recovery.join(timeout)
        return stopped and tasks_finished and not (recovery and recovery.is_alive())

    def _restore_previous(self, previous) -> None:
        """Reopen a stopped profile so it receives a fresh execution worker."""
        with self._lock:
            if self._closing:
                return
            self._profiles.activate(previous.database_path)
            restored = self._profiles.open(
                previous.database_path, self._receive_event, self._receive_progress
            )
            try:
                if self._started:
                    restored.execution.start()
            except Exception:
                restored.execution.shutdown(timeout=5.0)
                raise
            self._context = restored
            self._switching = False

    def _recover_when_stopped(self, previous, stopping) -> None:
        """Finish a timed-out worker stop without blocking the UI thread."""
        try:
            while True:
                with self._lock:
                    if self._closing:
                        return
                if stopping.execution.shutdown(timeout=0.25):
                    break
            self._restore_previous(previous)
        except Exception as exc:
            self._receive_event(
                ServiceEvent(EventLevel.ERROR, f"Could not restore the previous profile: {exc}")
            )

    def _schedule_profile_recovery(self, previous, stopping) -> None:
        recovery = threading.Thread(
            target=self._recover_when_stopped,
            args=(previous, stopping),
            name="MailArchive-ProfileRecovery",
            daemon=True,
        )
        self._recovery_thread = recovery
        recovery.start()

    def switch_profile(self, path: Path, *, timeout: float = 5.0) -> Settings:
        path = path.expanduser().resolve()
        with self._lock:
            self._ensure_available()
            if path == self.database_path:
                return self.settings
            self._switching = True
            previous = self._context
            for cancelled in self._authorizations.values():
                cancelled.set()
        old_stopped = False
        replacement = None
        recovery_scheduled = False
        switched = False
        try:
            if not self._background.wait(timeout):
                raise RuntimeError("A background task is still finishing. Try again shortly.")
            old_stopped = previous.execution.shutdown(timeout=timeout)
            if not old_stopped:
                self._schedule_profile_recovery(previous, previous)
                recovery_scheduled = True
                raise RuntimeError("Mail processing is still stopping. Try again shortly.")
            replacement = self._profiles.open(path, self._receive_event, self._receive_progress)
            self._profiles.activate(path)
            with self._lock:
                self._context = replacement
            if self._started:
                replacement.execution.start()
            try:
                self._configure_startup(replacement.settings.start_at_login)
            except Exception as exc:
                replacement.report(
                    ServiceEvent(EventLevel.WARNING, f"Could not configure start at login: {exc}")
                )
            switched = True
            return self.settings
        except Exception:
            if old_stopped:
                if replacement is not None and not replacement.execution.shutdown(timeout=timeout):
                    self._schedule_profile_recovery(previous, replacement)
                    recovery_scheduled = True
                else:
                    self._restore_previous(previous)
            raise
        finally:
            with self._lock:
                if not recovery_scheduled and (not old_stopped or switched):
                    self._switching = False

    def save_settings(self, settings: Settings) -> Settings:
        candidate = deepcopy(settings)
        candidate.validate()
        with self._lock:
            self._ensure_available()
            if candidate.accounts != self._context.settings.accounts:
                raise ValueError("Change email accounts through the account editor.")
            return self._persist_settings(candidate)

    def _persist_settings(self, candidate: Settings) -> Settings:
        """Publish one validated configuration while its caller owns any account gate."""
        with self._lock:
            self._ensure_available()
            previous = self._context.settings
            startup_changed = candidate.start_at_login != previous.start_at_login
            if startup_changed:
                self._configure_startup(candidate.start_at_login)
            try:
                self._context.save(candidate)
            except Exception:
                if startup_changed:
                    self._configure_startup(previous.start_at_login)
                raise
            self._context.settings = candidate
            return deepcopy(candidate)

    def save_rules(self, rules: list[Rule]) -> Settings:
        return self.save_settings(replace(self.settings, rules=deepcopy(rules)))

    def save_account(
        self, submission: AccountSubmission, replacing_id: str | None = None
    ) -> Settings:
        with self._lock, self._context.account_change():
            self._ensure_available()
            credential_lock = account_credential_lock(submission.account.id)
            if not credential_lock.acquire(blocking=False):
                raise RuntimeError("This account is authorizing or refreshing credentials.")
            try:
                return self._store_account(submission, replacing_id)
            finally:
                credential_lock.release()

    def _store_account(self, submission: AccountSubmission, replacing_id: str | None) -> Settings:
        candidate = self.settings
        if replacing_id is None:
            candidate.accounts.append(deepcopy(submission.account))
        else:
            index = next(
                (i for i, account in enumerate(candidate.accounts) if account.id == replacing_id),
                None,
            )
            if index is None or submission.account.id != replacing_id:
                raise ValueError("The email account no longer exists.")
            candidate.accounts[index] = deepcopy(submission.account)
        candidate.validate()
        changes_credentials = bool(submission.credential_updates or submission.replace_credentials)
        previous = self._credentials.get(submission.account.id) if changes_credentials else None
        try:
            if changes_credentials:
                store_account_credentials(
                    self._credentials,
                    submission.account,
                    submission.credential_updates,
                    replace=submission.replace_credentials,
                )
            return self._persist_settings(candidate)
        except Exception as exc:
            if changes_credentials:
                try:
                    if previous is None:
                        self._credentials.delete(submission.account.id)
                    else:
                        self._credentials.set(submission.account.id, previous)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"{exc} Restoring the previous credentials also failed: {rollback_exc}"
                    ) from exc
            raise

    def delete_account(self, account_id: str) -> Settings:
        with self._lock, self._context.account_change():
            self._ensure_available()
            if self.authorization_in_progress(account_id):
                raise RuntimeError("Finish or cancel this account's authorization first.")
            lock = account_credential_lock(account_id)
            if not lock.acquire(blocking=False):
                raise RuntimeError("This account is refreshing credentials.")
            try:
                candidate = self.settings
                if not any(account.id == account_id for account in candidate.accounts):
                    raise ValueError("The email account no longer exists.")
                candidate.accounts = [a for a in candidate.accounts if a.id != account_id]
                saved = self._persist_settings(candidate)
                try:
                    self._credentials.delete(account_id)
                except Exception as exc:
                    self._context.report(
                        ServiceEvent(
                            EventLevel.WARNING,
                            f"The account was removed, but its credentials could not be deleted: {exc}",
                            account_id,
                        )
                    )
                return saved
            finally:
                lock.release()

    def authorize_account(self, account_id: str) -> bool:
        with self._lock:
            self._ensure_available()
            if account_id in self._authorizations:
                return False
            account = next((a for a in self.settings.accounts if a.id == account_id), None)
            if account is None:
                raise ValueError("The email account no longer exists.")
            cancelled = threading.Event()
            self._authorizations[account_id] = cancelled
            context = self._context

        def authorize() -> None:
            try:
                self._authorize(account, self._credentials, cancelled=cancelled)
            except Exception as exc:
                if not cancelled.is_set():
                    context.report(
                        ServiceEvent(
                            EventLevel.ERROR,
                            f"{account.label}: Authorization failed: {exc}",
                            account_id,
                        )
                    )
            else:
                if not cancelled.is_set():
                    context.report(
                        ServiceEvent(
                            EventLevel.SUCCESS,
                            f"{account.label}: Authorization completed.",
                            account_id,
                        )
                    )
            finally:
                with self._lock:
                    if self._authorizations.get(account_id) is cancelled:
                        self._authorizations.pop(account_id)

        try:
            self._background.submit(authorize, lambda result: None)
        except Exception:
            with self._lock:
                self._authorizations.pop(account_id, None)
            raise
        return True

    def cancel_authorization(self, account_id: str) -> None:
        with self._lock:
            if account_id in self._authorizations:
                self._authorizations[account_id].set()

    def authorization_in_progress(self, account_id: str) -> bool:
        return account_id in self.authorizing_account_ids

    def check_now(self) -> bool:
        with self._lock:
            self._ensure_available()
            return self._context.execution.check_mail_now()

    def apply_rule_to_past_mail(
        self, rule_id: str, start: datetime | None, end: datetime | None, timezone_name: str
    ) -> str:
        with self._lock:
            self._ensure_available()
            return self._context.execution.apply_to_past_mail(rule_id, start, end, timezone_name)

    def stop_operation(self, operation_id: str) -> None:
        self._context.execution.stop_operation(operation_id)

    def retry_activity(self, key: str) -> None:
        with self._lock:
            self._ensure_available()
            self._context.execution.retry_activity(key)

    def current_jobs(self):
        return self._context.activity.current()

    def activity_page(self, *, before=None, limit: int = 100):
        return self._context.activity.history(before=before, limit=limit)

    def activity_detail(self, key: str):
        return self._context.activity.detail(key)

    def status(self):
        return self._context.queries.status()

    def monitoring_status(self, source_id: str, folders: list[str] | None = None):
        if folders is None:
            folders = next(
                (
                    m.folders
                    for a in self.settings.accounts
                    for m in a.mailboxes
                    if m.id == source_id
                ),
                [],
            )
        return self._context.queries.monitoring_status(source_id, folders)

    def paused_scopes(self, account_id: str):
        return self._context.queries.paused_scopes(account_id)

    def reset_scope_baseline(self, source_id: str, scope_key: str) -> int:
        with self._lock, self._context.account_change():
            self._ensure_available()
            return self._context.reset_scope(source_id, scope_key)

    def activity_log_page(self, *, since=None, offset: int = 0, limit: int = 50):
        return self._context.diagnostics.page(since=since, offset=offset, limit=limit)

    def clear_activity_log(self) -> None:
        self._context.diagnostics.clear()

    def submit_background(
        self, work: Callable[[], T], callback: Callable[[BackgroundResult[T]], None]
    ) -> None:
        with self._lock:
            self._ensure_available()
            self._background.submit(work, callback)

    def dispatch_callbacks(self) -> None:
        self._background.dispatch()

    def check_for_updates(self, callback: Callable) -> None:
        self.submit_background(
            self._update_check,
            lambda result: callback(result.value, str(result.error) if result.error else ""),
        )

    def read_service_account(self, path: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_available()
            reader = self._service_account_reader
        if reader is None:
            raise RuntimeError("Service account file loading is unavailable.")
        return reader(path)
