import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import RemoteMessage
from mailarchive.mail_sources import imap_namespace
from mailarchive.models import Account, Rule, Settings
from mailarchive.service import ArchiveService, EventLevel
from mailarchive.storage import ArchiveState
from tests.helpers import sample_mail
from tests.test_service import FakeMailbox


class ImapNamespaceTests(unittest.TestCase):
    def test_changed_mailbox_identity_does_not_skip_other_messages(self):
        for changes in (
            {"folder": "Invoices"},
            {"host": "other.example.org"},
            {"port": 1993},
            {"username": "other@example.org"},
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as tmp:
                account = Account(
                    "Mail", "imap.example.org", "me@example.org", archive_existing_messages=True
                )
                settings = Settings(
                    str(Path(tmp) / "archive"), accounts=[account], rules=[Rule("All", "")]
                )
                store = MemoryCredentialStore()
                store.set(account.id, "password")
                mailbox = FakeMailbox([RemoteMessage("1", sample_mail(subject="First"))])
                service = ArchiveService(
                    store, ArchiveState(Path(tmp) / "state.db"), mailbox=mailbox
                )
                self.assertEqual(service.run_once(settings)[0].archived, 1)
                settings.accounts = [replace(account, **changes)]
                mailbox.messages = [RemoteMessage("1", sample_mail(subject="Second"))]
                second = service.run_once(settings)[0]
                self.assertEqual((second.archived, second.already_processed), (1, 0))
                self.assertEqual(service.run_once(settings)[0].already_processed, 1)
                self.assertEqual(len(list(Path(tmp).rglob("*.eml"))), 2)

    def test_inbox_and_host_case_are_normalized_but_other_folders_are_not(self):
        account = Account("Mail", "IMAP.example.org", "me@example.org")
        self.assertEqual(
            imap_namespace(account, "42"),
            imap_namespace(replace(account, host="imap.example.org", folder="inbox"), "42"),
        )
        self.assertNotEqual(
            imap_namespace(replace(account, folder="Invoices"), "42"),
            imap_namespace(replace(account, folder="invoices"), "42"),
        )
        self.assertNotEqual(imap_namespace(account, "42"), imap_namespace(account, "43"))

    def test_legacy_history_is_rechecked_when_existing_mail_enabled_with_retry_after_failure(self):
        for table, extra in (
            ("processed_message", ", '2026-01-01', 'Subject', 'Rule', '/archive', '[]'"),
            ("skipped_message", ""),
            ("unmatched_message", ", 'rules', '2026-01-01'"),
            ("source_checkpoint", None),
        ):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as tmp:
                account = Account(
                    "Mail", "imap.example.org", "me@example.org", archive_existing_messages=True
                )
                settings = Settings(
                    str(Path(tmp) / "archive"), accounts=[account], rules=[Rule("All", "")]
                )
                state = ArchiveState(Path(tmp) / "state.db")
                with closing(sqlite3.connect(state.database_path)) as db, db:
                    values = (
                        "?, 'imap:validity-1', '1'" + extra
                        if extra is not None
                        else "?, 'imap:validity-1', '2026-01-01'"
                    )
                    db.execute(f"INSERT INTO {table} VALUES ({values})", (account.id,))
                store = MemoryCredentialStore()
                store.set(account.id, "password")
                events = []

                class InterruptedMailbox(FakeMailbox):
                    def fetch_messages(self, *args, **kwargs):
                        namespace, messages = super().fetch_messages(*args, **kwargs)

                        def interrupted():
                            yield from messages
                            raise OSError("listing interrupted")

                        return namespace, interrupted()

                mailbox = InterruptedMailbox([RemoteMessage("1", sample_mail())])
                service = ArchiveService(store, state, events.append, mailbox)
                first = service.run_once(settings)[0]
                self.assertEqual((first.archived, first.failed), (1, 1))
                self.assertTrue(state.needs_imap_namespace_upgrade(account.id))
                restarted_state = ArchiveState(state.database_path)
                mailbox = FakeMailbox(
                    [
                        RemoteMessage("1", sample_mail()),
                        RemoteMessage("2", sample_mail(subject="New")),
                    ]
                )
                service = ArchiveService(store, restarted_state, events.append, mailbox)
                retry = service.run_once(settings)[0]
                self.assertEqual(
                    (retry.archived, retry.already_processed, retry.skipped_existing), (1, 1, 0)
                )
                self.assertFalse(restarted_state.needs_imap_namespace_upgrade(account.id))
                self.assertEqual(service.run_once(settings)[0].already_processed, 2)
                self.assertTrue(
                    any(
                        event.level == EventLevel.WARNING and "One-time recheck" in event.message
                        for event in events
                    )
                )
                with closing(sqlite3.connect(state.database_path)) as db:
                    self.assertEqual(
                        db.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE source_namespace = 'imap:validity-1'"
                        ).fetchone()[0],
                        1,
                    )
                # A later folder change still honors the user's new-mail-only setting.
                settings.accounts = [
                    replace(account, folder="Other", archive_existing_messages=False)
                ]
                self.assertEqual(service.run_once(settings)[0].skipped_existing, 2)

    def test_legacy_upgrade_respects_new_mail_only_and_retries_an_incomplete_baseline(self):
        for table, extra in (
            ("processed_message", ", '2026-01-01', 'Subject', 'Rule', '/archive', '[]'"),
            ("skipped_message", ""),
            ("unmatched_message", ", 'rules', '2026-01-01'"),
            ("source_checkpoint", None),
        ):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as tmp:
                account = Account("Mail", "imap.example.org", "me@example.org")
                settings = Settings(
                    str(Path(tmp) / "archive"), accounts=[account], rules=[Rule("All", "")]
                )
                state = ArchiveState(Path(tmp) / "state.db")
                with closing(sqlite3.connect(state.database_path)) as db, db:
                    values = (
                        "?, 'imap:validity-1', '1'" + extra
                        if extra is not None
                        else "?, 'imap:validity-1', '2026-01-01'"
                    )
                    db.execute(f"INSERT INTO {table} VALUES ({values})", (account.id,))
                store = MemoryCredentialStore()
                store.set(account.id, "password")
                events = []

                class InterruptedMailbox(FakeMailbox):
                    def fetch_messages(inner_self, *args, **kwargs):
                        namespace, messages = super().fetch_messages(*args, **kwargs)

                        def interrupted():
                            yield from messages
                            raise OSError("listing interrupted")

                        return namespace, interrupted()

                interrupted_mailbox = InterruptedMailbox([RemoteMessage("1", sample_mail())])
                first = ArchiveService(store, state, events.append, interrupted_mailbox).run_once(
                    settings
                )[0]
                self.assertEqual((first.archived, first.skipped_existing, first.failed), (0, 1, 1))
                self.assertEqual(interrupted_mailbox.downloaded, [])
                self.assertTrue(state.needs_imap_namespace_upgrade(account.id))
                self.assertFalse(
                    state.has_completed_initial_scan(
                        account.id, imap_namespace(account, "validity-1")
                    )
                )

                mailbox = FakeMailbox(
                    [
                        RemoteMessage("1", sample_mail()),
                        RemoteMessage("2", sample_mail(subject="Before upgrade baseline")),
                    ]
                )
                restarted = ArchiveState(state.database_path)
                service = ArchiveService(store, restarted, events.append, mailbox)
                retry = service.run_once(settings)[0]
                self.assertEqual((retry.archived, retry.skipped_existing), (0, 2))
                self.assertEqual(mailbox.downloaded, [])
                self.assertFalse(restarted.needs_imap_namespace_upgrade(account.id))
                self.assertTrue(
                    any(
                        event.level == EventLevel.INFO
                        and "without downloading existing" in event.message
                        for event in events
                    )
                )

                settings.rules[0].destination = "Changed destination"
                mailbox.messages.append(RemoteMessage("3", sample_mail(subject="After baseline")))
                next_run = service.run_once(settings)[0]
                self.assertEqual((next_run.archived, next_run.already_processed), (1, 2))
                self.assertEqual(mailbox.downloaded, [(account.id, "3")])
                self.assertEqual(len(list(Path(tmp).rglob("*.eml"))), 1)
                with closing(sqlite3.connect(state.database_path)) as db:
                    self.assertEqual(
                        db.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE source_namespace = 'imap:validity-1'"
                        ).fetchone()[0],
                        1,
                    )

                account.archive_existing_messages = True
                self.assertEqual(service.run_once(settings)[0].archived, 2)
