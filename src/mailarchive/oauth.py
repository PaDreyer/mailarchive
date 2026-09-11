from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mailarchive.credential_data import load_credential_data, save_credential_data
from mailarchive.credentials import CredentialStore
from mailarchive.models import Account, AuthMode, MailProvider


GOOGLE_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
MICROSOFT_MAIL_READ_SCOPE = "https://graph.microsoft.com/Mail.Read"
MICROSOFT_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


class AuthorizationError(RuntimeError):
    pass


def parse_google_service_account_file(path: str) -> dict[str, str]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read the Google service-account file: {exc}") from exc
    except ValueError as exc:
        raise ValueError("The Google service-account file is not valid JSON.") from exc
    if not isinstance(value, dict) or value.get("type") != "service_account":
        raise ValueError("Select a Google service-account JSON key file.")
    required = ("client_email", "private_key", "token_uri")
    missing = [
        name
        for name in required
        if not isinstance(value.get(name), str) or not value[name].strip()
    ]
    if missing:
        raise ValueError(
            "The Google service-account file is missing: " + ", ".join(missing) + "."
        )
    retained = (
        "type",
        "client_email",
        "private_key",
        "private_key_id",
        "token_uri",
        "universe_domain",
    )
    return {name: str(value[name]) for name in retained if value.get(name) is not None}


def _authorization_error(result: Any, fallback: str) -> AuthorizationError:
    if not isinstance(result, dict):
        return AuthorizationError(fallback)
    detail = result.get("error_description") or result.get("error") or fallback
    return AuthorizationError(str(detail))


