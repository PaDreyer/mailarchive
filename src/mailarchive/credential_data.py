from __future__ import annotations

import json
import threading
from typing import Any

from mailarchive.credentials import CredentialStore
from mailarchive.models import Account, AuthMode, MailProvider

_FORMAT_VERSION = 1
_ACCOUNT_CREDENTIAL_LOCKS: dict[str, threading.RLock] = {}
_ACCOUNT_CREDENTIAL_LOCKS_GUARD = threading.Lock()


def account_credential_lock(account_id: str) -> threading.RLock:
    """Return the process-wide lock protecting one account's credential lifecycle."""
    with _ACCOUNT_CREDENTIAL_LOCKS_GUARD:
        return _ACCOUNT_CREDENTIAL_LOCKS.setdefault(account_id, threading.RLock())


def load_credential_data(store: CredentialStore, account_id: str) -> dict[str, Any]:
    raw = store.get(account_id)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        # Versions before provider support stored the IMAP password directly.
        return {"password": raw}
    if isinstance(value, dict) and value.get("format_version") == _FORMAT_VERSION:
        return {key: item for key, item in value.items() if key != "format_version"}
    return {"password": raw}


def save_credential_data(
    store: CredentialStore,
    account_id: str,
    value: dict[str, Any],
) -> None:
    payload = dict(value)
    payload["format_version"] = _FORMAT_VERSION
    store.set(account_id, json.dumps(payload, separators=(",", ":")))


def update_credential_data(
    store: CredentialStore,
    account_id: str,
    **updates: Any,
) -> dict[str, Any]:
    value = load_credential_data(store, account_id)
    value.update(updates)
    save_credential_data(store, account_id, value)
    return value


def credential_keys_for(account: Account) -> frozenset[str]:
    """Return the credential fields that are valid for an account configuration."""
    if account.provider == MailProvider.GENERIC_IMAP:
        if account.auth_mode == AuthMode.OAUTH_USER:
            return frozenset({"msal_cache"})
        return frozenset({"password"})
    if account.provider == MailProvider.GMAIL_API:
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            return frozenset({"google_service_account"})
        return frozenset({"google_credentials", "oauth_client_secret"})
    if account.auth_mode == AuthMode.OAUTH_APPLICATION:
        return frozenset({"client_secret", "msal_cache"})
    return frozenset({"msal_cache"})


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
        existing = {} if replace else load_credential_data(store, account.id)
        credentials = {key: value for key, value in existing.items() if key in allowed_keys}
        credentials.update({key: value for key, value in updates.items() if key in allowed_keys})
        if credentials:
            save_credential_data(store, account.id, credentials)
        elif existing or replace:
            store.delete(account.id)
