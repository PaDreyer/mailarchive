"""User actions and the lifetime of the currently selected profile."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    account_credential_lock,
    bind_legacy_account_credentials,
    credential_binding,
    store_account_credentials,
)
from mailarchive.application.account_edit import AccountEditSession
from mailarchive.application.account_status import (
    AccountAction,
    AccountAuthorizationResult,
    AccountStatus,
    AccountStatusService,
    AuthorizationOutcome,
    AuthorizationState,
    AuthorizationStatus,
    account_status,
    authorization_binding,
    authorization_binding_covers,
)
from mailarchive.application.archive_destinations import ArchiveDestinationPolicy
from mailarchive.application.background import BackgroundResult, BackgroundTasks
from mailarchive.application.credential_port import CredentialError, CredentialStore
from mailarchive.application.errors import (
    ExecutionShutdownError,
    ProfileUnavailableError,
    ShutdownCleanupError,
)
from mailarchive.application.events import EventLevel, RunProgress, ServiceEvent
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.application.profile import ProfileContext, ProfileManager
from mailarchive.domain.configuration import Account, AuthMode, Rule, Settings

T = TypeVar("T")
logger = logging.getLogger(__name__)


class _RecoveryState(Enum):
    WAITING_FOR_STOP = "waiting_for_stop"
    WAITING_FOR_PROFILE = "waiting_for_profile"
    RESTORING = "restoring"
    FAILED = "failed"


@dataclass(slots=True)
class _ProfileRecovery:
    previous: ProfileContext
    stopping: ProfileContext
    state: _RecoveryState = _RecoveryState.WAITING_FOR_STOP
    detail: str = ""
    wakeup: threading.Event = field(default_factory=threading.Event)


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
        authorization_inspector: Callable[[Account, CredentialStore], AuthorizationStatus]
        | None = None,
    ) -> None:
        self._profiles = profiles
        self._credentials = credentials
        self._authorize = authorize
        self._configure_startup = configure_startup
        self._update_check = update_check
        self._service_account_reader = service_account_reader
        self._authorization_inspector = authorization_inspector
        self._lock = threading.RLock()
        self._background = BackgroundTasks()
        self._fallback_statuses = AccountStatusService()
        self._authorizations: dict[str, threading.Event] = {}
        self._account_edits: set[AccountEditSession] = set()
        self._on_event: Callable[[ServiceEvent], None] = lambda event: None
        self._on_progress: Callable[[RunProgress], None] = lambda progress: None
        self._started = False
        self._closing = False
        self._switching = False
        self._recovery_thread: threading.Thread | None = None
        self._recovery_threads: set[threading.Thread] = set()
        self._profile_switch_threads: set[threading.Thread] = set()
        self._profile_switch_candidate: ProfileContext | None = None
        self._profile_recovery: _ProfileRecovery | None = None
        self._profile_transition_done = threading.Event()
        self._profile_transition_done.set()
        self._shutdown_errors: list[str] = []
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
        recovery = self._profile_recovery
        if (
            not self._closing
            and recovery is not None
            and recovery.state in {_RecoveryState.WAITING_FOR_PROFILE, _RecoveryState.FAILED}
        ):
            # Commands may already own the facade RLock. Restore I/O belongs to
            # the recovery worker, never to that command's UI-thread lock scope.
            recovery.state = _RecoveryState.WAITING_FOR_PROFILE
            if self._recovery_thread is None or not self._recovery_thread.is_alive():
                self._schedule_profile_recovery(
                    recovery.previous, recovery.stopping, pending=recovery
                )
            recovery.wakeup.set()
        if self._profile_recovery is not None:
            raise RuntimeError(
                "MailArchive is changing profiles. The previous profile is unavailable. "
                "Restore its database and retry, "
                "or select another profile. " + self._profile_recovery.detail
            )
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
            self._refresh_authorizations()
            self._context.execution.start()
            self._started = True

    def close(self, *, timeout: float = 5.0) -> bool:
        if timeout < 0:
            raise ValueError("The shutdown timeout cannot be negative.")
        deadline = time.monotonic() + timeout
        with self._lock:
            self._closing = True
            if self._profile_recovery is not None:
                self._profile_recovery.wakeup.set()
            self._record_shutdown_errors(self._cancel_account_sessions())
            context = self._context
            recovery_threads = tuple(self._recovery_threads)
            switch_threads = tuple(self._profile_switch_threads)
            candidate = self._profile_switch_candidate
            stopping = self._profile_recovery.stopping if self._profile_recovery else context
        failures: list[Exception] = []
        # Every owner gets its cleanup attempt, even after an unexpected owner failure.
        try:
            self._background.close(0)
        except Exception as exc:
            failures.append(exc)
        stopped = True
        owned_contexts = [stopping]
        if context is not stopping:
            owned_contexts.append(context)
        if candidate is not None and all(candidate is not owned for owned in owned_contexts):
            owned_contexts.append(candidate)
        for owned in owned_contexts:
            try:
                stopped = (
                    self._shutdown_execution(owned, max(0, deadline - time.monotonic())) and stopped
                )
            except Exception as exc:
                failures.append(exc)
                stopped = False
        tasks_finished = False
        try:
            tasks_finished = self._background.close(max(0, deadline - time.monotonic()))
        except Exception as exc:
            failures.append(exc)
        try:
            for owner in (*recovery_threads, *switch_threads):
                if owner.is_alive():
                    owner.join(max(0, deadline - time.monotonic()))
        except Exception as exc:
            failures.append(exc)
        if failures:
            raise ShutdownCleanupError(tuple(failures)) from failures[0]
        transition_finished = self._profile_transition_done.wait(
            max(0, deadline - time.monotonic())
        )
        with self._lock:
            late = self._profile_recovery.stopping if self._profile_recovery else self._context
        if late is not stopping and late is not context:
            stopped = (
                self._shutdown_execution(late, max(0, deadline - time.monotonic())) and stopped
            )
        return (
            stopped
            and tasks_finished
            and transition_finished
            and not any(owner.is_alive() for owner in (*recovery_threads, *switch_threads))
        )

    def _cancel_account_sessions(self) -> tuple[str, ...]:
        failures = []
        for cancelled in self._authorizations.values():
            cancelled.set()
        for editor in tuple(self._account_edits):
            try:
                editor.cancel_authorization()
                editor.close()
            except Exception as exc:
                failures.append(f"Could not close the account editor: {exc}")
        return tuple(failures)

    @property
    def shutdown_errors(self) -> tuple[str, ...]:
        """Retained-work settlement failures, separate from resource closure."""
        with self._lock:
            return tuple(self._shutdown_errors)

    def _record_shutdown_errors(self, failures: tuple[str, ...]) -> None:
        with self._lock:
            for failure in failures:
                if failure not in self._shutdown_errors:
                    self._shutdown_errors.append(failure)
                    # Reporting through the profile would attempt failed SQLite I/O again.
                    logger.warning("Shutdown recovery required: %s", failure)

    def _shutdown_execution(self, context: ProfileContext, timeout: float) -> bool:
        try:
            return context.execution.shutdown(timeout=timeout)
        except ExecutionShutdownError as exc:
            self._record_shutdown_errors(exc.failures)
            return exc.stopped

    def _restore_previous(self, previous) -> bool:
        """Reopen a stopped profile so it receives a fresh execution worker."""
        with self._lock:
            if self._closing:
                return False
            recovery, started = self._profile_recovery, self._started
        if not previous.database_path.is_file():
            raise FileNotFoundError(
                f"The previous profile database is missing: {previous.database_path}"
            )
        self._profiles.activate(previous.database_path)
        restored = self._profiles.open(
            previous.database_path, self._receive_event, self._receive_progress
        )
        with self._lock:
            if recovery is not None:
                recovery.stopping = restored
            closing = self._closing
        try:
            if started and not closing:
                restored.execution.start()
        except Exception:
            if not self._shutdown_execution(restored, 0) and recovery is not None:
                recovery.state = _RecoveryState.WAITING_FOR_STOP
            raise
        with self._lock:
            if self._closing:
                self._shutdown_execution(restored, 0)
                return False
            self._context = restored
            self._refresh_authorizations()
            self._switching = False
            return True

    def _attempt_profile_restore(self, recovery: _ProfileRecovery) -> bool:
        with self._lock:
            if self._closing or self._profile_recovery is not recovery:
                return False
            recovery.state = _RecoveryState.RESTORING
            transition = self._new_profile_transition()
        try:
            restored = self._restore_previous(recovery.previous)
        except Exception as exc:
            with self._lock:
                recovery.detail = str(exc)
                if recovery.state != _RecoveryState.WAITING_FOR_STOP:
                    recovery.state = (
                        _RecoveryState.WAITING_FOR_PROFILE
                        if isinstance(exc, (OSError, ProfileUnavailableError))
                        else _RecoveryState.FAILED
                    )
                self._switching = False
                return False
        finally:
            transition.set()
        with self._lock:
            if not restored:
                return False
            self._profile_recovery = None
            return True

    def _recover_when_stopped(self, recovery: _ProfileRecovery) -> None:
        """Finish a timed-out worker stop without blocking the UI thread."""
        while True:
            with self._lock:
                if self._closing or self._profile_recovery is not recovery:
                    return
                failed = recovery.state == _RecoveryState.FAILED
            if failed:
                # A fatal restore failure needs an explicit retry, not repeated I/O.
                recovery.wakeup.wait()
                recovery.wakeup.clear()
                continue
            try:
                if recovery.state == _RecoveryState.WAITING_FOR_STOP:
                    if not self._shutdown_execution(recovery.stopping, 0.25):
                        continue
                    recovery.state = _RecoveryState.WAITING_FOR_PROFILE
                if self._attempt_profile_restore(recovery):
                    return
                recovery.wakeup.wait(0.25)
                recovery.wakeup.clear()
            except Exception as exc:
                with self._lock:
                    recovery.state = _RecoveryState.FAILED
                    recovery.detail = str(exc)
                    self._switching = False
                self._receive_event(
                    ServiceEvent(EventLevel.ERROR, f"Could not restore the previous profile: {exc}")
                )

    def _schedule_profile_recovery(self, previous, stopping, *, pending=None) -> None:
        pending = pending or _ProfileRecovery(previous, stopping)
        with self._lock:
            obsolete = self._profile_recovery
            self._profile_recovery = pending
            if obsolete is not None and obsolete is not pending:
                obsolete.wakeup.set()
            if self._closing:
                return
            recovery = threading.Thread(
                target=self._recover_when_stopped,
                args=(pending,),
                name="MailArchive-ProfileRecovery",
                daemon=True,
            )
            self._recovery_thread = recovery
            self._recovery_threads = {
                thread for thread in self._recovery_threads if thread.is_alive()
            }
            self._recovery_threads.add(recovery)
            recovery.start()

    def _new_profile_transition(self) -> threading.Event:
        """Give each I/O attempt its own completion token while owning the facade lock."""
        transition = threading.Event()
        self._profile_transition_done = transition
        return transition

    def _begin_profile_switch(self, path: Path) -> tuple[ProfileContext | None, threading.Event]:
        with self._lock:
            if self._profile_recovery is None or self._profile_recovery.state in {
                _RecoveryState.WAITING_FOR_STOP,
                _RecoveryState.RESTORING,
            }:
                self._ensure_available()
            elif self._closing:
                raise RuntimeError("MailArchive is closing.")
            if path == self.database_path:
                self._ensure_available()
                return None, self._profile_transition_done
            obsolete = self._profile_recovery
            self._profile_recovery = None
            if obsolete is not None:
                obsolete.wakeup.set()
            self._switching = True
            transition = self._new_profile_transition()
            previous = self._context
            failures = self._cancel_account_sessions()
            if failures:
                self._switching = False
                transition.set()
                raise RuntimeError("; ".join(failures))
            return previous, transition

    def switch_profile(self, path: Path, *, timeout: float = 5.0) -> Settings:
        if timeout < 0:
            raise ValueError("The shutdown timeout cannot be negative.")
        deadline = time.monotonic() + timeout
        path = path.expanduser().resolve()
        previous, transition = self._begin_profile_switch(path)
        if previous is None:
            return self.settings
        return self._switch_profile(path, previous, transition, deadline)

    def request_profile_switch(
        self,
        path: Path,
        on_complete: Callable[[BackgroundResult[Settings]], None],
        *,
        timeout: float = 5.0,
    ) -> bool:
        """Own profile I/O separately; dispatch completion on the caller's UI loop.

        Closing cancels publication and waits for real switch I/O within its own
        budget. This worker cannot run in the background pool it must drain.
        """
        if timeout < 0:
            raise ValueError("The shutdown timeout cannot be negative.")
        deadline = time.monotonic() + timeout
        path = path.expanduser().resolve()
        previous, transition = self._begin_profile_switch(path)
        if previous is None:
            result = BackgroundResult(value=self.settings)
            self._background.post(lambda: self._complete_profile_switch(on_complete, result))
            return False

        def switch() -> None:
            try:
                result = BackgroundResult(
                    value=self._switch_profile(path, previous, transition, deadline)
                )
            except Exception as exc:
                result = BackgroundResult(error=exc)
            self._background.post(lambda: self._complete_profile_switch(on_complete, result))

        worker = threading.Thread(target=switch, name="MailArchive-ProfileSwitch", daemon=False)
        with self._lock:
            self._profile_switch_threads = {
                owner for owner in self._profile_switch_threads if owner.is_alive()
            }
            self._profile_switch_threads.add(worker)
            try:
                worker.start()
            except Exception:
                self._profile_switch_threads.discard(worker)
                self._switching = False
                transition.set()
                raise
        return True

    def _complete_profile_switch(
        self,
        callback: Callable[[BackgroundResult[Settings]], None],
        result: BackgroundResult[Settings],
    ) -> None:
        with self._lock:
            if self._closing:
                return
        callback(result)

    def _switch_profile(
        self,
        path: Path,
        previous: ProfileContext,
        transition: threading.Event,
        deadline: float,
    ) -> Settings:
        old_stopped = False
        replacement = None
        recovery_scheduled = False
        switched = False
        startup_attempted = False
        try:
            if not self._background.wait(max(0, deadline - time.monotonic())):
                raise RuntimeError("A background task is still finishing. Try again shortly.")
            try:
                old_stopped = previous.execution.shutdown(
                    timeout=max(0, deadline - time.monotonic())
                )
            except ExecutionShutdownError as exc:
                old_stopped = exc.stopped
                self._record_shutdown_errors(exc.failures)
                if not old_stopped:
                    self._schedule_profile_recovery(previous, previous)
                    recovery_scheduled = True
                raise
            if not old_stopped:
                self._schedule_profile_recovery(previous, previous)
                recovery_scheduled = True
                raise RuntimeError("Mail processing is still stopping. Try again shortly.")
            self._check_profile_switch_open()
            replacement = self._profiles.open(path, self._receive_event, self._receive_progress)
            self._check_profile_switch_open()
            with self._lock:
                if self._closing:
                    raise RuntimeError("MailArchive is closing.")
                self._profile_switch_candidate = replacement
            if self._started:
                replacement.execution.start()
            self._check_profile_switch_open()
            startup_attempted = True
            self._configure_profile_startup(replacement)
            self._check_profile_switch_open()
            self._profiles.activate(path)
            with self._lock:
                if self._closing:
                    raise RuntimeError("MailArchive is closing.")
                self._context = replacement
                self._refresh_authorizations()
            switched = True
            return self.settings
        except Exception:
            if startup_attempted:
                try:
                    self._configure_startup(previous.settings.start_at_login)
                except Exception as exc:
                    self._record_shutdown_errors((f"Could not restore start at login: {exc}",))
            if old_stopped:
                self._restore_location_on_close(previous)
                if replacement is not None and not self._shutdown_execution(
                    replacement, max(0, deadline - time.monotonic())
                ):
                    self._schedule_profile_recovery(previous, replacement)
                    recovery_scheduled = True
                else:
                    pending = _ProfileRecovery(
                        previous, previous, _RecoveryState.WAITING_FOR_PROFILE
                    )
                    self._profile_recovery = pending
                    if not self._attempt_profile_restore(pending):
                        self._schedule_profile_recovery(previous, previous, pending=pending)
                        recovery_scheduled = True
            raise
        finally:
            with self._lock:
                self._profile_switch_candidate = None
                if not recovery_scheduled and (not old_stopped or switched):
                    self._switching = False
            transition.set()

    def _configure_profile_startup(self, context: ProfileContext) -> None:
        try:
            self._configure_startup(context.settings.start_at_login)
        except Exception as exc:
            context.report(
                ServiceEvent(EventLevel.WARNING, f"Could not configure start at login: {exc}")
            )

    def _check_profile_switch_open(self) -> None:
        with self._lock:
            if self._closing:
                raise RuntimeError("MailArchive is closing.")

    def _restore_location_on_close(self, previous: ProfileContext) -> None:
        if self._closing and previous.database_path.is_file():
            try:
                self._profiles.activate(previous.database_path)
            except Exception as exc:
                self._record_shutdown_errors((f"Could not restore the profile location: {exc}",))

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
            ArchiveDestinationPolicy(self._context.database_path.parent).require_settings(candidate)
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
            self.account_statuses.register_accounts(candidate.accounts)
            self._context.execution.settings_changed(candidate)
            return deepcopy(candidate)

    def set_automatic_monitoring_paused(self, paused: bool) -> Settings:
        with self._lock:
            self._ensure_available()
            candidate = replace(self.settings, automatic_monitoring_paused=paused)
            return self._persist_settings(candidate)

    def automatic_monitoring_state(self) -> AutomaticMonitoringState:
        with self._lock:
            if self._closing or self._switching or self._profile_recovery is not None:
                return AutomaticMonitoringState.UNAVAILABLE
            return self._context.execution.automatic_monitoring_state()

    def save_rules(self, rules: list[Rule]) -> Settings:
        return self.save_settings(replace(self.settings, rules=deepcopy(rules)))

    def save_account(
        self, submission: AccountSubmission, replacing_id: str | None = None
    ) -> Settings:
        with self._lock, self._context.account_change():
            self._ensure_available()
            if self.authorization_in_progress(submission.account.id):
                raise RuntimeError("Finish or cancel this account's authorization first.")
            credential_lock = account_credential_lock(submission.account.id)
            if not credential_lock.acquire(blocking=False):
                raise RuntimeError("This account is authorizing or refreshing credentials.")
            try:
                authorization = self.preview_account_status(submission).authorization
                saved = self._store_account(submission, replacing_id)
                statuses = self.account_statuses
                statuses.set_authorization(submission.account, authorization)
                self._refresh_authorizations([submission.account])
                return saved
            except CredentialError as exc:
                self.account_statuses.credential_record_failed(
                    submission.account.id,
                    AuthorizationStatus(AuthorizationState.UNAVAILABLE, str(exc)),
                )
                raise
            finally:
                credential_lock.release()

    def _store_account(self, submission: AccountSubmission, replacing_id: str | None) -> Settings:
        candidate = self.settings
        existing = next((item for item in candidate.accounts if item.id == replacing_id), None)
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
        bind_before_edit = existing is not None and credential_binding(
            existing
        ) != credential_binding(submission.account)
        changes_credentials = changes_credentials or bind_before_edit
        previous = self._credentials.get(submission.account.id) if changes_credentials else None
        try:
            if bind_before_edit:
                # The previous published identity is the only authority for legacy
                # records. A retry or an unsaved draft cannot supply that identity.
                bind_legacy_account_credentials(self._credentials, existing)
            if submission.credential_updates or submission.replace_credentials:
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
                    if isinstance(rollback_exc, CredentialError):
                        self.account_statuses.credential_record_failed(
                            submission.account.id,
                            AuthorizationStatus(AuthorizationState.UNAVAILABLE, str(rollback_exc)),
                        )
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
                    self.account_statuses.credential_record_failed(
                        account_id,
                        AuthorizationStatus(
                            AuthorizationState.REQUIRED,
                            "The account's saved credentials were removed.",
                        ),
                    )
                except Exception as exc:
                    self.account_statuses.credential_record_failed(
                        account_id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, str(exc))
                    )
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

    @property
    def account_statuses(self) -> AccountStatusService:
        return self._context.account_statuses or self._fallback_statuses

    def account_status(self, account_id: str) -> AccountStatus:
        with self._lock:
            settings = self.settings
            account = next((a for a in settings.accounts if a.id == account_id), None)
            if account is None:
                raise ValueError("The email account no longer exists.")
            return self.account_statuses.resolve(account, settings.rules)

    def _refresh_authorizations(self, accounts: list[Account] | None = None) -> None:
        statuses = self.account_statuses
        for account in accounts if accounts is not None else self.settings.accounts:
            self._background.submit(
                lambda account=account: statuses.refresh(account), lambda result: None
            )

    def preview_account_status(
        self, submission: AccountSubmission, *, authorization: AuthorizationStatus | None = None
    ) -> AccountStatus:
        """Evaluate a draft against the cached identity, without credential-store I/O."""
        with self._lock:
            settings = self.settings
            account = submission.account
            existing = next((a for a in settings.accounts if a.id == account.id), None)
            cached = self.account_statuses.authorization(account)
            reusable = (
                existing is not None
                and authorization_binding_covers(
                    authorization_binding(existing),
                    authorization_binding(account),
                    capabilities=cached.capabilities,
                )
                and cached.state == AuthorizationState.AUTHORIZED
            )
            if account.auth_mode == AuthMode.OAUTH_USER and (
                existing is None
                or submission.replace_credentials
                or (
                    authorization_binding(account) != authorization_binding(existing)
                    and not reusable
                )
            ):
                cached = AuthorizationStatus(AuthorizationState.REQUIRED)
            return account_status(account, settings.rules, authorization or cached)

    def account_editor(self, account_id: str | None = None) -> AccountEditSession:
        with self._lock:
            self._ensure_available()
            context = self._context
            statuses = self.account_statuses
            existing = next((a for a in self.settings.accounts if a.id == account_id), None)
            if account_id is not None and existing is None:
                raise ValueError("The email account no longer exists.")
            if self._authorization_inspector is None:
                raise RuntimeError("Credential inspection is unavailable.")

            def ensure_context():
                with self._lock:
                    self._ensure_available()
                    if self._context is not context:
                        raise RuntimeError("This account editor belongs to another profile.")

            def save(submission):
                with self._lock:
                    ensure_context()
                    return self.save_account(submission, replacing_id=account_id)

            def submit(work, callback):
                with self._lock:
                    ensure_context()
                    self.submit_background(work, callback)

            def closed(editor):
                with self._lock:
                    self._account_edits.discard(editor)

            def refresh():
                with self._lock:
                    ensure_context()
                    if account_id is None:
                        raise RuntimeError("This account has no saved credentials to check.")
                    self.refresh_account_authorization(account_id)

            editor = AccountEditSession(
                existing,
                self._credentials,
                authorize=lambda *args, **kwargs: self._authorize(*args, **kwargs),
                inspect=self._authorization_inspector,
                resolve=lambda submission, authorization: self.preview_account_status(
                    submission, authorization=authorization
                ),
                refresh=refresh,
                save=save,
                submit=submit,
                on_close=closed,
                on_credentials_unavailable=lambda account_id, detail: (
                    statuses.credential_record_failed(
                        account_id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, detail)
                    )
                ),
            )
            self._account_edits.add(editor)
            return editor

    def refresh_account_authorization(self, account_id: str) -> None:
        with self._lock:
            self._ensure_available()
            account = next((a for a in self.settings.accounts if a.id == account_id), None)
            if account is None:
                raise ValueError("The email account no longer exists.")
            self.account_statuses.set_authorization(
                account, AuthorizationStatus(AuthorizationState.CHECKING)
            )
            self._refresh_authorizations([account])

    def authorize_account(
        self,
        account_id: str,
        *,
        on_complete: Callable[[AccountAuthorizationResult], None] | None = None,
    ) -> bool:
        with self._lock:
            self._ensure_available()
            if account_id in self._authorizations:
                return False
            account = next((a for a in self.settings.accounts if a.id == account_id), None)
            if account is None:
                raise ValueError("The email account no longer exists.")
            statuses = self.account_statuses
            if not statuses.resolve(account, self.settings.rules).allows(AccountAction.AUTHORIZE):
                raise ValueError("This account cannot start interactive authorization right now.")
            cancelled = threading.Event()
            self._authorizations[account_id] = cancelled
            context = self._context
            previous = statuses.authorization(account)
            statuses.set_authorization(account, AuthorizationStatus(AuthorizationState.AUTHORIZING))

        def authorize() -> AccountAuthorizationResult:
            outcome, detail = AuthorizationOutcome.COMPLETED, ""
            try:
                self._perform_account_authorization(account, cancelled)
            except Exception as exc:
                outcome, detail = AuthorizationOutcome.FAILED, str(exc)
            finally:
                with self._lock:
                    if self._authorizations.get(account_id) is cancelled:
                        self._authorizations.pop(account_id)
                    statuses.set_authorization(
                        account, AuthorizationStatus(AuthorizationState.CHECKING)
                    )
            if cancelled.is_set():
                outcome, detail = AuthorizationOutcome.CANCELLED, ""
            status = statuses.refresh(account)
            if detail:
                statuses.set_authorization(account, replace(status, detail=detail))
            result = AccountAuthorizationResult(account_id, outcome, detail)
            messages = {
                AuthorizationOutcome.COMPLETED: (EventLevel.SUCCESS, "Authorization completed."),
                AuthorizationOutcome.CANCELLED: (EventLevel.INFO, "Authorization cancelled."),
                AuthorizationOutcome.FAILED: (EventLevel.ERROR, f"Authorization failed: {detail}"),
            }
            level, message = messages[outcome]
            if not self._closing and not self._switching:
                context.report(ServiceEvent(level, f"{account.label}: {message}", account_id))
            return result

        def completed(result: BackgroundResult[AccountAuthorizationResult]) -> None:
            if (
                on_complete
                and result.value is not None
                and self._context is context
                and not self._closing
            ):
                on_complete(result.value)

        try:
            self._background.submit(authorize, completed)
        except Exception:
            with self._lock:
                self._authorizations.pop(account_id, None)
                statuses.set_authorization(account, previous)
            raise
        return True

    def _perform_account_authorization(self, account: Account, cancelled: threading.Event) -> None:
        """Keep cancellation and credential publication inside one serialized lifecycle."""
        with account_credential_lock(account.id):
            previous = self._credentials.get(account.id)
            try:
                self._authorize(account, self._credentials, cancelled=cancelled)
            finally:
                if cancelled.is_set():
                    if previous is None:
                        self._credentials.delete(account.id)
                    else:
                        self._credentials.set(account.id, previous)

    def cancel_authorization(self, account_id: str) -> None:
        with self._lock:
            if account_id in self._authorizations:
                self._authorizations[account_id].set()

    def authorization_in_progress(self, account_id: str) -> bool:
        return account_id in self.authorizing_account_ids

    def check_now(self) -> str | None:
        with self._lock:
            self._ensure_available()
            return self._context.execution.check_mail_now()

    def apply_rule_to_past_mail(
        self, rule_id: str, start: datetime | None, end: datetime | None, timezone_name: str
    ) -> str:
        with self._lock:
            self._ensure_available()
            return self._context.execution.apply_to_past_mail(rule_id, start, end, timezone_name)

    def stop_check(self, check_id: str) -> bool:
        with self._lock:
            self._ensure_available()
            return self._context.execution.stop_check(check_id)

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
