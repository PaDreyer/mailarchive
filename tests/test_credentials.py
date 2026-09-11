import unittest

from mailarchive.app import DesktopApp
from mailarchive.credential_data import load_credential_data, update_credential_data
from mailarchive.credentials import MemoryCredentialStore
from mailarchive.credentials import (
    CredentialError,
    KeyringCredentialStore,
    UnavailableCredentialStore,
    WindowsCredentialStore,
)
from mailarchive.models import Account, AuthMode, MailProvider


class FakeBackend:
    priority = 5


class FakeKeyring:
    def __init__(self) -> None:
        self.values = {}

    def get_keyring(self):
        return FakeBackend()

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def delete_password(self, service, account):
        del self.values[(service, account)]


class CredentialTests(unittest.TestCase):
    def test_google_user_store_keeps_only_relevant_credentials(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            id="gmail-account",
            label="Gmail",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            username="person@example.com",
            client_id="google-desktop-client-id",
        )
        app = object.__new__(DesktopApp)
        app.credential_store = store

        app._store_account_credentials(
            account,
            {
                "client_secret": "stale-hidden-secret",
                "password": "stale-hidden-password",
                "oauth_client_secret": "kept-client-secret",
                "google_credentials": {"refresh_token": "kept-token"},
            },
        )

        self.assertEqual(
            load_credential_data(store, account.id),
            {
                "format_version": 1,
                "oauth_client_secret": "kept-client-secret",
                "google_credentials": {"refresh_token": "kept-token"},
            },
        )

    def test_keyring_round_trip(self) -> None:
        backend = FakeKeyring()
        store = KeyringCredentialStore(keyring_module=backend)
        store.set("account-1", "secret")
        self.assertEqual(store.get("account-1"), "secret")
        store.delete("account-1")
        self.assertIsNone(store.get("account-1"))

    def test_unavailable_store_never_falls_back_to_plain_text(self) -> None:
        store = UnavailableCredentialStore("No secure keyring")
        with self.assertRaisesRegex(CredentialError, "No secure keyring"):
            store.set("account-1", "secret")

    def test_legacy_password_is_upgraded_to_structured_credential_data(self) -> None:
        store = MemoryCredentialStore()
        store.set("account-1", "old-password")

        update_credential_data(store, "account-1", client_secret="oauth-secret")
        data = load_credential_data(store, "account-1")

        self.assertEqual(data["password"], "old-password")
        self.assertEqual(data["client_secret"], "oauth-secret")

    def test_windows_credential_encoding_supports_larger_utf8_secrets(self) -> None:
        value = "private-key-" + ("x" * 2000)

        encoded = WindowsCredentialStore._encode_value(value)

        self.assertEqual(WindowsCredentialStore._decode_value(encoded), value)
        self.assertLessEqual(
            len(encoded),
            WindowsCredentialStore.MAX_CREDENTIAL_BLOB_SIZE,
        )

    def test_windows_credential_encoding_reads_legacy_utf16(self) -> None:
        encoded = "legacy-secret".encode("utf-16-le")

        self.assertEqual(
            WindowsCredentialStore._decode_value(encoded),
            "legacy-secret",
        )

    def test_windows_credential_encoding_rejects_oversized_values(self) -> None:
        with self.assertRaisesRegex(CredentialError, "too large"):
            WindowsCredentialStore._encode_value("x" * 3000)


if __name__ == "__main__":
    unittest.main()
