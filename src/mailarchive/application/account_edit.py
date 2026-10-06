"""Authorize an account draft without publishing configuration or credentials."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any
from uuid import uuid4

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    account_credential_lock,
    credential_binding,
    load_credential_data,
    store_account_credentials,
)
from mailarchive.application.account_status import (
    AccountAction,
    AccountAuthorizationResult,
    AccountStatus,
    AuthorizationCapability,
    AuthorizationOutcome,
    AuthorizationState,
    AuthorizationStatus,
    authorization_binding,
)
from mailarchive.application.cancellation import Cancellation
from mailarchive.application.credential_port import CredentialError, CredentialStore
from mailarchive.domain.configuration import Account, Settings


class _DraftCredentials:
    """One isolated credential record, never backed by protected storage."""

    def __init__(self, account_id: str, value: str | None) -> None:
        self._account_id, self._value = account_id, value

    def get(self, account_id: str) -> str | None:
        if account_id != self._account_id:
            raise ValueError("The credential record belongs to another account draft.")
        return self._value

    def set(self, account_id: str, value: str) -> None:
        self.get(account_id)
        self._value = value

    def delete(self, account_id: str) -> None:
        self.get(account_id)
        self._value = None


@dataclass(frozen=True, slots=True)
class _AuthorizationBinding:
    identity: tuple[object, ...]
    shared_access: bool
    credential_updates: bytes
    replace_credentials: bool

    def covers(self, requested: _AuthorizationBinding) -> bool:
        return (
            self.identity == requested.identity
            and self.credential_updates == requested.credential_updates
            and self.replace_credentials == requested.replace_credentials
            and (self.shared_access or not requested.shared_access)
        )


def _binding(submission: AccountSubmission) -> _AuthorizationBinding:
    binding = authorization_binding(submission.account)
    return _AuthorizationBinding(
        binding[:-1],
        bool(binding[-1]),
        sha256(json.dumps(submission.credential_updates, sort_keys=True).encode()).digest(),
        submission.replace_credentials,
    )


class AccountEditSession:
    def __init__(
        self,
        existing: Account | None,
        credentials: CredentialStore,
        *,
        authorize: Callable,
        inspect: Callable[[Account, CredentialStore], AuthorizationStatus],
        resolve: Callable[[AccountSubmission, AuthorizationStatus | None], AccountStatus],
        refresh: Callable[[], None],
        save: Callable[[AccountSubmission], Settings],
        submit: Callable,
        on_close: Callable[[AccountEditSession], None],
        on_credentials_unavailable: Callable[[str, str], None] | None = None,
    ) -> None:
        self._existing = deepcopy(existing)
        self._credentials = credentials
        self._authorize, self._inspect = authorize, inspect
        self._resolve, self._save, self._submit = resolve, save, submit
        self._refresh = refresh
        self._on_close = on_close
        self._on_credentials_unavailable = on_credentials_unavailable or (
            lambda account_id, detail: None
        )
        self._lock = threading.RLock()
        self._closed = False
        self._running: threading.Event | None = None
        self._credential_id = str(uuid4())
        self._grant: tuple[_AuthorizationBinding, dict[str, Any]] | None = None
        self._result_binding: _AuthorizationBinding | None = None
        self._result: AccountAuthorizationResult | None = None

    def result_for(self, submission: AccountSubmission) -> AccountAuthorizationResult | None:
        with self._lock:
            key = _binding(submission)
            if self._result_binding == key:
                return self._result
            if self._grant is not None and self._grant[0].covers(key):
                return AccountAuthorizationResult(
                    submission.account.id, AuthorizationOutcome.COMPLETED
                )
            return None

    def status(self, submission: AccountSubmission) -> AccountStatus:
        return self.authorization_snapshot(submission)[0]

    def authorization_snapshot(
        self, submission: AccountSubmission
    ) -> tuple[AccountStatus, AccountAuthorizationResult | None]:
        key = _binding(submission)
        with self._lock:
            authorization = None
            if self._running is not None:
                authorization = AuthorizationStatus(AuthorizationState.AUTHORIZING)
            elif self._grant is not None and self._grant[0].covers(key):
                authorization = AuthorizationStatus(AuthorizationState.AUTHORIZED)
            detail = self._result.detail if self._result and self._result_binding == key else ""
            result = self.result_for(submission)
        status = self._resolve(submission, authorization)
        if detail and not status.authorization.detail:
            status = replace(status, authorization=replace(status.authorization, detail=detail))
        return status, result

    def authorize(self, submission: AccountSubmission) -> bool:
        submission = deepcopy(submission)
        status = self.status(submission)
        with self._lock:
            if self._closed:
                raise RuntimeError("This account editor is closed.")
            if self._running is not None:
                return False
            if not status.allows(AccountAction.AUTHORIZE):
                raise ValueError("This account cannot start interactive authorization right now.")
            cancelled = threading.Event()
            self._running, self._result, self._result_binding = cancelled, None, None
        try:
            self._submit(
                lambda: self._perform_authorization(submission, cancelled),
                lambda result: None,
            )
        except Exception:
            with self._lock:
                self._running = None
            raise
        return True

    def _perform_authorization(
        self,
        submission: AccountSubmission,
        cancelled: threading.Event,
    ) -> None:
        key = _binding(submission)
        account = deepcopy(submission.account)
        # Separate lock ownership too: a browser login must not stall the live account.
        account.id = self._credential_id
        outcome, detail, grant = AuthorizationOutcome.COMPLETED, "", None
        cancellation = Cancellation(cancelled.is_set, "Authorization cancelled.")
        try:
            cancellation.checkpoint()
            raw = None
            if (
                self._existing is not None
                and not submission.replace_credentials
                and credential_binding(self._existing) == credential_binding(submission.account)
            ):
                raw = self._credential_snapshot(self._existing.id, cancellation)
            credentials = _DraftCredentials(account.id, raw)
            store_account_credentials(
                credentials,
                account,
                submission.credential_updates,
                replace=submission.replace_credentials,
            )
            cancellation.checkpoint()
            self._authorize(account, credentials, cancelled=cancelled)
            authorization = self._inspect(account, credentials)
            if authorization.state != AuthorizationState.AUTHORIZED:
                raise RuntimeError(
                    authorization.detail or "Authorization did not provide usable credentials."
                )
            grant = load_credential_data(credentials, account.id)
        except Exception as exc:
            outcome, detail = AuthorizationOutcome.FAILED, str(exc)
        with self._lock:
            if self._closed or self._running is not cancelled:
                return
            if cancelled.is_set():
                outcome, detail, grant = AuthorizationOutcome.CANCELLED, "", None
            self._running = None
            if grant is not None:
                self._grant = (
                    replace(
                        key,
                        shared_access=key.shared_access
                        or AuthorizationCapability.SHARED_MAIL in authorization.capabilities,
                    ),
                    grant,
                )
            self._result_binding = key
            self._result = AccountAuthorizationResult(submission.account.id, outcome, detail)

    def _credential_snapshot(self, account_id: str, cancellation: Cancellation) -> str | None:
        lock = account_credential_lock(account_id)
        while True:
            cancellation.checkpoint()
            if lock.acquire(timeout=0.05):
                try:
                    cancellation.checkpoint()
                    return self._credentials.get(account_id)
                except CredentialError as exc:
                    self._on_credentials_unavailable(account_id, str(exc))
                    raise
                finally:
                    lock.release()

    def save(self, submission: AccountSubmission) -> Settings:
        with self._lock:
            if self._closed:
                raise RuntimeError("This account editor is closed.")
            if self._running is not None:
                raise RuntimeError("Finish or cancel authorization before saving the account.")
            if self._grant is not None and self._grant[0].covers(_binding(submission)):
                submission = AccountSubmission(submission.account, deepcopy(self._grant[1]), True)
        saved = self._save(submission)
        self.close()
        return saved

    def cancel_authorization(self) -> None:
        with self._lock:
            if self._running is not None:
                self._running.set()

    def refresh_authorization(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("This account editor is closed.")
            if self._running is not None:
                raise RuntimeError("Finish or cancel authorization before checking credentials.")
        self._refresh()
        with self._lock:
            self._result_binding, self._result = None, None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.cancel_authorization()
            self._grant, self._result_binding, self._result = None, None, None
        self._on_close(self)
