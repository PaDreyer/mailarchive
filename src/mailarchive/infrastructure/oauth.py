from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from mailarchive.application.account_credentials import (
    account_credential_lock,
    load_credential_data,
    store_account_credentials,
)
from mailarchive.application.account_status import (
    AuthorizationCapability,
    AuthorizationState,
    AuthorizationStatus,
)
from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation, ProcessingStopped
from mailarchive.application.credential_port import CredentialError, CredentialStore
from mailarchive.application.errors import AuthorizationError, AuthorizationRequiredError
from mailarchive.domain.configuration import Account, AuthMode, MailProvider
from mailarchive.infrastructure.browser_authorization import BrowserAuthorization
from mailarchive.infrastructure.provider_config import (
    ProviderConfigurationError,
    require_microsoft_public_client_id,
)
from mailarchive.infrastructure.providers.http import HttpClient

GOOGLE_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
MICROSOFT_MAIL_READ_SCOPE = "https://graph.microsoft.com/Mail.Read"
MICROSOFT_MAIL_READ_SHARED_SCOPE = "https://graph.microsoft.com/Mail.Read.Shared"
MICROSOFT_IMAP_ACCESS_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
BROWSER_AUTHORIZATION_TIMEOUT_SECONDS = 120
TOKEN_REQUEST_TIMEOUT_SECONDS = 15

MICROSOFT_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


_INTERACTIVE_ERRORS = {
    "invalid_grant",
    "interaction_required",
    "login_required",
    "consent_required",
}


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
        name for name in required if not isinstance(value.get(name), str) or not value[name].strip()
    ]
    if missing:
        raise ValueError("The Google service-account file is missing: " + ", ".join(missing) + ".")
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


def _canonical_microsoft_scope(scope: str) -> str:
    """Only Graph permissions have equivalent short and resource-qualified names."""
    return scope.casefold().removeprefix("https://graph.microsoft.com/")


