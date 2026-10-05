"""Synthetic protected credentials for offline OAuth readiness tests."""

import json

from mailarchive.domain.configuration import Account


def microsoft_cache(account: Account, scopes: list[str]) -> str:
    identity = {"home_account_id": "user.tenant", "environment": "login.microsoftonline.com"}
    return json.dumps(
        {
            "Account": {
                "identity": {
                    **identity,
                    "username": account.username,
                    "realm": account.tenant_id or "tenant",
                    "authority_type": "MSSTS",
                }
            },
            "RefreshToken": {
                "refresh": {
                    **identity,
                    "client_id": account.client_id,
                    "target": " ".join(scopes),
                    "secret": "synthetic-refresh",
                }
            },
        }
    )
