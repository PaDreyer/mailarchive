"""Account readiness and action policy shared by presentation and processing."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from mailarchive.application.account_credentials import credential_binding
from mailarchive.application.credential_port import CredentialError
from mailarchive.domain.configuration import Account, AuthMode, MailProvider, Rule
from mailarchive.domain.rules import has_enabled_rule_for_account


class AuthorizationState(str, Enum):
    NOT_REQUIRED = "not_required"
    CHECKING = "checking"
    AUTHORIZING = "authorizing"
    REQUIRED = "required"
    AUTHORIZED = "authorized"
    UNAVAILABLE = "unavailable"


class AuthorizationCapability(str, Enum):
    SHARED_MAIL = "shared_mail"


class AccountState(str, Enum):
    CHECKING_AUTHORIZATION = "checking_authorization"
    AUTHORIZING = "authorizing"
    AUTHORIZATION_REQUIRED = "authorization_required"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    PAUSED = "paused"
    NO_ACTIVE_MAILBOXES = "no_active_mailboxes"
    WAITING_FOR_RULE = "waiting_for_rule"
    ATTENTION = "attention"
    SETTING_UP = "setting_up"
    ACTIVE = "active"


class AccountBlocker(str, Enum):
    CHECKING_AUTHORIZATION = "checking_authorization"
    AUTHORIZING = "authorizing"
    AUTHORIZATION_REQUIRED = "authorization_required"
    CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
    PAUSED = "paused"
    NO_ACTIVE_MAILBOXES = "no_active_mailboxes"
    NO_ACTIVE_RULE = "no_active_rule"


class AccountAction(str, Enum):
    CHECK_MAIL = "check_mail"
    READ_PAST_MAIL = "read_past_mail"
    RETRY_REMOTE = "retry_remote"
    AUTHORIZE = "authorize"
    CANCEL_AUTHORIZATION = "cancel_authorization"


_AUTH_BLOCKERS = frozenset(
    {
        AccountBlocker.CHECKING_AUTHORIZATION,
        AccountBlocker.AUTHORIZING,
        AccountBlocker.AUTHORIZATION_REQUIRED,
        AccountBlocker.CREDENTIALS_UNAVAILABLE,
    }
)
_MAIL_BLOCKERS = _AUTH_BLOCKERS | {
    AccountBlocker.PAUSED,
    AccountBlocker.NO_ACTIVE_MAILBOXES,
    AccountBlocker.NO_ACTIVE_RULE,
}
_ACTION_BLOCKERS = {
    AccountAction.CHECK_MAIL: _MAIL_BLOCKERS,
    AccountAction.READ_PAST_MAIL: _AUTH_BLOCKERS
    | {
        AccountBlocker.PAUSED,
        AccountBlocker.NO_ACTIVE_MAILBOXES,
    },
    AccountAction.RETRY_REMOTE: _AUTH_BLOCKERS,
    AccountAction.AUTHORIZE: frozenset(
        {AccountBlocker.CHECKING_AUTHORIZATION, AccountBlocker.AUTHORIZING}
    ),
    AccountAction.CANCEL_AUTHORIZATION: frozenset(),
}
_AUTHORIZATION_BLOCKERS = {
    AuthorizationState.CHECKING: AccountBlocker.CHECKING_AUTHORIZATION,
    AuthorizationState.AUTHORIZING: AccountBlocker.AUTHORIZING,
    AuthorizationState.REQUIRED: AccountBlocker.AUTHORIZATION_REQUIRED,
    AuthorizationState.UNAVAILABLE: AccountBlocker.CREDENTIALS_UNAVAILABLE,
}
_STATE_PRIORITY = (
    (AccountBlocker.AUTHORIZING, AccountState.AUTHORIZING),
    (AccountBlocker.CHECKING_AUTHORIZATION, AccountState.CHECKING_AUTHORIZATION),
    (AccountBlocker.AUTHORIZATION_REQUIRED, AccountState.AUTHORIZATION_REQUIRED),
    (AccountBlocker.CREDENTIALS_UNAVAILABLE, AccountState.CREDENTIALS_UNAVAILABLE),
    (AccountBlocker.PAUSED, AccountState.PAUSED),
    (AccountBlocker.NO_ACTIVE_MAILBOXES, AccountState.NO_ACTIVE_MAILBOXES),
    (AccountBlocker.NO_ACTIVE_RULE, AccountState.WAITING_FOR_RULE),
)


@dataclass(frozen=True, slots=True)
class AuthorizationStatus:
    state: AuthorizationState
    detail: str = ""
    capabilities: frozenset[AuthorizationCapability] = frozenset()


class AuthorizationOutcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AccountAuthorizationResult:
    account_id: str
    outcome: AuthorizationOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AccountStatus:
    state: AccountState
    blockers: frozenset[AccountBlocker]
    authorization: AuthorizationStatus

    def allows(self, action: AccountAction) -> bool:
        if action == AccountAction.CANCEL_AUTHORIZATION:
            return self.authorization.state == AuthorizationState.AUTHORIZING
        if action == AccountAction.AUTHORIZE:
            return self.authorization.state != AuthorizationState.NOT_REQUIRED and not (
                self.blockers & _ACTION_BLOCKERS[action]
            )
        return not self.blockers & _ACTION_BLOCKERS[action]


def account_status(
    account: Account,
    rules: list[Rule],
    authorization: AuthorizationStatus,
    monitoring: Iterable[str] = (),
) -> AccountStatus:
    """Resolve all blockers once; monitoring warnings are not account-wide gates."""
    blockers = {
        blocker
        for blocked, blocker in (
            (not account.enabled, AccountBlocker.PAUSED),
            (not any(m.enabled for m in account.mailboxes), AccountBlocker.NO_ACTIVE_MAILBOXES),
            (not has_enabled_rule_for_account(rules, account.id), AccountBlocker.NO_ACTIVE_RULE),
        )
        if blocked
    }
    auth_blocker = _AUTHORIZATION_BLOCKERS.get(authorization.state)
    if auth_blocker is not None:
        blockers.add(auth_blocker)
    monitoring = set(monitoring)
    fallback = next(
        (
            state
            for value, state in (
                ("paused", AccountState.ATTENTION),
                ("setting_up", AccountState.SETTING_UP),
            )
            if value in monitoring
        ),
        AccountState.ACTIVE,
    )
    state = next((state for blocker, state in _STATE_PRIORITY if blocker in blockers), fallback)
    return AccountStatus(state, frozenset(blockers), authorization)


def authorization_binding(account: Account) -> tuple[object, ...]:
    shared_access = account.provider == MailProvider.MICROSOFT_GRAPH and any(
        mailbox.enabled
        and mailbox.address.strip().casefold() != account.username.strip().casefold()
        for mailbox in account.mailboxes
    )
    return credential_binding(account) + (shared_access,)


def authorization_binding_covers(
    granted: tuple[object, ...],
    requested: tuple[object, ...],
    *,
    capabilities: frozenset[AuthorizationCapability] = frozenset(),
) -> bool:
    """A verified shared-mail grant also covers the same identity's own mailbox."""
    return (
        len(granted) == len(requested)
        and granted[:-1] == requested[:-1]
        and (
            bool(granted[-1])
            or AuthorizationCapability.SHARED_MAIL in capabilities
            or not requested[-1]
        )
    )


