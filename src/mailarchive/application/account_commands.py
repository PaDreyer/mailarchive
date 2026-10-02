"""Account configuration input passed from the editor to the application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mailarchive.domain.configuration import Account


@dataclass(frozen=True, slots=True)
class AccountSubmission:
    account: Account
    credential_updates: dict[str, Any]
    replace_credentials: bool
