import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailarchive.config import ConfigStore, default_data_dir
from mailarchive.models import (
    Account,
    AuthMode,
    DateFolderPosition,
    Mailbox,
    MailProvider,
    Rule,
    Settings,
)


class ConfigStoreTests(unittest.TestCase):
    def test_version_five_rules_migrate_to_all_accounts_and_save_as_schema_eight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            store.path.write_text(
                '{"schema_version": 5, "archive_root": "/archive", '
                '"rules": [{"id": "old-rule", "name": "Existing", "destination": "Inbox"}]}',
                encoding="utf-8",
            )
            settings = store.load()
            self.assertEqual(settings.schema_version, 8)
            self.assertEqual(settings.rules[0].id, "old-rule")
            self.assertIsNone(settings.rules[0].account_ids)
            store.save(settings)
            self.assertEqual(store.load().schema_version, 8)
            self.assertIsNone(store.load().rules[0].account_ids)

    def test_schema_six_upgrade_preserves_rule_destination_scope_and_save_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            store.path.write_text(
                '{"schema_version": 6, "archive_root": "/archive", "rules": ['
                '{"id": "existing", "name": "Invoices", "destination": "Finance/Supplier", '
                '"account_ids": ["work"], "save_mode": "attachments_only"}]}',
                encoding="utf-8",
            )
            settings = store.load()
            rule = settings.rules[0]
            self.assertEqual(rule.destination, "Finance/Supplier")
            self.assertEqual(rule.account_ids, ["work"])
            self.assertEqual(rule.save_mode.value, "attachments_only")
            self.assertEqual(rule.date_folder_position, DateFolderPosition.NONE)
            store.save(settings)
            self.assertEqual(store.load().rules[0], rule)

    def test_date_folder_settings_round_trip_and_older_builds_refuse_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            settings = Settings(
                "/archive",
                rules=[
                    Rule(position.value, "", date_folder_position=position)
                    for position in DateFolderPosition
                ],
            )
            store.save(settings)
            self.assertEqual(store.load().rules, settings.rules)
            original = store.path.read_bytes()
            with mock.patch("mailarchive.models.SETTINGS_SCHEMA_VERSION", 6):
                with self.assertRaisesRegex(RuntimeError, "newer version"):
                    store.load()
            self.assertEqual(store.path.read_bytes(), original)

    def test_rule_account_scope_round_trips_through_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            settings = Settings(
                "/archive", rules=[Rule("Scoped", "Work", account_ids=["work", "personal"])]
            )
            store.save(settings)
            self.assertEqual(store.load().rules[0].account_ids, ["work", "personal"])
            with mock.patch("mailarchive.models.SETTINGS_SCHEMA_VERSION", 5):
                with self.assertRaisesRegex(RuntimeError, "newer version"):
                    store.load()

    def test_future_settings_schema_is_rejected_without_overwriting_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ConfigStore(Path(temporary))
            original = '{"schema_version": 999, "archive_root": "/archive"}'
            store.path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "newer version"):
                store.load()
            self.assertEqual(store.path.read_text(encoding="utf-8"), original)

    @unittest.skipUnless(os.name == "posix", "XDG data directories are POSIX-specific")
    def test_default_data_directory_honors_xdg_environment(self) -> None:
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": "/custom/data"}):
            self.assertEqual(default_data_dir(), Path("/custom/data/mailarchive"))

    def test_missing_config_loads_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = ConfigStore(Path(temporary)).load()

            self.assertTrue(settings.archive_root.endswith("MailArchive"))
            self.assertEqual(len(settings.rules), 1)
            self.assertEqual(settings.rules[0].destination, "")
            self.assertEqual(settings.rules[0].date_folder_position, DateFolderPosition.NONE)

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
            settings.accounts.append(
                Account(
                    "Personal",
                    "imap.example.org",
                    "user@example.org",
                    mailboxes=[Mailbox("user@example.org", folders=["INBOX"])],
                )
            )
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
            settings.accounts.append(
                Account(
                    label="Work",
                    username="me@example.com",
                    provider=MailProvider.MICROSOFT_GRAPH,
                    auth_mode=AuthMode.OAUTH_APPLICATION,
                    client_id="client-id",
                    tenant_id="tenant-id",
                    mailboxes=[
                        Mailbox("me@example.com", folders=["inbox"], archive_existing_messages=True)
                    ],
                )
            )

            store.save(settings)
            loaded = store.load()

            self.assertEqual(loaded.default_poll_minutes, 12)
            self.assertEqual(loaded.accounts[0].provider, MailProvider.MICROSOFT_GRAPH)
            self.assertTrue(loaded.accounts[0].mailboxes[0].archive_existing_messages)
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
        self.assertEqual(account.mailboxes[0].folders[0], "Archive")
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
            mailboxes=[Mailbox("archive@example.com", folders=["INBOX"])],
        )

        account.validate()

    def test_gmail_user_sign_in_requires_account_client_id(self) -> None:
        account = Account(
            label="Personal Gmail",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@gmail.com",
            mailboxes=[Mailbox("me@gmail.com", folders=["INBOX"])],
        )

        with self.assertRaisesRegex(ValueError, "Google OAuth desktop client ID"):
            account.validate()

    def test_microsoft_user_sign_in_uses_bundled_client_id_by_default(self) -> None:
        account = Account(
            label="Outlook",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@example.com",
            mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
        )

        account.validate()

    def test_microsoft_user_sign_in_allows_blank_tenant(self) -> None:
        account = Account(
            label="Outlook",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            username="me@example.com",
            client_id="desktop-client-id",
            mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
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
                        mailboxes=[Mailbox("me@gmail.com", folders=["INBOX"])],
                    ),
                    Account(
                        label="Outlook",
                        provider=MailProvider.MICROSOFT_GRAPH,
                        auth_mode=AuthMode.OAUTH_USER,
                        username="me@example.com",
                        client_id="microsoft-public-client-id",
                        tenant_id="organizations",
                        mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
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
            mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
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

    def test_older_settings_disable_automatic_initial_archive_per_account(self) -> None:
        settings = Settings.from_dict(
            {
                "schema_version": 2,
                "archive_root": "/tmp/archive",
                "accounts": [
                    {
                        "label": "Legacy",
                        "host": "imap.example.org",
                        "username": "me@example.org",
                    }
                ],
            }
        )

        self.assertEqual(settings.schema_version, Settings.defaults().schema_version)
        self.assertEqual(settings.state_database_path, "")
        self.assertFalse(settings.accounts[0].mailboxes[0].archive_existing_messages)

    def test_global_initial_archive_setting_migrates_to_each_account(self) -> None:
        settings = Settings.from_dict(
            {
                "schema_version": 4,
                "archive_root": "/tmp/archive",
                "archive_existing_messages": True,
                "accounts": [
                    {
                        "label": "Migrated",
                        "host": "imap.example.org",
                        "username": "me@example.org",
                    }
                ],
            }
        )

        self.assertTrue(settings.accounts[0].mailboxes[0].archive_existing_messages)
        self.assertNotIn("archive_existing_messages", settings.to_dict())


if __name__ == "__main__":
    unittest.main()
