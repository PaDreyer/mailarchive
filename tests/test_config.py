"""Fresh profile configuration and source ownership regressions."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mailarchive.config import ConfigStore, default_data_dir
from mailarchive.models import (
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    RuleTarget,
    SaveMode,
    Settings,
)
from mailarchive.workspace import (
    APPLICATION_ID,
    DATABASE_SCHEMA_VERSION,
    WorkspaceError,
    WorkspaceStore,
)


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConfigStore(self.root)

    def test_fresh_profile_starts_without_rules_or_sources(self) -> None:
        self.assertEqual(self.store.load().accounts, [])
        self.assertEqual(self.store.load().rules, [])
        self.assertEqual(self.store.path.name, "workspace.sqlite3")
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION
            )

    def test_config_round_trip_keeps_ordered_rules_and_multiple_targets(self) -> None:
        settings = Settings.defaults()
        settings.archive_timezone = "Europe/Berlin"
        settings.rules = [
            Rule(
                "First",
                targets=[
                    RuleTarget(str(self.root / "A"), SaveMode.EMAIL_ONLY),
                    RuleTarget(str(self.root / "B")),
                ],
            ),
            Rule("Second", targets=[RuleTarget(str(self.root / "C"))]),
        ]
        self.store.save(settings)
        loaded = self.store.load()
        self.assertEqual(loaded.rules, settings.rules)
        self.assertEqual(loaded.archive_timezone, "Europe/Berlin")
        self.assertEqual(WorkspaceStore(self.store.path).configuration_revision(), 1)
        self.store.save(loaded)
        self.assertEqual(WorkspaceStore(self.store.path).configuration_revision(), 1)

    def test_empty_rules_stay_empty_when_saved_again(self) -> None:
        self.store.save(Settings.defaults())
        again = self.store.load()
        again.rules.clear()
        self.store.save(again)
        self.assertEqual(self.store.load().rules, [])

    def test_rule_json_has_only_targets_as_its_destination_authority(self) -> None:
        settings = Settings.defaults()
        settings.rules = [Rule("Archive", targets=[RuleTarget(str(self.root / "Archive"))])]
        self.store.save(settings)
        with closing(sqlite3.connect(self.store.path)) as db:
            payload = json.loads(db.execute("SELECT payload FROM config_revision").fetchone()[0])

        rule = payload["rules"][0]
        self.assertNotIn("destination", rule)
        self.assertNotIn("save_mode", rule)
        self.assertNotIn("attachments_in_destination", rule)
        self.assertEqual(rule["targets"][0]["path"], str(self.root / "Archive"))

    def test_credentials_and_old_global_paths_are_not_serialized(self) -> None:
        settings = Settings.defaults()
        settings.archive_root = str(self.root / "old-global-archive")
        settings.state_database_path = str(self.root / "old-state.sqlite3")
        settings.accounts = [
            Account(
                "Mail",
                "imap.example.org",
                "user@example.org",
                mailboxes=[Mailbox("user@example.org", ["INBOX"])],
            )
        ]
        self.store.save(settings)
        with closing(sqlite3.connect(self.store.path)) as db:
            payload = db.execute("SELECT payload FROM config_revision").fetchone()[0]
        for excluded in (
            '"password":',
            "old-global-archive",
            "old-state.sqlite3",
            "archive_existing_messages",
        ):
            self.assertNotIn(excluded, payload)

    def test_duplicate_provider_mailbox_is_rejected_across_accounts(self) -> None:
        settings = Settings.defaults()
        settings.accounts = [
            Account("First", "imap.example.org", "same@example.org"),
            Account("Second", "imap.example.org", "same@example.org"),
        ]
        with self.assertRaisesRegex(WorkspaceError, "configured twice"):
            self.store.save(settings)

    def test_source_id_survives_owner_change(self) -> None:
        mailbox = Mailbox("same@example.org", ["INBOX"])
        first = Account("First", "imap.example.org", "same@example.org", mailboxes=[mailbox])
        settings = Settings.defaults()
        settings.accounts = [first]
        self.store.save(settings)
        second = Account("Second", "imap.example.org", "same@example.org", mailboxes=[mailbox])
        settings.accounts = [second]
        self.store.save(settings)
        self.assertEqual(self.store.load().accounts[0].mailboxes[0].id, mailbox.id)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT account_id FROM source").fetchone()[0], second.id)

    def test_changed_server_gets_a_new_source_without_erasing_old_identity(self) -> None:
        mailbox = Mailbox("same@example.org", ["INBOX"])
        account = Account("Mail", "first.example.org", mailbox.address, mailboxes=[mailbox])
        settings = Settings.defaults()
        settings.accounts = [account]
        self.store.save(settings)
        old_id = mailbox.id
        account.host = "second.example.org"
        self.store.save(settings)
        self.assertNotEqual(mailbox.id, old_id)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT enabled FROM source WHERE id=?", (old_id,)).fetchone()[0], 0
            )

    def test_removed_imap_and_graph_folders_lose_their_automatic_scope(self) -> None:
        for provider in (MailProvider.GENERIC_IMAP, MailProvider.MICROSOFT_GRAPH):
            with self.subTest(provider=provider.value):
                path = self.root / provider.value / "workspace.sqlite3"
                state = WorkspaceStore(path)
                mailbox = Mailbox("same@example.org", ["Keep", "Remove"])
                account = Account(
                    provider.value,
                    host="imap.example.org" if provider == MailProvider.GENERIC_IMAP else "",
                    username=mailbox.address,
                    provider=provider,
                    auth_mode=(
                        AuthMode.PASSWORD
                        if provider == MailProvider.GENERIC_IMAP
                        else AuthMode.OAUTH_USER
                    ),
                    mailboxes=[mailbox],
                )
                settings = Settings.defaults()
                settings.accounts = [account]
                state.save_settings(settings)
                for folder in mailbox.folders:
                    state.finish_scope(mailbox.id, folder, f"process:{folder}", folder, "cursor")

                mailbox.folders = ["Keep"]
                state.save_settings(settings)

                self.assertIsNotNone(state.scope(mailbox.id, "Keep"))
                self.assertIsNone(state.scope(mailbox.id, "Remove"))
                mailbox.folders.append("Remove")
                state.save_settings(settings)
                self.assertIsNone(state.scope(mailbox.id, "Remove"))

    def test_reenabled_source_starts_with_fresh_scopes(self) -> None:
        mailbox = Mailbox("same@example.org", ["INBOX"])
        account = Account("Mail", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        settings = Settings.defaults()
        settings.accounts = [account]
        self.store.save(settings)
        state = WorkspaceStore(self.store.path)
        state.finish_scope(mailbox.id, "INBOX", "process", "sync", "cursor")

        account.enabled = False
        self.store.save(settings)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(
                db.execute("SELECT enabled FROM source WHERE id=?", (mailbox.id,)).fetchone()[0], 0
            )
        account.enabled = True
        self.store.save(settings)

        self.assertIsNone(state.scope(mailbox.id, "INBOX"))

    def test_all_folder_selection_preserves_only_still_selected_scopes(self) -> None:
        mailbox = Mailbox("same@example.org", ["one"])
        account = Account("Mail", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        settings = Settings.defaults()
        settings.accounts = [account]
        self.store.save(settings)
        state = WorkspaceStore(self.store.path)
        state.finish_scope(mailbox.id, "one", "process:one", "sync:one", "one-cursor")

        mailbox.folders = []
        self.store.save(settings)
        self.assertEqual(state.scope(mailbox.id, "one")["cursor"], "one-cursor")
        state.finish_scope(mailbox.id, "two", "process:two", "sync:two", "two-cursor")

        mailbox.folders = ["one"]
        self.store.save(settings)
        self.assertEqual(state.scope(mailbox.id, "one")["cursor"], "one-cursor")
        self.assertIsNone(state.scope(mailbox.id, "two"))

    def test_unknown_profile_is_rejected_without_overwriting_it(self) -> None:
        self.store.path.write_bytes(b"not a MailArchive database")
        with self.assertRaises(WorkspaceError):
            self.store.load()
        self.assertEqual(self.store.path.read_bytes(), b"not a MailArchive database")

    def test_unsupported_settings_format_is_rejected(self) -> None:
        self.assertRaisesRegex(ValueError, "Unsupported", Settings.from_dict, {"schema_version": 9})

    def test_invalid_target_is_rejected_before_saving(self) -> None:
        settings = Settings.defaults()
        settings.rules = [Rule("Bad", targets=[RuleTarget("relative/path")])]
        with self.assertRaisesRegex(ValueError, "full destination"):
            self.store.save(settings)

    def test_corrupt_active_revision_cannot_be_silently_superseded(self) -> None:
        settings = Settings.defaults()
        self.store.save(settings)
        state = WorkspaceStore(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE config_revision SET payload='{' WHERE active=1")

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            state.save_settings(Settings.defaults())
        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            state.prepare_run_settings(Settings.defaults())

    @unittest.skipUnless(os.name == "posix", "XDG data directories are POSIX-specific")
    def test_default_data_directory_honors_xdg_environment(self) -> None:
        with patch.dict("os.environ", {"XDG_DATA_HOME": "/custom/data"}):
            self.assertEqual(default_data_dir(), Path("/custom/data/mailarchive"))


if __name__ == "__main__":
    unittest.main()