class _AuthorizationScope(Enum):
    CONFIGURATION = "configuration"
    CREDENTIAL_RECORD = "credential_record"


@dataclass(frozen=True, slots=True)
class _CachedAuthorization:
    binding: tuple[object, ...]
    status: AuthorizationStatus
    scope: _AuthorizationScope = _AuthorizationScope.CONFIGURATION


class AccountStatusService:
    """Cache credential inspection without retaining secrets or doing UI-thread I/O.

    The optional inspector supports source implementations without interactive OAuth.
    Production composition always supplies the provider-aware inspector.
    Accounts register the live configuration before any retry snapshot is inspected.
    """

    def __init__(
        self,
        inspect: Callable[[Account], AuthorizationStatus] | None = None,
        monitoring: Callable[[Account], Iterable[str]] | None = None,
        *,
        accounts: Iterable[Account] = (),
    ) -> None:
        self._inspect = inspect
        self._monitoring = monitoring or (lambda account: ())
        self._lock = threading.RLock()
        self._cache: dict[str, _CachedAuthorization] = {}
        self._versions: dict[str, int] = {}
        self._revision = 0
        for account in accounts:
            self.set_authorization(account, self.authorization(account))

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def authorization(self, account: Account) -> AuthorizationStatus:
        if account.auth_mode != AuthMode.OAUTH_USER:
            return AuthorizationStatus(AuthorizationState.NOT_REQUIRED)
        default = AuthorizationStatus(
            AuthorizationState.CHECKING if self._inspect else AuthorizationState.AUTHORIZED
        )
        with self._lock:
            cached = self._cache.get(account.id, _CachedAuthorization((), default))
            if cached.scope == _AuthorizationScope.CREDENTIAL_RECORD:
                return cached.status
            requested = authorization_binding(account)
            reusable = (
                cached.status.state == AuthorizationState.AUTHORIZED
                and authorization_binding_covers(
                    cached.binding, requested, capabilities=cached.status.capabilities
                )
            )
            return cached.status if cached.binding == requested or reusable else default

    def set_authorization(self, account: Account, status: AuthorizationStatus) -> None:
        with self._lock:
            cached = self._cache.get(account.id)
            scope = (
                cached.scope
                if cached is not None
                and status.state
                not in {AuthorizationState.AUTHORIZED, AuthorizationState.NOT_REQUIRED}
                else _AuthorizationScope.CONFIGURATION
            )
            self._store_authorization(account.id, authorization_binding(account), status, scope)

    def _store_authorization(
        self,
        account_id: str,
        binding: tuple[object, ...],
        status: AuthorizationStatus,
        scope: _AuthorizationScope = _AuthorizationScope.CONFIGURATION,
    ) -> None:
        """Publish a status while the caller owns the cache lock."""
        self._versions[account_id] = self._versions.get(account_id, 0) + 1
        self._cache[account_id] = _CachedAuthorization(binding, status, scope)
        self._revision += 1

    def _credential_failure(self, account: Account, status: AuthorizationStatus) -> None:
        """Apply a credential failure without adopting a retry's mailbox configuration."""
        with self._lock:
            binding = authorization_binding(account)
            cached = self._cache.get(account.id)
            if cached is not None:
                if cached.binding[:-1] != credential_binding(account):
                    return
                binding = cached.binding
            scope = cached.scope if cached is not None else _AuthorizationScope.CONFIGURATION
            self._store_authorization(account.id, binding, status, scope)

    def require_authorization(self, account: Account, detail: str = "") -> None:
        self._credential_failure(account, AuthorizationStatus(AuthorizationState.REQUIRED, detail))

    def credentials_unavailable(self, account: Account, detail: str = "") -> None:
        self._credential_failure(
            account, AuthorizationStatus(AuthorizationState.UNAVAILABLE, detail)
        )

    def credential_record_failed(self, account_id: str, status: AuthorizationStatus) -> None:
        """Publish a protected-store failure under the account's credential lock.

        Record failures gate every OAuth configuration of this account. Only the
        registered live binding may restore access; frozen retries
        cannot clear this gate. Configuration-only reports remain scoped to their
        identity through require_authorization/credentials_unavailable.
        """
        if status.state not in {AuthorizationState.REQUIRED, AuthorizationState.UNAVAILABLE}:
            raise ValueError(
                "A credential record failure must require authorization or be unavailable."
            )
        with self._lock:
            cached = self._cache.get(account_id)
            self._store_authorization(
                account_id,
                cached.binding if cached is not None else (),
                status,
                _AuthorizationScope.CREDENTIAL_RECORD,
            )

    def refresh(self, account: Account) -> AuthorizationStatus:
        """Inspect on a worker; discard results superseded by edits or authorization."""
        with self._lock:
            status = self.authorization(account)
            if status.state in {AuthorizationState.NOT_REQUIRED, AuthorizationState.AUTHORIZING}:
                return status
            cached = self._cache.get(account.id)
            binding = authorization_binding(account)
            if (
                cached is not None
                and cached.scope == _AuthorizationScope.CREDENTIAL_RECORD
                and cached.binding
                and cached.binding != binding
            ):
                return status
            version = self._versions.get(account.id, 0)
        scope = _AuthorizationScope.CONFIGURATION
        try:
            status = (
                self._inspect(account)
                if self._inspect
                else AuthorizationStatus(AuthorizationState.AUTHORIZED)
            )
        except CredentialError as exc:
            status = AuthorizationStatus(AuthorizationState.UNAVAILABLE, str(exc))
            scope = _AuthorizationScope.CREDENTIAL_RECORD
        except Exception as exc:
            status = AuthorizationStatus(AuthorizationState.UNAVAILABLE, str(exc))
        with self._lock:
            if version != self._versions.get(account.id, 0):
                return self.authorization(account)
            cached = self._cache.get(account.id)
            if scope == _AuthorizationScope.CREDENTIAL_RECORD:
                self.credential_record_failed(account.id, status)
                return status
            if cached is not None and cached.binding and cached.binding != binding:
                # Frozen retry settings must not replace the live account's status.
                return status
            self._store_authorization(account.id, binding, status)
            return status

    def resolve(
        self,
        account: Account,
        rules: list[Rule],
        monitoring: Iterable[str] | None = None,
        *,
        inspect: bool = False,
    ) -> AccountStatus:
        authorization = self.authorization(account)
        if inspect and authorization.state == AuthorizationState.CHECKING:
            authorization = self.refresh(account)
        return account_status(
            account,
            rules,
            authorization,
            self._monitoring(account) if monitoring is None else monitoring,
        )
