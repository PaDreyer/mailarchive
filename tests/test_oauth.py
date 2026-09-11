import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.credential_data import load_credential_data, update_credential_data
from mailarchive.credentials import MemoryCredentialStore
from mailarchive.models import Account, AuthMode, MailProvider
from mailarchive.oauth import (
    GOOGLE_GMAIL_READONLY_SCOPE,
    AuthorizationError,
    OAuthManager,
    parse_google_service_account_file,
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
    def to_json(self):
        return json.dumps(
            {
                "token": "user-token",
                "refresh_token": "refresh-token",
                "client_id": "application-client-id",
                "client_secret": "application-client-secret",
            }
        )


class FakeUserFlow:
    def __init__(self) -> None:
        self.run_arguments = {}

    def run_local_server(self, **arguments):
        self.run_arguments = arguments
        return FakeUserCredentials()


class FakeRefreshableGoogleCredentials:
    def __init__(
        self,
        *,
        expired=True,
        refresh_token="refresh-token",
        valid=False,
        token="old-token",
    ) -> None:
        self.expired = expired
        self.refresh_token = refresh_token
        self.valid = valid
        self.token = token
        self.refresh_request = None

    def refresh(self, request):
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
            }
        )


class FakeMsalCache:
    has_state_changed = True

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
    ):
        self.client_id = client_id
        self.authority = authority
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
        self.account_queries = []

    def acquire_token_interactive(self, **arguments):
        self.interactive_arguments = arguments
        return self.interactive_result

    def get_accounts(self, username=None):
        self.account_queries.append(username)
        if username is not None:
            return [account for account in self.accounts if account.get("username") == username]
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        self.silent_arguments = (scopes, account)
        return self.silent_result


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

    def acquire_token_for_client(self, *, scopes):
        self.scopes = scopes
        return self.result


class FakeMsalModule:
    def __init__(
        self,
        *,
        accounts=None,
        silent_result=None,
        interactive_result=None,
        application_result=None,
    ) -> None:
        self.applications = []
        self.caches = []
        self.accounts = accounts
        self.silent_result = silent_result
        self.interactive_result = interactive_result
        self.application_result = (
            {"access_token": "application-token"}
            if application_result is None
            else application_result
        )

    def SerializableTokenCache(self):
        cache = FakeMsalCache()
        self.caches.append(cache)
        return cache

    def PublicClientApplication(self, client_id, authority, token_cache):
        application = FakePublicClientApplication(
            client_id,
            authority,
            token_cache,
            accounts=self.accounts,
            silent_result=self.silent_result,
            interactive_result=self.interactive_result,
        )
        self.applications.append(application)
        return application

    def ConfidentialClientApplication(
        self,
        client_id,
        authority,
        client_credential,
        token_cache,
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
        update_credential_data(
            store,
            account.id,
            oauth_client_secret="account-client-secret",
        )
        fake_flow = FakeUserFlow()
        captured = {}

        def flow_factory(configuration, scopes):
            captured["configuration"] = configuration
            captured["scopes"] = scopes
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
        self.assertEqual(
            load_credential_data(store, account.id)["google_credentials"]["token"],
            "user-token",
        )
        self.assertEqual(
            load_credential_data(store, account.id)["oauth_client_secret"],
            "account-client-secret",
        )
        self.assertEqual(fake_flow.run_arguments["access_type"], "offline")

    def test_google_user_access_refreshes_and_persists_expired_token(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Personal Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="account-client-id",
        )
        update_credential_data(
            store,
            account.id,
            google_credentials={"token": "old-token", "refresh_token": "refresh-token"},
            unrelated_secret="preserved",
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
        self.assertEqual(saved["unrelated_secret"], "preserved")

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

        update_credential_data(
            manager.credential_store,
            account.id,
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
            application.authority,
            "https://login.microsoftonline.com/organizations",
        )
        self.assertEqual(
            application.interactive_arguments,
            {
                "scopes": ["https://graph.microsoft.com/Mail.Read"],
                "login_hint": "me@example.com",
                "port": 0,
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

    def test_microsoft_user_sign_in_reports_missing_account_client_id(self) -> None:
        account = Account(
            label="Existing Outlook",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
        )
        manager = OAuthManager(MemoryCredentialStore())

        with self.assertRaisesRegex(AuthorizationError, "client ID is missing"):
            manager._microsoft_client_configuration(account)

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
        update_credential_data(store, account.id, msal_cache='{"old":"cache"}')
        fake_msal = FakeMsalModule(silent_result={"access_token": "silent-token"})
        manager = OAuthManager(store, microsoft_msal_module=fake_msal)

        token = manager.microsoft_access_token(account)

        self.assertEqual(token, "silent-token")
        application = fake_msal.applications[0]
        self.assertEqual(application.account_queries, ["me@example.com"])
        self.assertEqual(
            application.silent_arguments,
            (["https://graph.microsoft.com/Mail.Read"], {"username": "me@example.com"}),
        )
        self.assertEqual(fake_msal.caches[0].value, '{"old":"cache"}')
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
        update_credential_data(store, account.id, client_secret="secret")
        fake_msal = FakeMsalModule(application_result={"access_token": "app-token"})
        manager = OAuthManager(store, microsoft_msal_module=fake_msal)

        token = manager.microsoft_access_token(account)

        self.assertEqual(token, "app-token")
        application = fake_msal.applications[0]
        self.assertEqual(application.client_credential, "secret")
        self.assertEqual(application.scopes, ["https://graph.microsoft.com/.default"])
        self.assertEqual(
            application.authority,
            "https://login.microsoftonline.com/tenant-id",
        )

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

        update_credential_data(store, account.id, client_secret="secret")
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
        update_credential_data(
            store,
            account.id,
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
        update_credential_data(
            store,
            account.id,
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
        update_credential_data(
            store,
            account.id,
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
