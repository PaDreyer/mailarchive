import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailarchive.config import ConfigStore, default_data_dir
from mailarchive.models import Account, AuthMode, MailProvider, Settings


class ConfigStoreTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "XDG data directories are POSIX-specific")
    def test_default_data_directory_honors_xdg_environment(self) -> None:
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": "/custom/data"}):
            self.assertEqual(default_data_dir(), Path("/custom/data/mailarchive"))

    def test_missing_config_loads_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = ConfigStore(Path(temporary)).load()

            self.assertTrue(settings.archive_root.endswith("MailArchive"))
            self.assertEqual(len(settings.rules), 1)

    def test_default_state_database_uses_application_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))

            self.assertEqual(
                store.state_database_path(Settings.defaults()),
                Path(temporary) / "archive-state.sqlite3",
            )

    def test_custom_state_database_path_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary) / "config")
            settings = Settings.defaults()
            settings.state_database_path = str(Path(temporary) / "state" / "mail.db")

            store.save(settings)
            loaded = store.load()

            self.assertEqual(loaded.state_database_path, settings.state_database_path)
            self.assertEqual(store.state_database_path(loaded), Path(settings.state_database_path))

    def test_round_trip_never_serializes_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            settings = Settings.defaults()
            settings.accounts.append(Account("Personal", "imap.example.org", "user@example.org"))
            store.save(settings)

            text = store.path.read_text(encoding="utf-8")
            self.assertNotIn('"password":', text.casefold())
            loaded = store.load()
            self.assertEqual(loaded.accounts[0].host, "imap.example.org")
            self.assertEqual(loaded.rules[0].name, "All remaining emails")

    def test_provider_and_default_polling_interval_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            settings = Settings.defaults()
            settings.default_poll_minutes = 12
            settings.archive_existing_messages = True
            settings.accounts.append(
                Account(
                    label="Work",
                    username="me@example.com",
                    provider=MailProvider.MICROSOFT_GRAPH,
                    auth_mode=AuthMode.OAUTH_APPLICATION,
                    client_id="client-id",
                    tenant_id="tenant-id",
                    folder="inbox",
                )
            )

            store.save(settings)
            loaded = store.load()

            self.assertEqual(loaded.default_poll_minutes, 12)
            self.assertTrue(loaded.archive_existing_messages)
            self.assertEqual(loaded.accounts[0].provider, MailProvider.MICROSOFT_GRAPH)
            self.assertIsNone(loaded.accounts[0].poll_minutes)

    def test_legacy_imap_account_is_migrated(self) -> None:
        account = Account.from_dict(
            {
                "label": "Legacy",
                "host": "imap.example.org",
                "username": "me@example.org",
                "mailbox": "Archive",
                "poll_minutes": 8,
            }
        )

        self.assertEqual(account.provider, MailProvider.GENERIC_IMAP)
        self.assertEqual(account.folder, "Archive")
        self.assertEqual(account.poll_minutes, 8)

    def test_legacy_delegated_auth_name_is_migrated(self) -> None:
        account = Account.from_dict(
            {
                "label": "Gmail",
                "provider": "gmail_api",
                "auth_mode": "oauth_delegated",
                "username": "me@example.com",
                "client_id": "desktop-client-id",
            }
        )

        self.assertEqual(account.auth_mode, AuthMode.OAUTH_USER)

    def test_gmail_application_access_does_not_require_oauth_client_id(self) -> None:
        account = Account(
            label="Workspace archive",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            username="archive@example.com",
        )

        account.validate()

    def test_gmail_user_sign_in_requires_account_client_id(self) -> None:
        account = Account(
            label="Personal Gmail",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@gmail.com",
        )

        with self.assertRaisesRegex(ValueError, "Google OAuth desktop client ID"):
            account.validate()

    def test_microsoft_user_sign_in_requires_account_client_id(
        self,
    ) -> None:
        account = Account(
            label="Outlook",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@example.com",
        )

        with self.assertRaisesRegex(ValueError, "Microsoft Entra application client ID"):
            account.validate()

    def test_microsoft_user_sign_in_allows_blank_tenant(self) -> None:
        account = Account(
            label="Outlook",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@example.com",
            client_id="desktop-client-id",
        )

        account.validate()

    def test_user_oauth_account_configuration_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            settings = Settings.defaults()
            settings.accounts.extend(
                [
                    Account(
                        label="Gmail",
                        provider=MailProvider.GMAIL_API,
                        auth_mode=AuthMode.OAUTH_USER,
                        username="me@gmail.com",
                        client_id="google-desktop-client-id",
                    ),
                    Account(
                        label="Outlook",
                        provider=MailProvider.MICROSOFT_GRAPH,
                        auth_mode=AuthMode.OAUTH_USER,
                        username="me@example.com",
                        client_id="microsoft-public-client-id",
                        tenant_id="organizations",
                    ),
                ]
            )

            store.save(settings)
            loaded = store.load()

            self.assertEqual(loaded.accounts[0].client_id, "google-desktop-client-id")
            self.assertEqual(loaded.accounts[1].client_id, "microsoft-public-client-id")
            self.assertEqual(loaded.accounts[1].tenant_id, "organizations")

    def test_old_user_oauth_account_without_client_id_remains_editable(self) -> None:
        account = Account.from_dict(
            {
                "label": "Existing Gmail",
                "provider": "gmail_api",
                "auth_mode": "oauth_user",
                "username": "me@gmail.com",
            }
        )

        self.assertEqual(account.client_id, "")
        with self.assertRaisesRegex(ValueError, "Google OAuth desktop client ID"):
            account.validate()

    def test_microsoft_user_sign_in_rejects_invalid_tenant(self) -> None:
        account = Account(
            label="Outlook",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@example.com",
            client_id="desktop-client-id",
            tenant_id="bad/tenant",
        )

        with self.assertRaisesRegex(ValueError, "valid Microsoft tenant"):
            account.validate()

    def test_broken_config_has_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            store.data_dir.mkdir(exist_ok=True)
            store.path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Could not read settings"):
                store.load()

    def test_older_settings_disable_automatic_initial_archive(self) -> None:
        settings = Settings.from_dict(
            {
                "schema_version": 2,
                "archive_root": "/tmp/archive",
            }
        )

        self.assertEqual(settings.schema_version, 4)
        self.assertEqual(settings.state_database_path, "")
        self.assertFalse(settings.archive_existing_messages)


if __name__ == "__main__":
    unittest.main()
