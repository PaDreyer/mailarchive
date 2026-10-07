"""Profile work ownership is checked before any destructive recovery."""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.errors import WorkspaceError
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.profile_ownership import exclusive_profile_directory
from mailarchive.infrastructure.sqlite_core import SqliteDatabase
from tests.test_restart_core import FakeSource, Registry, raw_mail
from tests.workspace_fixture import make_service


class ProfileOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "shared" / "profile.data"
        self.profile = ProfileDatabase(self.path)

    def test_direct_open_rejects_sibling_profile_without_creating_database(self):
        sibling = self.path.with_name("another-profile")
        with self.assertRaisesRegex(WorkspaceError, "different folder"):
            ProfileDatabase(sibling, recover=True)
        self.assertFalse(sibling.exists())
        self.assertEqual(ProfileDatabase(self.path, recover=True).database_path, self.path)

    def test_preexisting_sibling_profiles_are_rejected_before_recovery(self):
        mailbox = Mailbox("owner@example.org", ["INBOX"], archive_existing_messages=True)
        account = Account("Owner", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        obstruction = self.root / "offline"
        obstruction.write_text("not a directory")
        settings = Settings(
            accounts=[account],
            rules=[Rule("Archive", targets=[RuleTarget(str(obstruction / "archive"))])],
        )
        source = FakeSource(
            {"1": RemoteMessage("1", raw_mail(), datetime.now(timezone.utc), "imap_internaldate")}
        )
        service = make_service(self.profile, Registry(source))
        self.assertEqual(service.run_once(settings)[0].failed, 1)
        plan = dict(self.profile.delivery.open_plans()[0])
        raw = Path(plan["raw_path"])
        content = raw.read_bytes()
        SqliteDatabase(self.path.with_name("old-profile-without-extension"))
        with patch.object(self.profile.operations, "recover") as recover:
            with self.assertRaisesRegex(WorkspaceError, "different folder"):
                self.profile.recover()
            recover.assert_not_called()
        with self.assertRaisesRegex(WorkspaceError, "different folder"):
            ProfileDatabase(self.path, recover=True)
        self.assertEqual(raw.read_bytes(), content)
        self.assertEqual(dict(self.profile.delivery.open_plans()[0]), plan)

    def test_unknown_or_damaged_sqlite_sibling_cannot_lose_work_files(self):
        sibling = self.path.with_name("unknown.data")
        raw = self.profile.spool.path / "accepted.eml"
        raw.write_bytes(b"accepted")
        with closing(sqlite3.connect(sibling)) as db, db:
            db.execute("CREATE TABLE unrelated(value)")
        for header in (sibling.read_bytes(), b"SQLite format 3\0" + b"damaged"):
            with self.subTest(header=header[:20]):
                sibling.write_bytes(header)
                with self.assertRaisesRegex(WorkspaceError, "different folder"):
                    self.profile.recover()
                self.assertEqual(raw.read_bytes(), b"accepted")

    def test_profile_marker_still_identifies_a_sibling_with_corrupt_magic(self):
        sibling = self.path.with_name("damaged-profile")
        SqliteDatabase(sibling)
        with sibling.open("r+b") as handle:
            handle.write(b"broken SQLite magic")
        raw = self.profile.spool.path / "accepted.eml"
        raw.write_bytes(b"accepted")
        with self.assertRaisesRegex(WorkspaceError, "different folder"):
            self.profile.recover()
        self.assertEqual(raw.read_bytes(), b"accepted")

    def test_unreadable_sibling_fails_ownership_verification_without_cleanup(self):
        sibling = self.path.with_name("unreadable")
        sibling.write_bytes(b"SQLite format 3\0")
        raw = self.profile.spool.path / "accepted.eml"
        raw.write_bytes(b"accepted")
        original = Path.open

        def unavailable(path, *args, **kwargs):
            if path == sibling:
                raise PermissionError("Cannot inspect sibling")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", unavailable):
            with self.assertRaisesRegex(WorkspaceError, "ownership"):
                self.profile.recover()
        self.assertEqual(raw.read_bytes(), b"accepted")

    def test_third_profile_cannot_bypass_an_empty_shared_work_directory(self):
        store = ConfigStore(self.root / "settings")
        store.select_database(self.path)
        other = self.root / "other" / "profile.sqlite3"
        store.select_database(other)
        sibling = self.path.with_name("sibling.sqlite3")
        with self.assertRaisesRegex(WorkspaceError, "different folder"):
            store.select_database(sibling)
        self.assertFalse(sibling.exists())
        self.assertTrue(self.profile.spool.path.is_dir())
        self.assertEqual(ConfigStore(self.root / "settings").path, other)

    def test_direct_creation_cannot_claim_unowned_raw_work_files(self):
        destination = self.root / "unowned" / "profile.sqlite3"
        work = destination.parent / "work"
        work.mkdir(parents=True)
        raw = work / "accepted.eml"
        raw.write_bytes(b"accepted")
        with self.assertRaisesRegex(WorkspaceError, "contains mail work files"):
            ProfileDatabase(destination, recover=True)
        self.assertFalse(destination.exists())
        self.assertEqual(raw.read_bytes(), b"accepted")

    def test_relative_and_symlink_paths_resolve_to_the_existing_owner(self):
        relative = Path(os.path.relpath(self.path))
        self.assertEqual(ProfileDatabase(relative).database_path, self.path)
        alias = self.path.with_name("alias.sqlite3")
        try:
            alias.symlink_to(self.path)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable")
        reopened = ProfileDatabase(alias, recover=True)
        self.assertEqual(reopened.database_path, self.path)
        self.assertEqual(reopened.spool.path, self.profile.spool.path)

    def test_single_profile_can_be_renamed_or_copied_to_another_folder(self):
        self.profile.configuration.save_settings(Settings(archive_timezone="Europe/Berlin"))
        renamed = self.path.with_name("renamed.db")
        self.path.rename(renamed)
        reopened = ProfileDatabase(renamed, recover=True)
        self.assertEqual(reopened.configuration.load_settings().archive_timezone, "Europe/Berlin")
        copied = self.root / "copy" / "profile.data"
        copied.parent.mkdir()
        shutil.copyfile(renamed, copied)
        self.assertEqual(
            ProfileDatabase(copied, recover=True).configuration.load_settings().archive_timezone,
            "Europe/Berlin",
        )

    def test_failed_selection_does_not_leave_a_permanent_owner(self):
        store = ConfigStore(self.root / "settings")
        destination = self.root / "failed" / "first.sqlite3"
        with patch.object(store, "_save_database_path", side_effect=OSError("read-only")):
            with self.assertRaisesRegex(OSError, "read-only"):
                store.select_database(destination)
        self.assertFalse(destination.exists())
        alternate = destination.with_name("second.sqlite3")
        self.assertEqual(store.select_database(alternate)[0].database_path, alternate)

    def test_independent_process_cannot_initialize_or_recover_under_ownership_lock(self):
        sibling = self.path.with_name("other.data")
        raw = self.profile.spool.path / "accepted.eml"
        raw.write_bytes(b"accepted")
        script = (
            "from pathlib import Path; "
            "from mailarchive.infrastructure.profile_database import ProfileDatabase; "
            "ProfileDatabase(Path(__import__('sys').argv[1]), recover=True)"
        )
        with exclusive_profile_directory(self.path):
            process = subprocess.run(
                [sys.executable, "-c", script, str(sibling)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("ownership could not be verified", process.stderr)
        self.assertFalse(sibling.exists())
        self.assertEqual(raw.read_bytes(), b"accepted")