class OAuthManager:
    def __init__(
        self,
        credential_store: CredentialStore,
        google_service_account_factory: Callable[..., Any] | None = None,
        google_request_factory: Callable[[], Any] | None = None,
        google_user_flow_factory: Callable[..., Any] | None = None,
        microsoft_msal_module: Any | None = None,
        microsoft_public_client_id: str | None = None,
        cancelled: threading.Event | None = None,
        on_authorization_required: Callable[[Account, str], None] | None = None,
        on_credentials_unavailable: Callable[[Account, str], None] | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.cancelled = cancelled
        self.google_service_account_factory = google_service_account_factory
        self.google_request_factory = google_request_factory
        self.google_user_flow_factory = google_user_flow_factory
        self.microsoft_msal_module = microsoft_msal_module
        self.microsoft_public_client_id = microsoft_public_client_id
        self.on_authorization_required = on_authorization_required or (lambda account, detail: None)
        self.on_credentials_unavailable = on_credentials_unavailable or (
            lambda account, detail: None
        )

    def authorization_status(self, account: Account) -> AuthorizationStatus:
        """Inspect protected storage locally, without authority discovery or token refresh."""
        if account.auth_mode != AuthMode.OAUTH_USER:
            return AuthorizationStatus(AuthorizationState.NOT_REQUIRED)
        with account_credential_lock(account.id):
            data = load_credential_data(self.credential_store, account.id)
        if account.provider == MailProvider.GMAIL_API:
            authorized = self._google_authorization_matches(account, data.get("google_credentials"))
        else:
            return self._microsoft_cached_authorization(account, data)
        return AuthorizationStatus(
            AuthorizationState.AUTHORIZED if authorized else AuthorizationState.REQUIRED
        )

    @staticmethod
    def _google_has_read_access(credentials: dict[str, Any]) -> bool:
        scopes = credentials.get("scopes", [])
        scopes = scopes.split() if isinstance(scopes, str) else scopes
        return isinstance(scopes, (list, tuple)) and GOOGLE_GMAIL_READONLY_SCOPE in scopes

    @staticmethod
    def _google_credential_data(credentials: Any) -> dict[str, Any]:
        data = json.loads(credentials.to_json())
        granted_scopes = getattr(credentials, "granted_scopes", None)
        if granted_scopes is not None:
            data["scopes"] = granted_scopes
        return data

    @classmethod
    def _google_authorization_matches(cls, account: Account, credentials: Any) -> bool:
        if not isinstance(credentials, dict):
            return False
        identity = credentials.get("account", "")
        return (
            credentials.get("client_id") == account.client_id.strip()
            and isinstance(credentials.get("refresh_token"), str)
            and bool(credentials["refresh_token"].strip())
            and cls._google_has_read_access(credentials)
            and (
                identity in (None, "")
                or isinstance(identity, str)
                and identity.strip().casefold() == account.username.strip().casefold()
            )
        )

    def _microsoft_cached_authorization(
        self, account: Account, data: dict[str, Any]
    ) -> AuthorizationStatus:
        required = AuthorizationStatus(AuthorizationState.REQUIRED)
        serialized = data.get("msal_cache")
        if not isinstance(serialized, str) or not serialized:
            return required
        msal = self._microsoft_module()
        cache = msal.SerializableTokenCache()
        try:
            cache.deserialize(serialized)
        except (ValueError, TypeError):
            return required
        client_id, tenant = self._microsoft_client_configuration(account)
        tenant = self._microsoft_tenant_realm(tenant, data)
        identities = {
            (entry.get("home_account_id"), entry.get("environment"))
            for entry in cache.search(msal.TokenCache.CredentialType.ACCOUNT)
            if str(entry.get("username", "")).casefold() == account.username.strip().casefold()
            and (
                tenant in {"common", "organizations", "consumers"}
                or str(entry.get("realm", "")).casefold() == tenant.casefold()
            )
        }
        if len(identities) != 1:
            return required
        home_id, environment = identities.pop()
        scopes = {
            _canonical_microsoft_scope(scope) for scope in self._microsoft_delegated_scopes(account)
        }
        grants = [
            {_canonical_microsoft_scope(scope) for scope in str(entry.get("target", "")).split()}
            for entry in cache.search(
                msal.TokenCache.CredentialType.REFRESH_TOKEN,
                query={
                    "home_account_id": home_id,
                    "environment": environment,
                    "client_id": client_id,
                },
            )
            if entry.get("secret")
        ]
        shared_scopes = {
            _canonical_microsoft_scope(MICROSOFT_MAIL_READ_SCOPE),
            _canonical_microsoft_scope(MICROSOFT_MAIL_READ_SHARED_SCOPE),
        }
        capabilities = frozenset(
            {AuthorizationCapability.SHARED_MAIL}
            if account.provider == MailProvider.MICROSOFT_GRAPH
            and any(shared_scopes <= grant for grant in grants)
            else ()
        )
        return AuthorizationStatus(
            AuthorizationState.AUTHORIZED
            if any(scopes <= grant for grant in grants)
            else AuthorizationState.REQUIRED,
            capabilities=capabilities,
        )

    @classmethod
    def _microsoft_tenant_realm(cls, tenant: str, data: dict[str, Any]) -> str:
        """Resolve a tenant alias only from the binding recorded during sign-in."""
        tenant = tenant.casefold()
        if tenant in {"common", "organizations", "consumers"}:
            return tenant
        try:
            return str(UUID(tenant))
        except ValueError:
            pass
        binding = data.get("microsoft_tenant")
        if (
            isinstance(binding, dict)
            and binding.get("authority") == cls._microsoft_authority(tenant)
            and isinstance(binding.get("realm"), str)
        ):
            try:
                return str(UUID(binding["realm"]))
            except ValueError:
                pass
        return tenant

    def _invalidate_user_authorization(self, account: Account, error: Exception) -> None:
        try:
            data = load_credential_data(self.credential_store, account.id)
            data.pop("google_credentials", None)
            data.pop("msal_cache", None)
            data.pop("microsoft_tenant", None)
            store_account_credentials(self.credential_store, account, data, replace=True)
        except CredentialError as exc:
            self.on_credentials_unavailable(account, str(exc))
            raise
        self.on_authorization_required(account, str(error))

    def authorize_google(self, account: Account) -> None:
        with account_credential_lock(account.id):
            self._authorize_google(account)

    def _authorize_google(self, account: Account) -> None:
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
            autogenerate_code_verifier=True,
        )
        with BrowserAuthorization(self.cancelled) as receiver:
            flow.redirect_uri = receiver.redirect_uri + "/"
            auth_uri, state = flow.authorization_url(
                access_type="offline", prompt="consent", login_hint=account.username
            )
            response = receiver.get_auth_response(
                auth_uri=auth_uri, state=state, timeout=BROWSER_AUTHORIZATION_TIMEOUT_SECONDS
            )
            if response is None:
                raise AuthorizationError("Browser sign-in timed out. You can try again.")
            authorization_response = flow.redirect_uri.replace("http://", "https://", 1)
            flow.fetch_token(
                authorization_response=authorization_response + "?" + urlencode(response),
                timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
            credentials = flow.credentials
        if self.cancelled is not None and self.cancelled.is_set():
            raise AuthorizationError("Authorization cancelled.")
        credential_data = self._google_credential_data(credentials)
        if not self._google_authorization_matches(account, credential_data):
            raise AuthorizationError(
                "Google sign-in did not provide matching, refreshable Gmail read access. Authorize again."
            )
        cancellation = (
            Cancellation(self.cancelled.is_set, reason="Authorization cancelled.")
            if self.cancelled
            else NO_CANCELLATION
        )
        try:
            profile = HttpClient().get_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                str(credential_data["token"]),
                cancellation=cancellation,
            )
            cancellation.checkpoint()
        except ProcessingStopped as exc:
            raise AuthorizationError(str(exc)) from exc
        identity = profile.get("emailAddress")
        if (
            not isinstance(identity, str)
            or identity.strip().casefold() != account.username.strip().casefold()
        ):
            raise AuthorizationError(
                "The signed-in Google identity does not match the configured mailbox. "
                "Authorize the intended Google account."
            )
        credential_data["account"] = identity
        store_account_credentials(
            self.credential_store,
            account,
            {"google_credentials": credential_data},
        )

    def google_access_token(
        self, account: Account, *, mailbox_address: str | None = None, force_refresh: bool = False
    ) -> str:
        with account_credential_lock(account.id):
            try:
                return self._google_access_token(
                    account, mailbox_address=mailbox_address, force_refresh=force_refresh
                )
            except AuthorizationRequiredError as exc:
                self._invalidate_user_authorization(account, exc)
                raise
            except CredentialError as exc:
                self.on_credentials_unavailable(account, str(exc))
                raise

    def _google_access_token(
        self, account: Account, *, mailbox_address: str | None = None, force_refresh: bool = False
    ) -> str:
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            return self._google_application_access_token(account, mailbox_address=mailbox_address)
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
            raise AuthorizationRequiredError(
                "Google authorization is required. Select the account and choose Authorize."
            )
        credentials = Credentials.from_authorized_user_info(
            credential_info,
            scopes=[GOOGLE_GMAIL_READONLY_SCOPE],
        )
        if force_refresh or not credentials.valid:
            if not credentials.refresh_token:
                raise AuthorizationRequiredError(
                    "Google authorization has expired. Select the account and authorize it again."
                )
            try:
                credentials.refresh(Request())
            except Exception as exc:
                if any(
                    isinstance(arg, dict) and arg.get("error") in _INTERACTIVE_ERRORS
                    for arg in exc.args
                ):
                    raise AuthorizationRequiredError(
                        "Google authorization has expired. Authorize the account again."
                    ) from exc
                raise AuthorizationError(
                    "Could not refresh Google authorization. Check the connection and "
                    "reauthorize the account if access has expired or been revoked."
                ) from exc
            credential_data = self._google_credential_data(credentials)
            if not self._google_has_read_access(credential_data):
                raise AuthorizationRequiredError(
                    "Google authorization no longer provides Gmail read access. "
                    "Authorize the account again."
                )
            store_account_credentials(
                self.credential_store,
                account,
                {"google_credentials": credential_data},
            )
        if not credentials.valid or not credentials.token:
            raise AuthorizationRequiredError(
                "Google authorization has expired. Select the account and authorize it again."
            )
        return credentials.token

    def _google_application_access_token(
        self, account: Account, *, mailbox_address: str | None = None
    ) -> str:
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
            ).with_subject(mailbox_address or account.username)
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
        with account_credential_lock(account.id):
            self._authorize_microsoft(account)

    def _authorize_microsoft(self, account: Account) -> None:
        msal = self._microsoft_module()
        cache = msal.SerializableTokenCache()
        client_id, tenant_id = self._microsoft_client_configuration(account)
        application = msal.PublicClientApplication(
            client_id,
            authority=self._microsoft_authority(tenant_id),
            token_cache=cache,
            timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
        )
        with BrowserAuthorization(self.cancelled) as receiver:
            result = application.acquire_token_interactive(
                scopes=self._microsoft_delegated_scopes(account),
                login_hint=account.username,
                port=receiver.get_port(),
                timeout=BROWSER_AUTHORIZATION_TIMEOUT_SECONDS,
                auth_code_receiver=receiver,
            )
        if self.cancelled is not None and self.cancelled.is_set():
            raise AuthorizationError("Authorization cancelled.")
        if not isinstance(result, dict) or "access_token" not in result:
            raise _authorization_error(result, "Microsoft authorization failed.")
        matching_accounts = application.get_accounts(username=account.username)
        if len(matching_accounts) != 1:
            raise AuthorizationError(
                "The signed-in Microsoft identity does not uniquely match the configured "
                "mailbox. Sign out in the browser and authorize the intended account."
            )
        updates: dict[str, Any] = {"msal_cache": cache.serialize()}
        realm = matching_accounts[0].get("realm")
        if isinstance(realm, str) and realm:
            updates["microsoft_tenant"] = {
                "authority": self._microsoft_authority(tenant_id.casefold()),
                "realm": realm.casefold(),
            }
        store_account_credentials(
            self.credential_store,
            account,
            updates,
            replace=True,
        )

    def microsoft_access_token(self, account: Account, *, force_refresh: bool = False) -> str:
        with account_credential_lock(account.id):
            try:
                return self._microsoft_access_token(account, force_refresh=force_refresh)
            except AuthorizationRequiredError as exc:
                self._invalidate_user_authorization(account, exc)
                raise
            except CredentialError as exc:
                self.on_credentials_unavailable(account, str(exc))
                raise

    def _microsoft_access_token(self, account: Account, *, force_refresh: bool = False) -> str:
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
                timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
            if force_refresh:
                # acquire_token_for_client rejects force_refresh; invalidate only
                # this application's cached access tokens through MSAL's public API.
                application.remove_tokens_for_client()
            result = application.acquire_token_for_client(scopes=[MICROSOFT_DEFAULT_SCOPE])
        else:
            application = msal.PublicClientApplication(
                client_id,
                authority=self._microsoft_authority(tenant_id),
                token_cache=cache,
                timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
            accounts = application.get_accounts(username=account.username)
            if not accounts:
                raise AuthorizationRequiredError(
                    "Microsoft authorization is required. Select the account and choose Authorize."
                )
            if len(accounts) != 1:
                raise AuthorizationRequiredError(
                    "More than one cached Microsoft identity matches this mailbox. Authorize "
                    "the account again to select it unambiguously."
                )
            scopes = self._microsoft_delegated_scopes(account)
            if account.provider == MailProvider.MICROSOFT_GRAPH:
                scopes = self._microsoft_graph_cached_scopes(
                    cache,
                    client_id,
                    accounts[0],
                    application.authority.tenant,
                    scopes,
                    force_refresh=force_refresh,
                )
            result = application.acquire_token_silent_with_error(
                scopes,
                account=accounts[0],
                force_refresh=force_refresh,
            )
            if not result:
                raise AuthorizationRequiredError(
                    "Microsoft authorization has expired. Select the account and authorize it again."
                )
        if cache.has_state_changed:
            store_account_credentials(
                self.credential_store,
                account,
                {"msal_cache": cache.serialize()},
            )
        if not isinstance(result, dict) or "access_token" not in result:
            if (
                account.auth_mode == AuthMode.OAUTH_USER
                and isinstance(result, dict)
                and (result.get("error") in _INTERACTIVE_ERRORS)
            ):
                raise AuthorizationRequiredError(
                    str(result.get("error_description") or result["error"])
                )
            raise _authorization_error(result, "Could not obtain a Microsoft access token.")
        return str(result["access_token"])

    @staticmethod
    def _microsoft_graph_cached_scopes(
        cache: Any,
        client_id: str,
        identity: dict[str, Any],
        realm: str,
        scopes: list[str],
        *,
        force_refresh: bool,
    ) -> list[str]:
        """Use cached Graph spellings; let MSAL enforce token validity and renewal."""
        required = {_canonical_microsoft_scope(scope) for scope in scopes}
        # Materialize before removal: MSAL's search holds a lock and iterates its cache.
        entries = list(
            cache.search(
                cache.CredentialType.ACCESS_TOKEN,
                query={
                    "client_id": client_id,
                    "home_account_id": identity.get("home_account_id"),
                    "environment": identity.get("environment"),
                    "realm": realm,
                },
            )
        )
        candidates = []
        for entry in entries:
            spellings = {
                _canonical_microsoft_scope(scope): scope for scope in entry["target"].split()
            }
            if not required <= spellings.keys():
                continue
            if force_refresh:
                # A rejected token must not survive under another equivalent spelling.
                cache.remove_at(entry)
            else:
                candidates.append(
                    (
                        int(entry["expires_on"]),
                        [spellings[_canonical_microsoft_scope(scope)] for scope in scopes],
                    )
                )
        return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else scopes

    def _microsoft_client_parts(self, account: Account) -> tuple[Any, Any, dict[str, Any]]:
        msal = self._microsoft_module()
        data = load_credential_data(self.credential_store, account.id)
        cache = msal.SerializableTokenCache()
        serialized_cache = data.get("msal_cache")
        if isinstance(serialized_cache, str) and serialized_cache:
            cache.deserialize(serialized_cache)
        return msal, cache, data

    def _microsoft_module(self) -> Any:
        msal = self.microsoft_msal_module
        if msal is None:
            try:
                import msal
            except ImportError as exc:
                raise AuthorizationError(
                    "Microsoft OAuth support is not installed. Reinstall MailArchive with its OAuth dependencies."
                ) from exc
        return msal

    def _microsoft_client_configuration(self, account: Account) -> tuple[str, str]:
        client_id = account.client_id.strip()
        if account.auth_mode == AuthMode.OAUTH_USER and not client_id:
            client_id = (self.microsoft_public_client_id or "").strip()
            if not client_id:
                try:
                    client_id = require_microsoft_public_client_id()
                except ProviderConfigurationError as exc:
                    raise AuthorizationError(
                        "Microsoft sign-in is not configured in this MailArchive build."
                    ) from exc
        if not client_id:
            raise AuthorizationError(
                "The Microsoft Entra application client ID is missing. Edit the account and add it."
            )
        tenant_id = account.tenant_id.strip()
        if account.auth_mode == AuthMode.OAUTH_USER and not tenant_id:
            tenant_id = "common"
        return client_id, tenant_id

    @staticmethod
    def _microsoft_delegated_scopes(account: Account) -> list[str]:
        if account.provider == MailProvider.MICROSOFT_GRAPH:
            scopes = [MICROSOFT_MAIL_READ_SCOPE]
            if any(
                mailbox.enabled
                and mailbox.address.strip().casefold() != account.username.strip().casefold()
                for mailbox in account.mailboxes
            ):
                scopes.append(MICROSOFT_MAIL_READ_SHARED_SCOPE)
            return scopes
        if account.provider == MailProvider.GENERIC_IMAP:
            return [MICROSOFT_IMAP_ACCESS_SCOPE]
        raise AuthorizationError("This account does not support Microsoft delegated access.")

    @staticmethod
    def _microsoft_authority(tenant_id: str) -> str:
        return f"https://login.microsoftonline.com/{tenant_id}"


def authorize_account(
    account: Account,
    credential_store: CredentialStore,
    cancelled: threading.Event | None = None,
) -> None:
    manager = OAuthManager(credential_store, cancelled=cancelled)
    if account.provider == MailProvider.GMAIL_API:
        if account.auth_mode == AuthMode.OAUTH_USER:
            manager.authorize_google(account)
        else:
            raise AuthorizationError("Google Workspace application access is not interactive.")
    elif (
        account.provider
        in {
            MailProvider.GENERIC_IMAP,
            MailProvider.MICROSOFT_GRAPH,
        }
        and account.auth_mode == AuthMode.OAUTH_USER
    ):
        manager.authorize_microsoft(account)
    else:
        raise AuthorizationError("This account does not use interactive OAuth authorization.")
