import hashlib
import json
import socket
import tempfile
import threading
import unittest
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import msal

from mailarchive.application.account_credentials import (
    account_credential_lock,
    load_credential_data,
)
from mailarchive.application.credential_port import CredentialError
from mailarchive.application.errors import AuthorizationRequiredError
from mailarchive.domain.configuration import Account, AuthMode, MailProvider
from mailarchive.infrastructure.browser_authorization import BrowserAuthorization
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.oauth import (
    BROWSER_AUTHORIZATION_TIMEOUT_SECONDS,
    GOOGLE_GMAIL_READONLY_SCOPE,
    MICROSOFT_IMAP_ACCESS_SCOPE,
    MICROSOFT_MAIL_READ_SCOPE,
    TOKEN_REQUEST_TIMEOUT_SECONDS,
    AuthorizationError,
    OAuthManager,
    authorize_account,
    parse_google_service_account_file,
)
from mailarchive.infrastructure.provider_config import ProviderConfigurationError
from tests.concurrency import THREAD_TIMEOUT, ObservedLock
from tests.oauth_fixture import (
    MicrosoftRequestsTransport,
    microsoft_cache,
    update_bound_credentials,
)


class FakeServiceAccountCredentials:
    def __init__(self) -> None:
        self.subject = ""
        self.token = None
        self.request = None

    def with_subject(self, subject):
        self.subject = subject
        return self

    def refresh(self, request):
        self.request = request
        self.token = "service-account-token"


class FakeUserCredentials:
    def __init__(self, client_id="client-id"):
        self.client_id = client_id
        self.granted_scopes = None

    def to_json(self):
        return json.dumps(
            {
                "token": "user-token",
                "refresh_token": "refresh-token",
                "client_id": self.client_id,
                "client_secret": "application-client-secret",
                "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
            }
        )


class FakeUserFlow:
    def __init__(self, client_id="client-id") -> None:
        self.authorization_arguments = {}
        self.token_arguments = {}
        self.credentials = FakeUserCredentials(client_id)

    def authorization_url(self, **arguments):
        self.authorization_arguments = arguments
        return "https://example.org/authorize", "state"

    def fetch_token(self, **arguments):
        self.token_arguments = arguments


class FakeBrowserAuthorization:
    redirect_uri = "http://localhost:12345"

    def __init__(self, cancelled):
        self.cancelled = cancelled

    def get_port(self):
        return 12345

    def get_auth_response(self, **arguments):
        return {"code": "synthetic", "state": arguments["state"]}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeRefreshableGoogleCredentials:
    def __init__(
        self,
        *,
        expired=True,
        refresh_token="refresh-token",
        valid=False,
        token="old-token",
        refresh_hook=None,
    ) -> None:
        self.expired = expired
        self.refresh_token = refresh_token
        self.valid = valid
        self.token = token
        self.refresh_request = None
        self.refresh_hook = refresh_hook
        self.granted_scopes = None

    def refresh(self, request):
        if self.refresh_hook is not None:
            self.refresh_hook()
        self.refresh_request = request
        self.expired = False
        self.valid = True
        self.token = "refreshed-token"

    def to_json(self):
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "client_id": "client-id",
                "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
            }
        )


class FakeMsalCache:
    has_state_changed = True
    CredentialType = msal.TokenCache.CredentialType

    def search(self, credential_type, *, query):
        return iter(())

    def deserialize(self, value):
        self.value = value

    def serialize(self):
        return '{"cache":"value"}'


class FakePublicClientApplication:
    def __init__(
        self,
        client_id,
        authority,
        token_cache,
        *,
        accounts=None,
        silent_result=None,
        interactive_result=None,
        silent_hook=None,
        interactive_hook=None,
    ):
        self.client_id = client_id
        self.authority_url = authority
        self.authority = SimpleNamespace(tenant=urlsplit(authority).path.strip("/"))
        self.token_cache = token_cache
        self.accounts = [{"username": "me@example.com"}] if accounts is None else accounts
        self.silent_result = (
            {"access_token": "silent-token"} if silent_result is None else silent_result
        )
        self.interactive_result = (
            {"access_token": "delegated-token"}
            if interactive_result is None
            else interactive_result
        )
        self.silent_hook = silent_hook
        self.interactive_hook = interactive_hook
        self.account_queries = []
        self.silent_arguments = None
        self.force_refresh = None
        self.interactive_arguments = None

    def acquire_token_interactive(self, **arguments):
        self.interactive_arguments = arguments
        if self.interactive_hook is not None:
            self.interactive_hook()
        return self.interactive_result

    def get_accounts(self, username=None):
        self.account_queries.append(username)
        if username is not None:
            return [account for account in self.accounts if account.get("username") == username]
        return self.accounts

    def acquire_token_silent(self, scopes, account, *, force_refresh=False):
        self.silent_arguments = (scopes, account)
        self.force_refresh = force_refresh
        if self.silent_hook is not None:
            self.silent_hook()
        return self.silent_result

    def acquire_token_silent_with_error(self, scopes, account, *, force_refresh=False):
        return self.acquire_token_silent(scopes, account, force_refresh=force_refresh)


class FakeConfidentialClientApplication:
    def __init__(
        self,
        client_id,
        authority,
        client_credential,
        token_cache,
        result,
    ):
        self.client_id = client_id
        self.authority = authority
        self.client_credential = client_credential
        self.token_cache = token_cache
        self.result = result
        self.calls = []

    def acquire_token_for_client(self, *, scopes):
        self.calls.append("acquire_token_for_client")
        self.scopes = scopes
        return self.result

    def remove_tokens_for_client(self):
        self.calls.append("remove_tokens_for_client")


