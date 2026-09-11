import json
import tempfile
import unittest
from pathlib import Path

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


class FakeMsalCache:
    has_state_changed = True

    def deserialize(self, value):
        self.value = value

    def serialize(self):
        return '{"cache":"value"}'


class FakePublicClientApplication:
    def __init__(self, client_id, authority, token_cache):
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache

    def acquire_token_interactive(self, **arguments):
        self.interactive_arguments = arguments
        return {"access_token": "delegated-token"}


class FakeMsalModule:
    def __init__(self) -> None:
        self.applications = []

    def SerializableTokenCache(self):
        return FakeMsalCache()

    def PublicClientApplication(self, client_id, authority, token_cache):
        application = FakePublicClientApplication(client_id, authority, token_cache)
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


if __name__ == "__main__":
    unittest.main()