class OAuthManager:
    def __init__(
        self,
        credential_store: CredentialStore,
        google_service_account_factory: Callable[..., Any] | None = None,
        google_request_factory: Callable[[], Any] | None = None,
        google_user_flow_factory: Callable[..., Any] | None = None,
        microsoft_msal_module: Any | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.google_service_account_factory = google_service_account_factory
        self.google_request_factory = google_request_factory
        self.google_user_flow_factory = google_user_flow_factory
        self.microsoft_msal_module = microsoft_msal_module

    def authorize_google(self, account: Account) -> None:
        if not account.client_id.strip():
            raise AuthorizationError(
                "The Google OAuth desktop client ID is missing. Edit the account and add it."
            )
        flow_factory = self.google_user_flow_factory
        if flow_factory is None:
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ImportError as exc:
                raise AuthorizationError(
                    "Google OAuth support is not installed. Reinstall MailArchive with its OAuth dependencies."
                ) from exc
            flow_factory = InstalledAppFlow.from_client_config

        data = load_credential_data(self.credential_store, account.id)
        client_config = {
            "installed": {
                "client_id": account.client_id.strip(),
                "client_secret": str(data.get("oauth_client_secret", "")),
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = flow_factory(
            client_config,
            scopes=[GOOGLE_GMAIL_READONLY_SCOPE],
        )
        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
            authorization_prompt_message="Opening Google authorization in your browser...",
            success_message="Authorization complete. You can close this browser window.",
            access_type="offline",
            prompt="consent",
        )
        data["google_credentials"] = json.loads(credentials.to_json())
        save_credential_data(self.credential_store, account.id, data)

    def google_access_token(self, account: Account) -> str:
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            return self._google_application_access_token(account)
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise AuthorizationError(
                "Google OAuth support is not installed. Reinstall MailArchive with its OAuth dependencies."
            ) from exc

        data = load_credential_data(self.credential_store, account.id)
        credential_info = data.get("google_credentials")
        if not isinstance(credential_info, dict):
            raise AuthorizationError(
                "Google authorization is required. Select the account and choose Authorize."
            )
        credentials = Credentials.from_authorized_user_info(
            credential_info,
            scopes=[GOOGLE_GMAIL_READONLY_SCOPE],
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            data["google_credentials"] = json.loads(credentials.to_json())
            save_credential_data(self.credential_store, account.id, data)
        if not credentials.valid or not credentials.token:
            raise AuthorizationError(
                "Google authorization has expired. Select the account and authorize it again."
            )
        return credentials.token

    def _google_application_access_token(self, account: Account) -> str:
        factory = self.google_service_account_factory
        request_factory = self.google_request_factory
        if factory is None:
            try:
                from google.oauth2.service_account import Credentials
            except ImportError as exc:
                raise AuthorizationError(
                    "Google OAuth support is not installed. Reinstall MailArchive with its OAuth dependencies."
                ) from exc
            factory = Credentials.from_service_account_info
        if request_factory is None:
            try:
                from google.auth.transport.requests import Request
            except ImportError as exc:
                raise AuthorizationError(
                    "Google OAuth support is not installed. Reinstall MailArchive with its OAuth dependencies."
                ) from exc
            request_factory = Request

        data = load_credential_data(self.credential_store, account.id)
        credential_info = data.get("google_service_account")
        if not isinstance(credential_info, dict):
            raise AuthorizationError(
                "A Google service-account JSON key is required. Edit the account and select the key file."
            )
        try:
            credentials = factory(
                credential_info,
                scopes=[GOOGLE_GMAIL_READONLY_SCOPE],
            ).with_subject(account.username)
            credentials.refresh(request_factory())
        except Exception as exc:
            raise AuthorizationError(
                f"Google Workspace application authorization failed: {exc}"
            ) from exc
        if not credentials.token:
            raise AuthorizationError("Google did not return a service-account access token.")
        return str(credentials.token)

    def authorize_microsoft(self, account: Account) -> None:
        if account.auth_mode != AuthMode.OAUTH_USER:
            raise AuthorizationError("Interactive authorization is only used for delegated access.")
        msal, cache, data = self._microsoft_client_parts(account)
        client_id, tenant_id = self._microsoft_client_configuration(account)
        application = msal.PublicClientApplication(
            client_id,
            authority=self._microsoft_authority(tenant_id),
            token_cache=cache,
        )
        result = application.acquire_token_interactive(
            scopes=[MICROSOFT_MAIL_READ_SCOPE],
            login_hint=account.username,
            port=0,
        )
        if "access_token" not in result:
            raise _authorization_error(result, "Microsoft authorization failed.")
        data["msal_cache"] = cache.serialize()
        save_credential_data(self.credential_store, account.id, data)

    def microsoft_access_token(self, account: Account) -> str:
        msal, cache, data = self._microsoft_client_parts(account)
        client_id, tenant_id = self._microsoft_client_configuration(account)
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            client_secret = str(data.get("client_secret", ""))
            if not client_secret:
                raise AuthorizationError("The Microsoft application client secret is missing.")
            application = msal.ConfidentialClientApplication(
                client_id,
                authority=self._microsoft_authority(tenant_id),
                client_credential=client_secret,
                token_cache=cache,
            )
            result = application.acquire_token_for_client(scopes=[MICROSOFT_DEFAULT_SCOPE])
        else:
            application = msal.PublicClientApplication(
                client_id,
                authority=self._microsoft_authority(tenant_id),
                token_cache=cache,
            )
            accounts = application.get_accounts(username=account.username) or application.get_accounts()
            if not accounts:
                raise AuthorizationError(
                    "Microsoft authorization is required. Select the account and choose Authorize."
                )
            result = application.acquire_token_silent(
                [MICROSOFT_MAIL_READ_SCOPE],
                account=accounts[0],
            )
            if not result:
                raise AuthorizationError(
                    "Microsoft authorization has expired. Select the account and authorize it again."
                )
        if cache.has_state_changed:
            data["msal_cache"] = cache.serialize()
            save_credential_data(self.credential_store, account.id, data)
        if not isinstance(result, dict) or "access_token" not in result:
            raise _authorization_error(result, "Could not obtain a Microsoft access token.")
        return str(result["access_token"])

    def _microsoft_client_parts(self, account: Account) -> tuple[Any, Any, dict[str, Any]]:
        msal = self.microsoft_msal_module
        if msal is None:
            try:
                import msal
            except ImportError as exc:
                raise AuthorizationError(
                    "Microsoft OAuth support is not installed. Reinstall MailArchive with its OAuth dependencies."
                ) from exc
        data = load_credential_data(self.credential_store, account.id)
        cache = msal.SerializableTokenCache()
        serialized_cache = data.get("msal_cache")
        if isinstance(serialized_cache, str) and serialized_cache:
            cache.deserialize(serialized_cache)
        return msal, cache, data

    def _microsoft_client_configuration(self, account: Account) -> tuple[str, str]:
        client_id = account.client_id.strip()
        if not client_id:
            raise AuthorizationError(
                "The Microsoft Entra application client ID is missing. Edit the account and add it."
            )
        tenant_id = account.tenant_id.strip()
        if account.auth_mode == AuthMode.OAUTH_USER and not tenant_id:
            tenant_id = "common"
        return client_id, tenant_id

    @staticmethod
    def _microsoft_authority(tenant_id: str) -> str:
        return f"https://login.microsoftonline.com/{tenant_id}"


def authorize_account(
    account: Account,
    credential_store: CredentialStore,
) -> None:
    manager = OAuthManager(credential_store)
    if account.provider == MailProvider.GMAIL_API:
        if account.auth_mode == AuthMode.OAUTH_USER:
            manager.authorize_google(account)
        else:
            raise AuthorizationError("Google Workspace application access is not interactive.")
    elif (
        account.provider == MailProvider.MICROSOFT_GRAPH
        and account.auth_mode == AuthMode.OAUTH_USER
    ):
        manager.authorize_microsoft(account)
    else:
        raise AuthorizationError("This account does not use interactive OAuth authorization.")
