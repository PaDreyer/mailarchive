from __future__ import annotations

import json
from typing import Any

from mailarchive.credentials import CredentialStore


_FORMAT_VERSION = 1


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
        return value
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
