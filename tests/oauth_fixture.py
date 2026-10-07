"""Synthetic protected credentials for offline OAuth readiness tests."""

import json
import time
from base64 import urlsafe_b64encode
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from mailarchive.application.account_credentials import (
    bind_legacy_account_credentials,
    update_credential_data,
)
from mailarchive.domain.configuration import Account


class _MsalResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, value):
        self.text = json.dumps(value)


class MicrosoftRefreshHttp:
    """Run real MSAL refresh handling against an isolated token endpoint."""

    def __init__(self, result):
        self.result = result
        self.refreshes = []

    def get(self, url, **kwargs):
        if not url.endswith("/v2.0/.well-known/openid-configuration"):
            raise AssertionError(f"Unexpected MSAL discovery request: {url}")
        authority = url.rsplit("/v2.0/", 1)[0]
        return _MsalResponse(
            {
                "authorization_endpoint": authority + "/oauth2/v2.0/authorize",
                "token_endpoint": authority + "/oauth2/v2.0/token",
                "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
            }
        )

    def post(self, url, data, **kwargs):
        if urlsplit(url).hostname != "login.microsoftonline.com":
            raise AssertionError(f"Unexpected MSAL token endpoint: {url}")
        if data.get("grant_type") != "refresh_token":
            raise AssertionError("Expected a refresh-token exchange")
        self.refreshes.append(url)
        return _MsalResponse(self.result)


class MicrosoftRequestsTransport(MicrosoftRefreshHttp):
    """Observe MSAL's default Requests transport, including its real timeout arguments."""

    def __init__(self, result, *, token_request=None):
        super().__init__(result)
        self.requests = []
        self.token_request = token_request

    def request(self, _session, method, url, **kwargs):
        self.requests.append((method, url, kwargs.get("timeout")))
        if method == "GET":
            if "/discovery/instance" in url:
                return _MsalResponse(
                    {
                        "tenant_discovery_endpoint": (
                            "https://login.microsoftonline.com/common/v2.0/"
                            ".well-known/openid-configuration"
                        ),
                        "metadata": [
                            {
                                "preferred_network": "login.microsoftonline.com",
                                "preferred_cache": "login.microsoftonline.com",
                                "aliases": ["login.microsoftonline.com"],
                            }
                        ],
                    }
                )
            return self.get(url, **kwargs)
        if method == "POST" and urlsplit(url).path.endswith("/oauth2/v2.0/token"):
            if self.token_request is not None:
                self.token_request()
            return _MsalResponse(self.result)
        raise AssertionError(f"Unexpected Microsoft request: {method} {url}")


class MicrosoftInteractiveTransport(MicrosoftRequestsTransport):
    """Let real MSAL exchange a browser code and build its own credential cache."""

    def __init__(self, account: Account, scopes: str):
        super().__init__({})
        self.account, self.scopes = deepcopy(account), scopes
        self.requested_scopes: list[str] = []

    @staticmethod
    def _encode(value):
        return urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def receive(self, *, auth_uri, state, **kwargs):
        query = parse_qs(urlsplit(auth_uri).query)
        self.requested_scopes.append(query["scope"][0])
        now = int(time.time())
        claims = {
            "iss": f"https://login.microsoftonline.com/{self.account.tenant_id}/v2.0",
            "aud": self.account.client_id,
            "sub": "synthetic-user",
            "oid": "synthetic-user",
            "tid": self.account.tenant_id,
            "preferred_username": self.account.username,
            "nonce": query["nonce"][0],
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
        }
        self.result = {
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": self.scopes,
            "client_info": self._encode({"uid": "synthetic-user", "utid": self.account.tenant_id}),
            "id_token": (
                self._encode({"alg": "RS256"}) + "." + self._encode(claims) + ".synthetic"
            ),
        }
        return {"code": "synthetic-code", "state": state}


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


def update_bound_credentials(store, account, **updates):
    """Publish synthetic records with an explicitly registered account identity."""
    update_credential_data(store, account.id, **updates)
    bind_legacy_account_credentials(store, account)
