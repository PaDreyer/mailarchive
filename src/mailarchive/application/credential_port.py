"""Credential storage contract used by account and provider use cases."""

from __future__ import annotations

from typing import Protocol


class CredentialError(RuntimeError):
    pass


class CredentialStore(Protocol):
    def get(self, account_id: str) -> str | None: ...

    def set(self, account_id: str, password: str) -> None: ...

    def delete(self, account_id: str) -> None: ...
