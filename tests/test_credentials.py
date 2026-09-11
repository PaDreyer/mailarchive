import unittest
from unittest.mock import Mock, patch

import mailarchive.credentials as credentials_module
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

    def test_keyring_accepts_callable_positive_backend_priority(self) -> None:
        backend = FakeBackend()
        backend.priority = Mock(return_value=5)
        keyring = FakeKeyring()
        keyring.get_keyring = Mock(return_value=backend)

        store = KeyringCredentialStore(keyring_module=keyring)

        self.assertEqual(store.service_name, "MailArchive")
        backend.priority.assert_called_once_with()

    def test_keyring_rejects_an_insecure_backend(self) -> None:
        backend = FakeBackend()
        backend.priority = 0
        keyring = FakeKeyring()
        keyring.get_keyring = Mock(return_value=backend)

        with self.assertRaisesRegex(CredentialError, "No secure system keyring"):
            KeyringCredentialStore(keyring_module=keyring)

    def test_keyring_wraps_backend_initialization_failure(self) -> None:
        keyring = FakeKeyring()
        keyring.get_keyring = Mock(side_effect=RuntimeError("locked"))

        with self.assertRaisesRegex(CredentialError, "system keyring is unavailable: locked"):
            KeyringCredentialStore(keyring_module=keyring)

    def test_keyring_wraps_read_write_and_delete_failures(self) -> None:
        read_backend = FakeKeyring()
        read_backend.get_password = Mock(side_effect=RuntimeError("read failed"))
        read_store = KeyringCredentialStore(keyring_module=read_backend)
        with self.assertRaisesRegex(CredentialError, "Could not read the password: read failed"):
            read_store.get("account-1")

        write_backend = FakeKeyring()
        write_backend.set_password = Mock(side_effect=RuntimeError("write failed"))
        write_store = KeyringCredentialStore(keyring_module=write_backend)
        with self.assertRaisesRegex(CredentialError, "Could not store the password: write failed"):
            write_store.set("account-1", "secret")

        delete_backend = FakeKeyring()
        delete_backend.values[("MailArchive", "account-1")] = "secret"
        delete_backend.delete_password = Mock(side_effect=RuntimeError("delete failed"))
        delete_store = KeyringCredentialStore(keyring_module=delete_backend)
        with self.assertRaisesRegex(CredentialError, "Could not delete the password: delete failed"):
            delete_store.delete("account-1")

    def test_keyring_does_not_delete_a_missing_value(self) -> None:
        backend = FakeKeyring()
        backend.delete_password = Mock()
        store = KeyringCredentialStore(keyring_module=backend)

        store.delete("missing")

        backend.delete_password.assert_not_called()

    def test_unavailable_store_never_falls_back_to_plain_text(self) -> None:
        store = UnavailableCredentialStore("No secure keyring")
        operations = [
            ("get", ("account-1",)),
            ("set", ("account-1", "secret")),
            ("delete", ("account-1",)),
        ]
        for method, arguments in operations:
            with self.subTest(method=method):
                with self.assertRaisesRegex(CredentialError, "No secure keyring"):
                    getattr(store, method)(*arguments)

    def test_legacy_password_is_upgraded_to_structured_credential_data(self) -> None:
        store = MemoryCredentialStore()
        store.set("account-1", "old-password")

        update_credential_data(store, "account-1", client_secret="oauth-secret")
        data = load_credential_data(store, "account-1")

        self.assertEqual(data["password"], "old-password")
        self.assertEqual(data["client_secret"], "oauth-secret")

    def test_unknown_credential_format_is_treated_as_legacy_password(self) -> None:
        store = MemoryCredentialStore()
        raw = '{"format_version":2,"password":"not-trusted"}'
        store.set("account-1", raw)

        self.assertEqual(load_credential_data(store, "account-1"), {"password": raw})

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

    def test_windows_store_rejects_non_windows_initialization(self) -> None:
        with patch.object(credentials_module.os, "name", "posix"):
            with self.assertRaisesRegex(CredentialError, "only available on Windows"):
                WindowsCredentialStore()

    def test_windows_delete_ignores_missing_value_and_wraps_other_errors(self) -> None:
        store = object.__new__(WindowsCredentialStore)
        store.prefix = "MailArchive"
        store._advapi = Mock()
        store._advapi.CredDeleteW.return_value = False

        with patch.object(
            credentials_module.ctypes,
            "get_last_error",
            return_value=WindowsCredentialStore.ERROR_NOT_FOUND,
            create=True,
        ):
            store.delete("missing")

        with patch.object(
            credentials_module.ctypes,
            "get_last_error",
            return_value=5,
            create=True,
        ):
            with self.assertRaisesRegex(CredentialError, "Windows error 5"):
                store.delete("denied")


if __name__ == "__main__":
    unittest.main()