class FakeMsalModule:
    def __init__(
        self,
        *,
        accounts=None,
        silent_result=None,
        interactive_result=None,
        application_result=None,
        silent_hook=None,
        interactive_hook=None,
    ) -> None:
        self.applications = []
        self.caches = []
        self.accounts = accounts
        self.silent_result = silent_result
        self.interactive_result = interactive_result
        self.silent_hook = silent_hook
        self.interactive_hook = interactive_hook
        self.application_result = (
            {"access_token": "application-token"}
            if application_result is None
            else application_result
        )

    def SerializableTokenCache(self):
        cache = FakeMsalCache()
        self.caches.append(cache)
        return cache

    def PublicClientApplication(self, client_id, authority, token_cache, **kwargs):
        application = FakePublicClientApplication(
            client_id,
            authority,
            token_cache,
            accounts=self.accounts,
            silent_result=self.silent_result,
            interactive_result=self.interactive_result,
            silent_hook=self.silent_hook,
            interactive_hook=self.interactive_hook,
        )
        self.applications.append(application)
        return application

    def ConfidentialClientApplication(
        self,
        client_id,
        authority,
        client_credential,
        token_cache,
        **kwargs,
    ):
        application = FakeConfidentialClientApplication(
            client_id,
            authority,
            client_credential,
            token_cache,
            self.application_result,
        )
        self.applications.append(application)
        return application


