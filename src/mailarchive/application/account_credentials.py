from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any
from weakref import WeakValueDictionary

from mailarchive.application.credential_port import CredentialStore
from mailarchive.domain.configuration import Account, AuthMode, MailProvider

_FORMAT_VERSION = 1
_BINDING_KEY = "credential_binding"
_BINDING_VERSION_KEY = "credential_binding_version"
_BINDING_VERSION = 2
_ACCOUNT_CREDENTIAL_LOCKS: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_ACCOUNT_CREDENTIAL_LOCKS_GUARD = threading.Lock()


class CredentialIdentityError(RuntimeError):
    """A saved remote identity cannot use another identity's protected record."""


def credential_binding(account: Account) -> tuple[object, ...]:
    """Configuration that binds stored credentials to a remote identity."""
    common: tuple[object, ...] = (account.provider, account.auth_mode)
    if account.provider == MailProvider.GENERIC_IMAP:
        common += (
            account.host.strip().casefold(),
            account.port,
            account.use_ssl,
            account.username
            if account.auth_mode == AuthMode.PASSWORD
            else account.username.strip().casefold(),
        )
        if account.auth_mode != AuthMode.OAUTH_USER:
            return common
    if account.auth_mode == AuthMode.OAUTH_USER:
        return common + (
            account.client_id.strip(),
            account.tenant_id.strip().casefold(),
            account.username.strip().casefold(),
        )
    if account.provider == MailProvider.MICROSOFT_GRAPH:
        return common + (account.client_id.strip(), account.tenant_id.strip().casefold())
    return common


def account_credential_lock(account_id: str) -> threading.RLock:
    """Return the process-wide lock protecting one account's credential lifecycle."""
    with _ACCOUNT_CREDENTIAL_LOCKS_GUARD:
        return _ACCOUNT_CREDENTIAL_LOCKS.setdefault(account_id, threading.RLock())


def _legacy_binding(account: Account) -> list[object]:
    binding = list(credential_binding(account))
    if account.provider == MailProvider.GENERIC_IMAP and account.auth_mode == AuthMode.PASSWORD:
        binding[-1] = account.username.strip().casefold()
    return binding


def _credential_record(store: CredentialStore, account_id: str) -> tuple[dict[str, Any], Any, int]:
    raw = store.get(account_id)
    if not raw:
        return {}, None, 1
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        # Versions before provider support stored the IMAP password directly.
        return {"password": raw}, None, 1
    if isinstance(value, dict) and value.get("format_version") == _FORMAT_VERSION:
        return (
            {
                key: item
                for key, item in value.items()
                if key not in {"format_version", _BINDING_KEY, _BINDING_VERSION_KEY}
            },
            value.get(_BINDING_KEY),
            value.get(_BINDING_VERSION_KEY, 1),
        )
    return {"password": raw}, None, 1


def load_credential_data(store: CredentialStore, account_id: str) -> dict[str, Any]:
    return _credential_record(store, account_id)[0]


def bind_legacy_account_credentials(store: CredentialStore, account: Account) -> bool:
    """Pin an old record to registered settings before those settings are edited."""
    with account_credential_lock(account.id):
        data, binding, version = _credential_record(store, account.id)
        legacy_match = version == 1 and binding == _legacy_binding(account)
        if data and (binding is None or legacy_match):
            save_credential_data(store, account.id, data, binding=list(credential_binding(account)))
            return True
        return False


def load_account_credential_data(
    store: CredentialStore,
    account: Account,
    live_account: Callable[[str], Account | None] | None = None,
    *,
    verify_legacy: Callable[[dict[str, Any]], bool] | None = None,
    bind_legacy: bool = True,
) -> dict[str, Any]:
    """Read credentials only for their bound identity, never adopt a retry's identity.

    A resolver reads registered live settings without acquiring the UI lock. Saves
    publish settings and credentials under the same account credential lock.
    verify_legacy is reserved for OAuth records with independently checked identity.
    """
    with account_credential_lock(account.id):
        data, binding, version = _credential_record(store, account.id)
        if not data:
            return data
        requested = list(credential_binding(account))
        legacy_password = (
            version == 1
            and account.provider == MailProvider.GENERIC_IMAP
            and account.auth_mode == AuthMode.PASSWORD
        )
        if binding is None or legacy_password:
            current = live_account(account.id) if live_account is not None else None
            verified = (
                binding is None
                and current is None
                and verify_legacy is not None
                and verify_legacy(data)
            )
            if not verified and (current is None or list(credential_binding(current)) != requested):
                raise CredentialIdentityError(
                    "The saved remote identity has no matching bound credentials. "
                    "Save credentials for this account before accessing mail."
                )
            if binding is not None and (current is None or binding != _legacy_binding(current)):
                raise CredentialIdentityError("Credentials belong to another remote identity.")
            if bind_legacy and not verified:
                save_credential_data(store, account.id, data, binding=requested)
        elif binding != requested:
            raise CredentialIdentityError(
                "The saved remote identity does not match the stored credentials. "
                "Its unfinished downloads remain available for matching credentials."
            )
        return data


def save_credential_data(
    store: CredentialStore,
    account_id: str,
    value: dict[str, Any],
    *,
    binding: list[object] | None = None,
) -> None:
    with account_credential_lock(account_id):
        payload = dict(value)
        if binding is None:
            _, binding, version = _credential_record(store, account_id)
        else:
            version = _BINDING_VERSION
        if binding is not None:
            payload[_BINDING_KEY] = binding
            payload[_BINDING_VERSION_KEY] = version
        payload["format_version"] = _FORMAT_VERSION
        store.set(account_id, json.dumps(payload, separators=(",", ":")))


def update_credential_data(
    store: CredentialStore,
    account_id: str,
    **updates: Any,
) -> dict[str, Any]:
    with account_credential_lock(account_id):
        value = load_credential_data(store, account_id)
        value.update(updates)
        save_credential_data(store, account_id, value)
        return value


def credential_keys_for(account: Account) -> frozenset[str]:
    """Return the credential fields that are valid for an account configuration."""
    if account.provider == MailProvider.GENERIC_IMAP:
        if account.auth_mode == AuthMode.OAUTH_USER:
            return frozenset({"msal_cache", "microsoft_tenant"})
        return frozenset({"password"})
    if account.provider == MailProvider.GMAIL_API:
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            return frozenset({"google_service_account"})
        return frozenset({"google_credentials", "oauth_client_secret"})
    if account.auth_mode == AuthMode.OAUTH_APPLICATION:
        return frozenset({"client_secret", "msal_cache"})
    return frozenset({"msal_cache", "microsoft_tenant"})


def store_account_credentials(
    store: CredentialStore,
    account: Account,
    updates: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    """Persist only credentials compatible with the account's provider and auth mode."""
    with account_credential_lock(account.id):
        allowed_keys = credential_keys_for(account)
        existing, binding, version = (
            ({}, None, _BINDING_VERSION) if replace else _credential_record(store, account.id)
        )
        if binding is not None and version == 1:
            # This command receives the registered identity (or an explicit save).
            # Remote reads require the live resolver before adopting old bindings.
            if binding == _legacy_binding(account):
                binding = list(credential_binding(account))
        if binding is not None and binding != list(credential_binding(account)):
            raise CredentialIdentityError("Credentials belong to another remote identity.")
        credentials = {key: value for key, value in existing.items() if key in allowed_keys}
        credentials.update({key: value for key, value in updates.items() if key in allowed_keys})
        if credentials:
            save_credential_data(
                store, account.id, credentials, binding=list(credential_binding(account))
            )
        elif existing or replace:
            store.delete(account.id)
