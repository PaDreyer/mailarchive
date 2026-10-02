"""Fresh profile configuration and source ownership regressions."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.errors import WorkspaceError
from mailarchive.application.service import EventLevel, ServiceEvent
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    RuleTarget,
    SaveMode,
    Settings,
)
from mailarchive.infrastructure.diagnostics import ActivityLog
from mailarchive.infrastructure.profile_location import ConfigStore, default_data_dir
from mailarchive.infrastructure.sqlite_schema import APPLICATION_ID
from tests.test_restart_core import FakeSource, Registry, raw_mail
from tests.workspace_fixture import WorkspaceStore, make_service


def activity_log(path: Path) -> ActivityLog:
    profile = WorkspaceStore(path)
    return ActivityLog(profile.connection, profile.ensure_configuration_revision)


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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_selecting_existing_database_uses_its_settings_activity_and_work(self) -> None:
        old_settings = Settings.defaults()
        self.store.save(old_settings)
        activity_log(self.store.path).record(ServiceEvent(EventLevel.INFO, "Old profile"))
        old_path = self.store.path
        target_store = ConfigStore(self.root / "elsewhere")
        mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = Account("Mail", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        rule = Rule("Archive", targets=[RuleTarget(str(obstruction / "archive"))])
        target_settings = Settings(accounts=[account], rules=[rule])
        target_store.save(target_settings)
        activity_log(target_store.path).record(ServiceEvent(EventLevel.INFO, "Target profile"))
        source = FakeSource(
            {
                "1": RemoteMessage(
                    "1",
                    raw_mail(),
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    "imap_internaldate",
                )
            }
        )
        target_state = WorkspaceStore(target_store.path)
        service = make_service(target_state, Registry(source))
        self.assertEqual(service.run_range(target_settings, {mailbox.id})[0].failed, 1)
        work_copy = Path(target_state.open_plans()[0]["raw_path"])
        self.assertTrue(work_copy.exists())

        selected, loaded = self.store.select_database(target_store.path)

        self.assertEqual(ConfigStore(self.root).path, target_store.path)
        self.assertEqual(ConfigStore(self.root).load().rules, [rule])
        self.assertEqual(loaded.rules, [rule])
        self.assertEqual(
            activity_log(selected.database_path).page().events[0].message, "Target profile"
        )
        self.assertEqual(Path(selected.delivery.open_plans()[0]["raw_path"]), work_copy)
        self.assertEqual(
            activity_log(self.store.default_state_database_path).page().events[0].message,
            "Old profile",
        )
        obstruction.unlink()
        self.assertEqual(make_service(selected, Registry(source)).resume_open(), (1, 0))
        self.assertTrue(list((self.root / "offline" / "archive").glob("*.eml")))

        restored, restored_settings = self.store.select_database(old_path)
        self.assertEqual(restored.database_path, old_path)
        self.assertEqual(restored_settings.rules, old_settings.rules)
        self.assertEqual(activity_log(old_path).page().events[0].message, "Old profile")
        self.assertEqual(ConfigStore(self.root).path, old_path)

    def test_selecting_missing_database_creates_an_empty_profile(self) -> None:
        old_rule = Rule("Old", targets=[RuleTarget(str(self.root / "Archive"))])
        self.store.save(Settings(rules=[old_rule]))
        activity_log(self.store.path).record(ServiceEvent(EventLevel.INFO, "Old profile"))
        destination = self.root / "elsewhere" / "mail.sqlite3"

        selected, loaded = self.store.select_database(destination)

        self.assertEqual(selected.database_path, destination)
        self.assertEqual(loaded.rules, [])
        self.assertEqual(activity_log(destination).page().total, 0)
        self.assertEqual(ConfigStore(self.root).path, destination)
        self.assertEqual(
            WorkspaceStore(self.store.default_state_database_path).load_settings().rules, [old_rule]
        )

    def test_new_database_does_not_reuse_another_profiles_work_folder(self) -> None:
        self.store.save(Settings.defaults())
        work = self.root / "elsewhere" / "work"
        work.mkdir(parents=True)
        raw = work / "accepted.eml"
        raw.write_bytes(b"mail")

        with self.assertRaisesRegex(ValueError, "contains mail work files"):
            self.store.select_database(self.root / "elsewhere" / "mail.sqlite3")

        self.assertEqual(raw.read_bytes(), b"mail")
        self.assertFalse((work.parent / "mail.sqlite3").exists())
        self.assertEqual(ConfigStore(self.root).path, self.store.default_state_database_path)

    def test_selecting_invalid_existing_database_keeps_original_selected(self) -> None:
        self.store.save(Settings.defaults())
        destination = self.root / "elsewhere" / "another.sqlite3"
        destination.parent.mkdir()
        destination.write_bytes(b"unrelated")

        with self.assertRaisesRegex(WorkspaceError, "Could not open MailArchive profile database"):
            self.store.select_database(destination)

        self.assertEqual(destination.read_bytes(), b"unrelated")
        self.assertEqual(ConfigStore(self.root).path, self.store.default_state_database_path)

    def test_selecting_database_rolls_back_when_location_cannot_be_saved(self) -> None:
        self.store.save(Settings.defaults())
        destination = self.root / "elsewhere" / "mail.sqlite3"

        with (
            patch.object(self.store, "_save_database_path", side_effect=OSError("read-only")),
            self.assertRaisesRegex(OSError, "read-only"),
        ):
            self.store.select_database(destination)

        self.assertFalse(destination.exists())
        self.assertEqual(ConfigStore(self.root).path, self.store.default_state_database_path)
        self.assertEqual(self.store.load().rules, [])

    def test_missing_configured_database_does_not_create_an_empty_profile(self) -> None:
        settings = Settings.defaults()
        self.store.save(settings)
        destination = self.root / "elsewhere" / "mail.sqlite3"
        self.store.select_database(destination)
        destination.unlink()

        with self.assertRaisesRegex(RuntimeError, "configured database is missing"):
            ConfigStore(self.root).load()
        self.assertFalse(destination.exists())

    def test_invalid_location_file_does_not_fall_back_to_default_profile(self) -> None:
        self.store.save(Settings.defaults())
        self.store.location_file.write_text('{"version": 1, "path": "relative.sqlite3"}')

        with self.assertRaisesRegex(RuntimeError, "database location"):
            ConfigStore(self.root)

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

    def test_first_check_choice_survives_config_round_trip(self) -> None:
        settings = Settings.defaults()
        settings.accounts = [
            Account(
                "Mail",
                "imap.example.org",
                "user@example.org",
                mailboxes=[
                    Mailbox(
                        "user@example.org",
                        ["INBOX"],
                        archive_existing_messages=True,
                    )
                ],
            )
        ]

        self.store.save(settings)

        self.assertTrue(self.store.load().accounts[0].mailboxes[0].archive_existing_messages)

    def test_profile_missing_first_check_choice_is_rejected(self) -> None:
        settings = Settings.defaults()
        settings.accounts = [
            Account(
                "Mail",
                "imap.example.org",
                "user@example.org",
                mailboxes=[Mailbox("user@example.org", ["INBOX"])],
            )
        ]
        self.store.save(settings)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            row = db.execute("SELECT id, payload FROM config_revision WHERE active=1").fetchone()
            payload = json.loads(row[1])
            del payload["accounts"][0]["mailboxes"][0]["archive_existing_messages"]
            db.execute(
                "UPDATE config_revision SET payload=? WHERE id=?",
                (json.dumps(payload, sort_keys=True), row[0]),
            )

        with self.assertRaises(WorkspaceError):
            self.store.load()

    def test_credentials_and_old_global_paths_are_not_serialized(self) -> None:
        settings = Settings.defaults()
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
            '"archive_root":',
            '"state_database_path":',
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