class OAuthTests(unittest.TestCase):
    def test_real_msal_default_transport_bounds_delegated_and_application_requests(self):
        for mode in (AuthMode.OAUTH_USER, AuthMode.OAUTH_APPLICATION):
            with self.subTest(mode=mode):
                account = Account(
                    "Owner",
                    username="owner@example.org",
                    provider=MailProvider.MICROSOFT_GRAPH,
                    auth_mode=mode,
                    client_id="00000000-0000-0000-0000-000000000001",
                    tenant_id="12345678-1234-1234-1234-123456789abc",
                )
                store = MemoryCredentialStore()
                update_bound_credentials(
                    store,
                    account,
                    client_secret="synthetic-secret",
                    msal_cache=microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE]),
                )
                transport = MicrosoftRequestsTransport({"access_token": "synthetic-access"})
                with patch(
                    "requests.sessions.Session.request",
                    autospec=True,
                    side_effect=transport.request,
                ):
                    token = OAuthManager(store).microsoft_access_token(account, force_refresh=True)
                self.assertEqual(token, "synthetic-access")
                self.assertTrue(
                    any(method == "POST" for method, _url, _timeout in transport.requests)
                )
                self.assertTrue(
                    all(
                        timeout == TOKEN_REQUEST_TIMEOUT_SECONDS
                        for _method, _url, timeout in transport.requests
                    ),
                    transport.requests,
                )

    def test_real_msal_timeout_preserves_refresh_credentials_and_authorization(self):
        from requests.exceptions import Timeout

        account = Account(
            "Owner",
            username="owner@example.org",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="00000000-0000-0000-0000-000000000001",
            tenant_id="12345678-1234-1234-1234-123456789abc",
        )
        store = MemoryCredentialStore()
        update_bound_credentials(
            store, account, msal_cache=microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE])
        )
        retained = store.get(account.id)
        required, unavailable = Mock(), Mock()
        manager = OAuthManager(
            store, on_authorization_required=required, on_credentials_unavailable=unavailable
        )
        transport = MicrosoftRequestsTransport(
            {}, token_request=Mock(side_effect=Timeout("Token endpoint timed out"))
        )
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            with self.assertRaises(Timeout):
                manager.microsoft_access_token(account, force_refresh=True)
        self.assertEqual(store.get(account.id), retained)
        required.assert_not_called()
        unavailable.assert_not_called()

    def setUp(self):
        receiver = patch(
            "mailarchive.infrastructure.oauth.BrowserAuthorization", FakeBrowserAuthorization
        )
        receiver.start()
        self.addCleanup(receiver.stop)
        profile = patch(
            "mailarchive.infrastructure.providers.http.HttpClient.get_json",
            return_value={"emailAddress": "me@example.com"},
        )
        self.google_profile = profile.start()
        self.addCleanup(profile.stop)

    def test_locked_credential_store_reports_unavailable_without_requiring_new_consent(self):
        store = Mock()
        store.get.side_effect = CredentialError("Keyring locked")
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            account = Account(
                "Owner",
                username="owner@example.org",
                provider=provider,
                auth_mode=AuthMode.OAUTH_USER,
                client_id="client",
            )
            unavailable, required = Mock(), Mock()
            manager = OAuthManager(
                store,
                microsoft_msal_module=FakeMsalModule(),
                on_authorization_required=required,
                on_credentials_unavailable=unavailable,
            )
            get_token = (
                manager.google_access_token
                if provider == MailProvider.GMAIL_API
                else manager.microsoft_access_token
            )
            with self.assertRaises(CredentialError):
                get_token(account)
            unavailable.assert_called_once_with(account, "Keyring locked")
            required.assert_not_called()

    def test_google_permanent_refresh_failure_requires_sign_in_but_transient_failure_does_not(self):
        from google.auth.exceptions import RefreshError

        account = Account(
            "Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
        )
        for error, required in (
            (RefreshError("Expired", {"error": "invalid_grant"}), True),
            (RuntimeError("Network unavailable"), False),
        ):
            with self.subTest(required=required):
                store = MemoryCredentialStore()
                update_bound_credentials(
                    store,
                    account,
                    oauth_client_secret="synthetic-secret",
                    google_credentials={"client_id": "client", "refresh_token": "synthetic"},
                )
                on_required = Mock()
                credentials = FakeRefreshableGoogleCredentials(refresh_hook=Mock(side_effect=error))
                manager = OAuthManager(store, on_authorization_required=on_required)
                with patch(
                    "google.oauth2.credentials.Credentials.from_authorized_user_info",
                    return_value=credentials,
                ):
                    with self.assertRaises(
                        AuthorizationRequiredError if required else AuthorizationError
                    ):
                        manager.google_access_token(account)
                saved = load_credential_data(store, account.id)
                self.assertEqual(saved["oauth_client_secret"], "synthetic-secret")
                self.assertEqual("google_credentials" not in saved, required)
                self.assertEqual(on_required.call_count, int(required))

    def test_google_refresh_rejects_a_grant_without_gmail_read_access(self):
        account = Account(
            "Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
        )
        store, on_required = MemoryCredentialStore(), Mock()
        update_bound_credentials(
            store,
            account,
            oauth_client_secret="synthetic-secret",
            google_credentials={
                "client_id": account.client_id,
                "refresh_token": "refresh-token",
                "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
            },
        )
        credentials = FakeRefreshableGoogleCredentials()
        credentials.granted_scopes = ["openid"]
        manager = OAuthManager(store, on_authorization_required=on_required)
        with patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_info",
            return_value=credentials,
        ):
            with self.assertRaisesRegex(AuthorizationRequiredError, "Gmail read access"):
                manager.google_access_token(account)
        on_required.assert_called_once()
        saved = load_credential_data(store, account.id)
        self.assertNotIn("google_credentials", saved)
        self.assertEqual(saved["oauth_client_secret"], "synthetic-secret")

    def test_microsoft_refresh_errors_preserve_transient_failures_for_retry(self):
        account = Account(
            "Microsoft",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
        )
        for code, required in (
            ("interaction_required", True),
            ("invalid_grant", True),
            ("temporarily_unavailable", False),
        ):
            with self.subTest(code=code):
                store = MemoryCredentialStore()
                update_bound_credentials(store, account, msal_cache='{"old":"cache"}')
                on_required = Mock()
                manager = OAuthManager(
                    store,
                    on_authorization_required=on_required,
                    microsoft_msal_module=FakeMsalModule(silent_result={"error": code}),
                )
                with self.assertRaises(
                    AuthorizationRequiredError if required else AuthorizationError
                ):
                    manager.microsoft_access_token(account)
                self.assertEqual(
                    "msal_cache" not in load_credential_data(store, account.id), required
                )
                self.assertEqual(on_required.call_count, int(required))

    def test_invalidation_reports_the_changed_record_before_releasing_its_lock(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            with self.subTest(provider=provider):
                store = MemoryCredentialStore()
                account = Account("Owner", provider=provider, auth_mode=AuthMode.OAUTH_USER)
                update_bound_credentials(
                    store,
                    account,
                    google_credentials={"refresh_token": "synthetic"},
                    msal_cache="synthetic",
                )

                def on_required(failed_account, detail, account=account, store=store):
                    self.assertEqual(failed_account.id, account.id)
                    self.assertEqual(detail, "Revoked grant")
                    self.assertIsNone(store.get(account.id))
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        self.assertFalse(
                            executor.submit(
                                account_credential_lock(account.id).acquire, blocking=False
                            ).result(timeout=THREAD_TIMEOUT)
                        )

                callback = Mock(side_effect=on_required)
                manager = OAuthManager(store, on_authorization_required=callback)
                method = (
                    "google_access_token"
                    if provider == MailProvider.GMAIL_API
                    else "microsoft_access_token"
                )
                with patch.object(
                    manager,
                    "_" + method,
                    side_effect=AuthorizationRequiredError("Revoked grant"),
                ):
                    with self.assertRaises(AuthorizationRequiredError):
                        getattr(manager, method)(account)
                callback.assert_called_once()

    def test_failed_invalidation_reports_storage_failure_instead_of_successful_deletion(self):
        store = MemoryCredentialStore()
        account = Account(
            "Owner", provider=MailProvider.MICROSOFT_GRAPH, auth_mode=AuthMode.OAUTH_USER
        )
        update_bound_credentials(store, account, msal_cache="synthetic")
        previous = store.get(account.id)
        required, unavailable = Mock(), Mock()
        manager = OAuthManager(
            store, on_authorization_required=required, on_credentials_unavailable=unavailable
        )
        with (
            patch.object(store, "delete", side_effect=CredentialError("Keyring locked")),
            patch.object(
                manager,
                "_microsoft_access_token",
                side_effect=AuthorizationRequiredError("Revoked"),
            ),
        ):
            with self.assertRaisesRegex(CredentialError, "Keyring locked"):
                manager.microsoft_access_token(account)
        self.assertEqual(store.get(account.id), previous)
        required.assert_not_called()
        unavailable.assert_called_once_with(account, "Keyring locked")

    def test_google_user_sign_in_reports_missing_account_client_id(self) -> None:
        account = Account(
            label="Existing Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
        )
        manager = OAuthManager(MemoryCredentialStore())

        with self.assertRaisesRegex(AuthorizationError, "client ID is missing"):
            manager.authorize_google(account)

    def test_google_user_sign_in_uses_account_client_credentials(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Personal Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        update_bound_credentials(
            store,
            account,
            oauth_client_secret="account-client-secret",
        )
        fake_flow = FakeUserFlow(account.client_id)
        self.google_profile.return_value = {"emailAddress": account.username}
        captured = {}

        def flow_factory(configuration, scopes, *, autogenerate_code_verifier=False):
            captured["configuration"] = configuration
            captured["scopes"] = scopes
            captured["pkce"] = autogenerate_code_verifier
            return fake_flow

        manager = OAuthManager(
            store,
            google_user_flow_factory=flow_factory,
        )

        manager.authorize_google(account)

        self.assertEqual(
            captured["configuration"]["installed"]["client_id"],
            "account-client-id",
        )
        self.assertEqual(
            captured["configuration"]["installed"]["client_secret"],
            "account-client-secret",
        )
        self.assertEqual(
            captured["configuration"]["installed"]["redirect_uris"],
            ["http://localhost"],
        )
        self.assertEqual(captured["scopes"], [GOOGLE_GMAIL_READONLY_SCOPE])
        self.assertTrue(captured["pkce"])
        self.assertEqual(
            load_credential_data(store, account.id)["google_credentials"]["token"],
            "user-token",
        )
        self.assertEqual(
            load_credential_data(store, account.id)["oauth_client_secret"],
            "account-client-secret",
        )
        self.assertEqual(
            load_credential_data(store, account.id)["google_credentials"]["account"],
            account.username,
        )
        self.google_profile.assert_called_once()
        self.assertEqual(
            fake_flow.authorization_arguments,
            {"access_type": "offline", "prompt": "consent", "login_hint": account.username},
        )
        self.assertEqual(fake_flow.token_arguments["timeout"], 15)
        self.assertEqual(
            fake_flow.token_arguments["authorization_response"],
            "https://localhost:12345/?code=synthetic&state=state",
        )

    def test_google_sign_in_rejects_other_identity_and_partial_consent(self):
        account = Account(
            "Gmail",
            username="me@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
        )
        for denied in ("identity", "scopes"):
            with self.subTest(denied=denied):
                store, flow = MemoryCredentialStore(), FakeUserFlow()
                flow.credentials.granted_scopes = (
                    ["openid"] if denied == "scopes" else [GOOGLE_GMAIL_READONLY_SCOPE]
                )
                manager = OAuthManager(
                    store, google_user_flow_factory=lambda *args, flow=flow, **kwargs: flow
                )
                with patch(
                    "mailarchive.infrastructure.providers.http.HttpClient.get_json",
                    return_value={"emailAddress": "other@example.com"},
                ):
                    with self.assertRaises(AuthorizationError):
                        manager.authorize_google(account)
                self.assertIsNone(store.get(account.id))

    def test_google_actual_flow_uses_s256_and_sends_the_matching_verifier(self):
        from google_auth_oauthlib.flow import InstalledAppFlow
        from requests import PreparedRequest, Response

        account = Account(
            "Google",
            username="me@example.com",
            client_id="client-id",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
        )
        captured = {}
        token_response = Response()
        token_response.status_code = 200
        token_response.request = PreparedRequest()
        token_response.request.prepare(method="POST", url="https://oauth2.googleapis.com/token")
        token_response._content = json.dumps(
            {
                "access_token": "synthetic",
                "refresh_token": "synthetic-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": GOOGLE_GMAIL_READONLY_SCOPE,
            }
        ).encode()

        def factory(config, scopes, **kwargs):
            self.assertTrue(kwargs.get("autogenerate_code_verifier"))
            flow = InstalledAppFlow.from_client_config(config, scopes, **kwargs)
            request = Mock(return_value=token_response)
            flow.oauth2session.request = request
            captured["flow"], captured["request"] = flow, request
            return flow

        def receive(*, auth_uri, state, timeout):
            captured["query"] = parse_qs(urlsplit(auth_uri).query)
            return {"state": state, "code": "synthetic-code"}

        store = MemoryCredentialStore()
        manager = OAuthManager(store, google_user_flow_factory=factory)
        with patch.object(FakeBrowserAuthorization, "get_auth_response", side_effect=receive):
            manager.authorize_google(account)
        query = captured["query"]
        self.assertEqual(query["code_challenge_method"], ["S256"])
        token_data = captured["request"].call_args.kwargs["data"]
        verifier = token_data["code_verifier"]
        expected = (
            urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        )
        self.assertEqual(query["code_challenge"], [expected])
        self.assertTrue(
            load_credential_data(store, account.id)["google_credentials"]["refresh_token"]
        )

    def test_google_cancel_after_profile_lookup_does_not_publish_credentials(self):
        account = Account(
            "Gmail",
            username="me@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
        )
        store, cancelled = MemoryCredentialStore(), threading.Event()
        manager = OAuthManager(
            store,
            cancelled=cancelled,
            google_user_flow_factory=lambda *args, **kwargs: FakeUserFlow(),
        )

        def cancel_during_profile(*args, **kwargs):
            cancelled.set()
            return {"emailAddress": account.username}

        self.google_profile.side_effect = cancel_during_profile
        with self.assertRaisesRegex(AuthorizationError, "Authorization cancelled"):
            manager.authorize_google(account)
        self.assertIsNone(store.get(account.id))

    def test_google_user_access_refreshes_and_persists_expired_token(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Personal Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        update_bound_credentials(
            store,
            account,
            google_credentials={"token": "old-token", "refresh_token": "refresh-token"},
            unrelated_secret="removed",
        )
        credentials = FakeRefreshableGoogleCredentials()
        request = object()
        manager = OAuthManager(store)

        with (
            patch(
                "google.oauth2.credentials.Credentials.from_authorized_user_info",
                return_value=credentials,
            ) as from_info,
            patch(
                "google.auth.transport.requests.Request",
                return_value=request,
            ),
        ):
            token = manager.google_access_token(account)

        self.assertEqual(token, "refreshed-token")
        self.assertIs(credentials.refresh_request, request)
        from_info.assert_called_once_with(
            {"token": "old-token", "refresh_token": "refresh-token"},
            scopes=[GOOGLE_GMAIL_READONLY_SCOPE],
        )
        saved = load_credential_data(store, account.id)
        self.assertEqual(saved["google_credentials"]["token"], "refreshed-token")
        self.assertNotIn("unrelated_secret", saved)

    def test_google_forced_refresh_replaces_locally_valid_token_and_persists_rotation(self):
        account = Account(
            "Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
        )
        store = MemoryCredentialStore()
        info = {"token": "old-token", "refresh_token": "refresh-token"}
        update_bound_credentials(store, account, google_credentials=info)
        credentials = FakeRefreshableGoogleCredentials(expired=False, valid=True)
        credentials.refresh_hook = lambda: setattr(credentials, "refresh_token", "rotated-refresh")
        manager = OAuthManager(store)
        with patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_info",
            return_value=credentials,
        ):
            self.assertEqual(manager.google_access_token(account), "old-token")
            self.assertIsNone(credentials.refresh_request)
            self.assertEqual(
                manager.google_access_token(account, force_refresh=True), "refreshed-token"
            )
        saved = load_credential_data(store, account.id)["google_credentials"]
        self.assertEqual(saved["token"], "refreshed-token")
        self.assertEqual(saved["refresh_token"], "rotated-refresh")

    def test_google_forced_refresh_never_returns_rejected_token_when_refresh_is_unavailable(self):
        account = Account(
            "Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
        )
        for missing in (True, False):
            with self.subTest(missing_refresh_token=missing):
                store = MemoryCredentialStore()
                info = {"token": "old-token", "refresh_token": "refresh-token"}
                update_bound_credentials(store, account, google_credentials=info)
                credentials = FakeRefreshableGoogleCredentials(
                    expired=False, valid=True, refresh_token=None if missing else "refresh-token"
                )
                credentials.refresh_hook = Mock(side_effect=RuntimeError("revoked refresh-token"))
                with patch(
                    "google.oauth2.credentials.Credentials.from_authorized_user_info",
                    return_value=credentials,
                ):
                    with self.assertRaises(AuthorizationError) as error:
                        OAuthManager(store).google_access_token(account, force_refresh=True)
                self.assertNotIn("refresh-token", str(error.exception))
                if missing:
                    self.assertNotIn("google_credentials", load_credential_data(store, account.id))
                else:
                    self.assertEqual(
                        load_credential_data(store, account.id)["google_credentials"], info
                    )

    def test_google_refresh_is_serialized_across_managers(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Personal Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        update_bound_credentials(
            store,
            account,
            google_credentials={"token": "old-token", "refresh_token": "refresh-token"},
        )
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        blocked = threading.Event()

        def first_hook() -> None:
            first_entered.set()
            self.assertTrue(release_first.wait(timeout=THREAD_TIMEOUT))

        first_credentials = FakeRefreshableGoogleCredentials(refresh_hook=first_hook)
        second_credentials = FakeRefreshableGoogleCredentials(
            refresh_hook=second_entered.set,
        )
        first_manager = OAuthManager(store)
        second_manager = OAuthManager(store)

        with (
            patch(
                "google.oauth2.credentials.Credentials.from_authorized_user_info",
                side_effect=[first_credentials, second_credentials],
            ),
            patch("google.auth.transport.requests.Request", return_value=object()),
            patch(
                "mailarchive.infrastructure.oauth.account_credential_lock",
                side_effect=lambda account_id: ObservedLock(
                    account_credential_lock(account_id), blocked
                ),
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            try:
                first = executor.submit(first_manager.google_access_token, account)
                self.assertTrue(first_entered.wait(timeout=THREAD_TIMEOUT))
                second = executor.submit(second_manager.google_access_token, account)
                self.assertTrue(blocked.wait(THREAD_TIMEOUT))
                self.assertFalse(second_entered.is_set())
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=THREAD_TIMEOUT), "refreshed-token")
            self.assertEqual(second.result(timeout=THREAD_TIMEOUT), "refreshed-token")

        self.assertTrue(second_entered.is_set())

    def test_google_user_access_requires_authorization_and_rejects_invalid_token(self) -> None:
        account = Account(
            label="Personal Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        manager = OAuthManager(MemoryCredentialStore())

        with self.assertRaisesRegex(AuthorizationError, "authorization is required"):
            manager.google_access_token(account)

        update_bound_credentials(
            manager.credential_store,
            account,
            google_credentials={"token": "expired"},
        )
        invalid_credentials = FakeRefreshableGoogleCredentials(
            expired=True,
            refresh_token=None,
            valid=False,
            token=None,
        )
        with patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_info",
            return_value=invalid_credentials,
        ):
            with self.assertRaisesRegex(AuthorizationError, "authorize it again"):
                manager.google_access_token(account)

    def test_microsoft_user_sign_in_uses_account_public_client(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
            tenant_id="organizations",
        )
        fake_msal = FakeMsalModule()
        manager = OAuthManager(
            store,
            microsoft_msal_module=fake_msal,
        )

        manager.authorize_microsoft(account)

        application = fake_msal.applications[0]
        self.assertEqual(application.client_id, "account-client-id")
        self.assertEqual(
            application.authority_url,
            "https://login.microsoftonline.com/organizations",
        )
        self.assertEqual(
            application.interactive_arguments,
            {
                "scopes": ["https://graph.microsoft.com/Mail.Read"],
                "login_hint": "me@example.com",
                "port": 12345,
                "timeout": BROWSER_AUTHORIZATION_TIMEOUT_SECONDS,
                "auth_code_receiver": application.interactive_arguments["auth_code_receiver"],
            },
        )
        self.assertIn("msal_cache", load_credential_data(store, account.id))

    def test_microsoft_user_sign_in_defaults_to_common_authority(self) -> None:
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        manager = OAuthManager(MemoryCredentialStore())

        self.assertEqual(
            manager._microsoft_client_configuration(account),
            ("account-client-id", "common"),
        )

    def test_microsoft_user_sign_in_uses_injected_public_client_and_imap_scope(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook IMAP",
            host="outlook.office365.com",
            username="me@example.com",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_USER,
        )
        fake_msal = FakeMsalModule()
        manager = OAuthManager(
            store,
            microsoft_msal_module=fake_msal,
            microsoft_public_client_id="bundled-public-client-id",
        )

        manager.authorize_microsoft(account)

        application = fake_msal.applications[0]
        self.assertEqual(application.client_id, "bundled-public-client-id")
        self.assertEqual(
            application.interactive_arguments["scopes"],
            [MICROSOFT_IMAP_ACCESS_SCOPE],
        )
        self.assertEqual(application.interactive_arguments["login_hint"], "me@example.com")

    def test_microsoft_delegated_client_id_prefers_account_override_then_bundle(self) -> None:
        injected_manager = OAuthManager(
            MemoryCredentialStore(),
            microsoft_public_client_id="bundled-public-client-id",
        )
        overridden = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        bundled = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
        )

        self.assertEqual(
            injected_manager._microsoft_client_configuration(overridden),
            ("account-client-id", "common"),
        )
        self.assertEqual(
            injected_manager._microsoft_client_configuration(bundled),
            ("bundled-public-client-id", "common"),
        )

        with patch(
            "mailarchive.infrastructure.oauth.require_microsoft_public_client_id",
            return_value="configured-public-client-id",
        ) as configured_client_id:
            self.assertEqual(
                OAuthManager(MemoryCredentialStore())._microsoft_client_configuration(bundled),
                ("configured-public-client-id", "common"),
            )
        configured_client_id.assert_called_once_with()

    def test_microsoft_user_sign_in_reports_missing_account_client_id(self) -> None:
        account = Account(
            label="Existing Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
        )
        manager = OAuthManager(MemoryCredentialStore())

        with (
            patch(
                "mailarchive.infrastructure.oauth.require_microsoft_public_client_id",
                side_effect=ProviderConfigurationError("internal configuration detail"),
            ),
            self.assertRaisesRegex(AuthorizationError, "not configured") as raised,
        ):
            manager._microsoft_client_configuration(account)
        self.assertNotIn("internal configuration detail", str(raised.exception))

    def test_microsoft_interactive_failure_surfaces_provider_detail(self) -> None:
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        manager = OAuthManager(
            MemoryCredentialStore(),
            microsoft_msal_module=FakeMsalModule(
                interactive_result={"error_description": "Consent was denied"},
            ),
        )

        with self.assertRaisesRegex(AuthorizationError, "Consent was denied"):
            manager.authorize_microsoft(account)

    def test_microsoft_interactive_authorization_replaces_stale_credentials(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        update_bound_credentials(
            store,
            account,
            password="stale-password",
            client_secret="stale-secret",
            msal_cache='{"old":"cache"}',
        )
        manager = OAuthManager(store, microsoft_msal_module=FakeMsalModule())

        manager.authorize_microsoft(account)

        self.assertEqual(
            load_credential_data(store, account.id),
            {"msal_cache": '{"cache":"value"}'},
        )
        self.assertFalse(hasattr(manager.microsoft_msal_module.caches[0], "value"))

    def test_microsoft_interactive_authorization_rejects_a_different_identity(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook",
            username="expected@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        manager = OAuthManager(
            store,
            microsoft_msal_module=FakeMsalModule(
                accounts=[{"username": "different@example.com"}],
            ),
        )

        with self.assertRaisesRegex(AuthorizationError, "does not uniquely match"):
            manager.authorize_microsoft(account)

        self.assertIsNone(store.get(account.id))

    def test_microsoft_interactive_authorization_rejects_application_mode(self) -> None:
        account = Account(
            label="Microsoft application",
            username="archive@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="application-id",
            tenant_id="tenant-id",
        )
        manager = OAuthManager(MemoryCredentialStore(), microsoft_msal_module=FakeMsalModule())

        with self.assertRaisesRegex(AuthorizationError, "only used for delegated access"):
            manager.authorize_microsoft(account)

    def test_microsoft_application_access_keeps_per_account_registration(self) -> None:
        account = Account(
            label="Microsoft application",
            username="archive@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="organization-application-id",
            tenant_id="organization-tenant-id",
        )
        manager = OAuthManager(
            MemoryCredentialStore(),
        )

        self.assertEqual(
            manager._microsoft_client_configuration(account),
            ("organization-application-id", "organization-tenant-id"),
        )

    def test_microsoft_user_access_uses_silent_token_and_updates_cache(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
            tenant_id="organizations",
        )
        update_bound_credentials(store, account, msal_cache='{"old":"cache"}')
        fake_msal = FakeMsalModule(silent_result={"access_token": "silent-token"})
        manager = OAuthManager(store, microsoft_msal_module=fake_msal)

        token = manager.microsoft_access_token(account)

        self.assertEqual(token, "silent-token")
        application = fake_msal.applications[0]
        self.assertEqual(application.account_queries, ["me@example.com"])
        self.assertFalse(application.force_refresh)
        self.assertEqual(
            application.silent_arguments,
            (["https://graph.microsoft.com/Mail.Read"], {"username": "me@example.com"}),
        )
        self.assertEqual(fake_msal.caches[0].value, '{"old":"cache"}')
        self.assertEqual(load_credential_data(store, account.id)["msal_cache"], '{"cache":"value"}')

    def test_microsoft_imap_access_can_force_refresh_and_persists_the_updated_cache(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook IMAP",
            host="outlook.office365.com",
            username="me@example.com",
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
        )
        update_bound_credentials(store, account, msal_cache='{"old":"cache"}')
        fake_msal = FakeMsalModule(silent_result={"access_token": "new-token"})
        manager = OAuthManager(store, microsoft_msal_module=fake_msal)

        self.assertEqual(manager.microsoft_access_token(account, force_refresh=True), "new-token")

        application = fake_msal.applications[0]
        self.assertTrue(application.force_refresh)
        self.assertEqual(
            application.silent_arguments,
            ([MICROSOFT_IMAP_ACCESS_SCOPE], {"username": "me@example.com"}),
        )
        self.assertIsNone(application.interactive_arguments)
        self.assertEqual(load_credential_data(store, account.id)["msal_cache"], '{"cache":"value"}')

    def test_microsoft_user_access_requires_cached_account_or_silent_result(self) -> None:
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        scenarios = (
            (FakeMsalModule(accounts=[]), "authorization is required"),
            (
                FakeMsalModule(
                    accounts=[{"username": "me@example.com"}],
                    silent_result={},
                ),
                "authorization has expired",
            ),
        )
        for fake_msal, message in scenarios:
            with self.subTest(message=message):
                manager = OAuthManager(
                    MemoryCredentialStore(),
                    microsoft_msal_module=fake_msal,
                )
                with self.assertRaisesRegex(AuthorizationError, message):
                    manager.microsoft_access_token(account)

    def test_microsoft_user_access_never_falls_back_to_a_different_cached_username(self) -> None:
        account = Account(
            label="Outlook",
            username="expected@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        fake_msal = FakeMsalModule(accounts=[{"username": "different@example.com"}])
        manager = OAuthManager(
            MemoryCredentialStore(),
            microsoft_msal_module=fake_msal,
        )

        with self.assertRaisesRegex(AuthorizationError, "authorization is required"):
            manager.microsoft_access_token(account)

        self.assertEqual(fake_msal.applications[0].account_queries, ["expected@example.com"])

    def test_microsoft_user_access_rejects_ambiguous_cached_identities(self) -> None:
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        fake_msal = FakeMsalModule(
            accounts=[
                {"username": "me@example.com", "home_account_id": "personal"},
                {"username": "me@example.com", "home_account_id": "work"},
            ]
        )
        manager = OAuthManager(
            MemoryCredentialStore(),
            microsoft_msal_module=fake_msal,
        )

        with self.assertRaisesRegex(AuthorizationError, "More than one cached"):
            manager.microsoft_access_token(account)

        self.assertIsNone(fake_msal.applications[0].silent_arguments)

    def test_microsoft_user_access_uses_imap_delegated_scope(self) -> None:
        account = Account(
            label="Outlook IMAP",
            host="outlook.office365.com",
            username="me@example.com",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        fake_msal = FakeMsalModule()
        manager = OAuthManager(
            MemoryCredentialStore(),
            microsoft_msal_module=fake_msal,
        )

        self.assertEqual(manager.microsoft_access_token(account), "silent-token")
        self.assertEqual(
            fake_msal.applications[0].silent_arguments,
            ([MICROSOFT_IMAP_ACCESS_SCOPE], {"username": "me@example.com"}),
        )

    def test_cancelled_browser_closes_listener_and_releases_account(self) -> None:
        for provider in (
            MailProvider.GMAIL_API,
            MailProvider.MICROSOFT_GRAPH,
            MailProvider.GENERIC_IMAP,
        ):
            with self.subTest(provider=provider):
                account = Account(
                    label="Mail",
                    username="me@example.com",
                    provider=provider,
                    auth_mode=AuthMode.OAUTH_USER,
                    client_id="client-id",
                )
                entered, cancelled = threading.Event(), threading.Event()
                receivers = []
                store = MemoryCredentialStore()

                def receiver_factory(event, receivers=receivers):
                    receiver = BrowserAuthorization(event)
                    receivers.append(receiver)
                    return receiver

                fake_msal = FakeMsalModule()

                def wait_for_browser(fake_msal=fake_msal):
                    receiver = fake_msal.applications[-1].interactive_arguments[
                        "auth_code_receiver"
                    ]
                    receiver.get_auth_response(
                        auth_uri="https://example.org/authorize",
                        state="state",
                        timeout=BROWSER_AUTHORIZATION_TIMEOUT_SECONDS,
                    )

                fake_msal.interactive_hook = wait_for_browser
                manager = OAuthManager(
                    store,
                    cancelled=cancelled,
                    google_user_flow_factory=lambda *args, **kwargs: FakeUserFlow(),
                    microsoft_msal_module=fake_msal,
                )
                method = (
                    manager.authorize_google
                    if provider == MailProvider.GMAIL_API
                    else manager.authorize_microsoft
                )
                browser = Mock()
                browser.open.side_effect = lambda *args, entered=entered, **kwargs: (
                    entered.set() or True
                )
                with (
                    patch(
                        "mailarchive.infrastructure.oauth.BrowserAuthorization", receiver_factory
                    ),
                    patch(
                        "mailarchive.infrastructure.browser_authorization.webbrowser.get",
                        return_value=browser,
                    ),
                    ThreadPoolExecutor(max_workers=1) as executor,
                ):
                    pending = executor.submit(method, account)
                    try:
                        self.assertTrue(entered.wait(THREAD_TIMEOUT))
                        port = receivers[0].get_port()
                    finally:
                        cancelled.set()
                    with self.assertRaisesRegex(AuthorizationError, "cancelled"):
                        pending.result(timeout=THREAD_TIMEOUT)
                    with self.assertRaises(OSError):
                        socket.create_connection(("127.0.0.1", port), timeout=1)
                self.assertIsNone(store.get(account.id))
                retry = OAuthManager(
                    store,
                    google_user_flow_factory=lambda *args, **kwargs: FakeUserFlow(),
                    microsoft_msal_module=FakeMsalModule(),
                )
                retry_method = (
                    retry.authorize_google
                    if provider == MailProvider.GMAIL_API
                    else retry.authorize_microsoft
                )
                retry_method(account)
                self.assertIsNotNone(store.get(account.id))

    def test_microsoft_authorization_and_refresh_are_serialized_across_managers(self) -> None:
        account = Account(
            label="Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        blocked = threading.Event()

        def interactive_hook() -> None:
            first_entered.set()
            self.assertTrue(release_first.wait(timeout=THREAD_TIMEOUT))

        def silent_hook() -> None:
            second_entered.set()

        store = MemoryCredentialStore()
        fake_msal = FakeMsalModule(
            silent_hook=silent_hook,
            interactive_hook=interactive_hook,
        )
        first_manager = OAuthManager(store, microsoft_msal_module=fake_msal)
        second_manager = OAuthManager(store, microsoft_msal_module=fake_msal)

        with (
            patch(
                "mailarchive.infrastructure.oauth.account_credential_lock",
                side_effect=lambda account_id: ObservedLock(
                    account_credential_lock(account_id), blocked
                ),
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            try:
                first = executor.submit(first_manager.authorize_microsoft, account)
                self.assertTrue(first_entered.wait(timeout=THREAD_TIMEOUT))
                second = executor.submit(second_manager.microsoft_access_token, account)
                self.assertTrue(blocked.wait(THREAD_TIMEOUT))
                self.assertFalse(second_entered.is_set())
            finally:
                release_first.set()
            self.assertIsNone(first.result(timeout=THREAD_TIMEOUT))
            self.assertEqual(second.result(timeout=THREAD_TIMEOUT), "silent-token")

        self.assertTrue(second_entered.is_set())

    def test_generic_imap_oauth_is_interactively_authorized(self) -> None:
        account = Account(
            label="Outlook IMAP",
            host="outlook.office365.com",
            username="me@example.com",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )

        with patch("mailarchive.infrastructure.oauth.OAuthManager") as manager_type:
            authorize_account(account, MemoryCredentialStore())

        manager_type.return_value.authorize_microsoft.assert_called_once_with(account)

    def test_microsoft_application_access_uses_secret_and_default_scope(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Microsoft application",
            username="archive@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="application-id",
            tenant_id="tenant-id",
        )
        update_bound_credentials(store, account, client_secret="secret")
        fake_msal = FakeMsalModule(application_result={"access_token": "app-token"})
        manager = OAuthManager(store, microsoft_msal_module=fake_msal)

        token = manager.microsoft_access_token(account)

        self.assertEqual(token, "app-token")
        application = fake_msal.applications[0]
        self.assertEqual(application.client_credential, "secret")
        self.assertEqual(application.scopes, ["https://graph.microsoft.com/.default"])
        self.assertEqual(application.calls, ["acquire_token_for_client"])
        self.assertEqual(
            application.authority,
            "https://login.microsoftonline.com/tenant-id",
        )

    def test_microsoft_application_force_refresh_invalidates_cache_before_acquiring(self):
        store = MemoryCredentialStore()
        account = Account(
            "Graph application",
            username="archive@example.org",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="application",
            tenant_id="tenant",
        )
        update_bound_credentials(
            store, account, client_secret="secret", msal_cache='{"old":"cache"}'
        )
        msal = FakeMsalModule(application_result={"access_token": "new-application-token"})
        manager = OAuthManager(store, microsoft_msal_module=msal)
        self.assertEqual(
            manager.microsoft_access_token(account, force_refresh=True), "new-application-token"
        )
        application = msal.applications[0]
        self.assertEqual(
            application.calls, ["remove_tokens_for_client", "acquire_token_for_client"]
        )
        self.assertEqual(application.scopes, ["https://graph.microsoft.com/.default"])
        saved = load_credential_data(store, account.id)
        self.assertEqual(saved["msal_cache"], '{"cache":"value"}')
        self.assertEqual(saved["client_secret"], "secret")

    def test_microsoft_application_access_requires_secret_and_surfaces_failure(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Microsoft application",
            username="archive@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="application-id",
            tenant_id="tenant-id",
        )
        manager = OAuthManager(store, microsoft_msal_module=FakeMsalModule())
        with self.assertRaisesRegex(AuthorizationError, "client secret is missing"):
            manager.microsoft_access_token(account)

        update_bound_credentials(store, account, client_secret="secret")
        manager = OAuthManager(
            store,
            microsoft_msal_module=FakeMsalModule(
                application_result={"error_description": "Invalid client secret"},
            ),
        )
        with self.assertRaisesRegex(AuthorizationError, "Invalid client secret"):
            manager.microsoft_access_token(account)

    def test_google_workspace_application_access_impersonates_mailbox(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Workspace",
            username="archive@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
        )
        service_account = {
            "type": "service_account",
            "client_email": "mailarchive@project.iam.gserviceaccount.com",
            "private_key": "private-key",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        update_bound_credentials(
            store,
            account,
            google_service_account=service_account,
        )
        fake_credentials = FakeServiceAccountCredentials()
        captured = {}

        def credential_factory(info, scopes):
            captured["info"] = info
            captured["scopes"] = scopes
            return fake_credentials

        request = object()
        manager = OAuthManager(
            store,
            google_service_account_factory=credential_factory,
            google_request_factory=lambda: request,
        )

        token = manager.google_access_token(account)

        self.assertEqual(token, "service-account-token")
        self.assertEqual(fake_credentials.subject, "archive@example.com")
        self.assertIs(fake_credentials.request, request)
        self.assertEqual(captured["info"], service_account)
        self.assertEqual(captured["scopes"], [GOOGLE_GMAIL_READONLY_SCOPE])

    def test_google_workspace_force_refresh_mints_new_token_for_same_addressed_mailbox(self):
        account = Account(
            "Workspace",
            username="owner@example.org",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
        )
        store = MemoryCredentialStore()
        info = {
            "type": "service_account",
            "client_email": "service@example.org",
            "private_key": "key",
        }
        update_bound_credentials(store, account, google_service_account=info)
        credentials = [FakeServiceAccountCredentials(), FakeServiceAccountCredentials()]
        factory = Mock(side_effect=credentials)
        manager = OAuthManager(
            store, google_service_account_factory=factory, google_request_factory=object
        )
        for forced in (False, True):
            self.assertEqual(
                manager.google_access_token(
                    account, mailbox_address="archive@example.org", force_refresh=forced
                ),
                "service-account-token",
            )
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(
            all(
                credential.subject == "archive@example.org" and credential.request is not None
                for credential in credentials
            )
        )
        self.assertEqual(load_credential_data(store, account.id)["google_service_account"], info)

    def test_google_workspace_application_access_requires_key(self) -> None:
        account = Account(
            label="Workspace",
            username="archive@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
        )
        manager = OAuthManager(
            MemoryCredentialStore(),
            google_service_account_factory=lambda info, scopes: None,
            google_request_factory=lambda: object(),
        )

        with self.assertRaisesRegex(AuthorizationError, "JSON key"):
            manager.google_access_token(account)

    def test_google_workspace_application_access_wraps_refresh_failure(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Workspace",
            username="archive@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
        )
        update_bound_credentials(
            store,
            account,
            google_service_account={"type": "service_account"},
        )

        class FailingCredentials(FakeServiceAccountCredentials):
            def refresh(self, request):
                raise RuntimeError("signature rejected")

        manager = OAuthManager(
            store,
            google_service_account_factory=lambda info, scopes: FailingCredentials(),
            google_request_factory=lambda: object(),
        )

        with self.assertRaisesRegex(
            AuthorizationError,
            "application authorization failed: signature rejected",
        ):
            manager.google_access_token(account)

    def test_google_workspace_application_access_rejects_empty_token(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Workspace",
            username="archive@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
        )
        update_bound_credentials(
            store,
            account,
            google_service_account={"type": "service_account"},
        )

        class EmptyTokenCredentials(FakeServiceAccountCredentials):
            def refresh(self, request):
                self.request = request

        manager = OAuthManager(
            store,
            google_service_account_factory=lambda info, scopes: EmptyTokenCredentials(),
            google_request_factory=lambda: object(),
        )

        with self.assertRaisesRegex(AuthorizationError, "did not return"):
            manager.google_access_token(account)

    def test_service_account_file_is_validated_and_reduced(self) -> None:
        value = {
            "type": "service_account",
            "project_id": "example-project",
            "private_key_id": "key-id",
            "private_key": "private-key",
            "client_email": "mailarchive@project.iam.gserviceaccount.com",
            "client_id": "123456",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service-account.json"
            path.write_text(json.dumps(value), encoding="utf-8")

            parsed = parse_google_service_account_file(str(path))

        self.assertEqual(parsed["client_email"], value["client_email"])
        self.assertEqual(parsed["private_key"], value["private_key"])
        self.assertNotIn("project_id", parsed)
        self.assertNotIn("client_id", parsed)

    def test_service_account_file_rejects_oauth_client_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "oauth-client.json"
            path.write_text(json.dumps({"installed": {"client_id": "id"}}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "service-account"):
                parse_google_service_account_file(str(path))

    def test_service_account_file_reports_invalid_json_and_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service-account.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                parse_google_service_account_file(str(path))

            path.write_text(
                json.dumps({"type": "service_account", "client_email": "account@example.com"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "private_key, token_uri"):
                parse_google_service_account_file(str(path))


if __name__ == "__main__":
    unittest.main()
