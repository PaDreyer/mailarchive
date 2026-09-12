import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import RemoteMessage
from mailarchive.models import (
    Account,
    Condition,
    DateFolderPosition,
    Mailbox,
    MailField,
    Rule,
    SaveMode,
    Settings,
)
from mailarchive.rules import matching_rules_fingerprint
from mailarchive.service import ArchiveRunBusyError, ArchiveService, EventLevel
from mailarchive.storage import ArchiveState
from tests.helpers import imap_namespace, sample_mail


class FakeMailbox:
    def __init__(self, messages: list[RemoteMessage]) -> None:
        self.messages = messages
        self.downloaded: list[tuple[str, str]] = []

    def fetch_messages(self, account: Account, password: str, should_fetch=None, *, sync=None):
        account, target = account.account, account
        from mailarchive.mail_identity import imap_scope

        scope = imap_scope(target, "validity-1")

        def messages():
            for message in self.messages:
                if should_fetch is None or should_fetch(scope, message.id):
                    self.downloaded.append((account.id, message.id))
                    yield message
            if sync is not None:
                sync.next_cursor = "0"

        return scope, messages()


class FailingMailbox:
    def fetch_messages(self, account: Account, password: str, should_fetch=None, *, sync=None):
        raise RuntimeError("mailbox unavailable")


class ServiceTests(unittest.TestCase):
    def test_account_change_excludes_runs_and_releases_the_lock_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events, progress = [], []
            service = ArchiveService(
                MemoryCredentialStore(),
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
                progress_handler=progress.append,
            )
            with self.assertRaisesRegex(OSError, "save failed"):
                with service.account_change():
                    with self.assertRaises(ArchiveRunBusyError):
                        service.run_once(Settings.defaults())
                    self.assertEqual(events[-1].level, EventLevel.WARNING)
                    self.assertEqual(progress, [])
                    raise OSError("save failed")

            with service.account_change():
                pass
            service.run_once(Settings.defaults())
            self.assertEqual(events[-1].level, EventLevel.INFO)
            self.assertFalse(progress[-1].active)

    def test_live_progress_counts_archived_and_skipped_mail_without_logging_each_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            account.mailboxes[0].archive_existing_messages = True
            settings = Settings.defaults()
            settings.accounts = [account]
            settings.archive_root = str(Path(temporary) / "Archive")
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            progress = []
            events = []
            mailbox = FakeMailbox(
                [RemoteMessage(str(index), sample_mail()) for index in range(100)]
            )
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
                mailbox,
                progress_handler=progress.append,
            )

            with patch("mailarchive.service.time.monotonic", return_value=1.0):
                first = service.run_once(settings)[0]
                self.assertEqual(first.checked, 100)
                self.assertIn("100 checked, 100 archived, 0 skipped", progress[-1].message)
                self.assertFalse(progress[-1].active)
                self.assertTrue(any("Downloading email 1" in item.message for item in progress))
                self.assertEqual(len(events), 2)
                self.assertLess(len(progress), 10)

                progress.clear()
                second = service.run_once(settings)[0]

            self.assertEqual(second.checked, 100)
            self.assertEqual(second.skipped, 100)
            self.assertIn("100 checked, 0 archived, 100 skipped", progress[-1].message)
            self.assertFalse(progress[-1].active)
            self.assertEqual(len(mailbox.downloaded), 100)
            self.assertTrue(any("Checking messages" in item.message for item in progress))

    def test_progress_announces_connection_before_fetch_and_finishes_after_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            progress = []

            class ObservedMailbox:
                def fetch_messages(inner_self, *_, **kwargs):
                    self.assertIn("Connecting and loading", progress[-1].message)
                    self.assertTrue(progress[-1].active)
                    raise RuntimeError("mailbox unavailable")

            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                mailbox=ObservedMailbox(),
                progress_handler=progress.append,
            )

            result = service.run_once(settings)[0]

            self.assertEqual(result.failed, 1)
            self.assertFalse(progress[-1].active)
            self.assertIn("1 failed", progress[-1].message)

    def test_empty_and_multi_account_runs_finish_progress_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(str(Path(temporary) / "Archive"))
            progress = []
            credentials = MemoryCredentialStore()
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                mailbox=FakeMailbox([]),
                progress_handler=progress.append,
            )
            service.run_once(settings)
            self.assertFalse(progress[-1].active)
            self.assertIn("No active", progress[-1].message)

            progress.clear()
            settings.accounts = [
                Account("First", mailboxes=[Mailbox("", folders=["INBOX"])]),
                Account("Second", mailboxes=[Mailbox("", folders=["INBOX"])]),
            ]
            for account in settings.accounts:
                credentials.set(account.id, "secret")
            service.run_once(settings)

            self.assertEqual(sum(not item.active for item in progress), 1)
            self.assertTrue(
                any(
                    item.message.startswith("First:") and "Connecting" in item.message
                    for item in progress
                )
            )
            self.assertTrue(
                any(
                    item.message.startswith("Second:") and "Connecting" in item.message
                    for item in progress
                )
            )

    def test_same_message_uses_different_rules_for_different_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Account(
                "Work",
                "imap.example.org",
                "work@example.org",
                mailboxes=[
                    Mailbox("work@example.org", folders=["INBOX"], archive_existing_messages=True)
                ],
            )
            personal = Account(
                "Personal",
                "imap.example.org",
                "personal@example.org",
                mailboxes=[
                    Mailbox(
                        "personal@example.org", folders=["INBOX"], archive_existing_messages=True
                    )
                ],
            )
            settings = Settings(
                str(Path(temporary) / "Archive"),
                accounts=[work, personal],
                rules=[
                    Rule("Work only", "Work", account_ids=[work.id]),
                    Rule(
                        "Fallback",
                        "Personal",
                        date_folder_position=DateFolderPosition.BEFORE_SUBFOLDER,
                    ),
                ],
            )
            credentials = MemoryCredentialStore()
            for account in settings.accounts:
                credentials.set(account.id, "secret")
            state = ArchiveState(Path(temporary) / "state.sqlite3")
            service = ArchiveService(
                credentials,
                state,
                mailbox=FakeMailbox([RemoteMessage("same-message", sample_mail())]),
            )

            results = service.run_once(settings)

            self.assertEqual([result.archived for result in results], [1, 1])
            self.assertEqual(
                sorted(row["rule_name"] for row in state.recent()), ["Fallback", "Work only"]
            )
            self.assertEqual(
                sorted(row["destination"] for row in state.recent()),
                sorted(
                    [
                        str(Path(settings.archive_root) / "Work"),
                        str(Path(settings.archive_root) / "2026" / "09" / "Personal"),
                    ]
                ),
            )
            settings.rules[0].date_folder_position = DateFolderPosition.AFTER_SUBFOLDER
            self.assertEqual(
                [result.already_processed for result in service.run_once(settings)], [1, 1]
            )
            self.assertEqual(len(list(Path(settings.archive_root).rglob("*.eml"))), 2)
            self.assertTrue(
                state.was_processed(work.id, imap_namespace(work, "validity-1"), "same-message")
            )
            self.assertTrue(
                state.was_processed(
                    personal.id, imap_namespace(personal, "validity-1"), "same-message"
                )
            )

    def test_excluded_account_stays_unprocessed_until_rule_includes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Account(
                "Work",
                "imap.example.org",
                "work@example.org",
                mailboxes=[
                    Mailbox("work@example.org", folders=["INBOX"], archive_existing_messages=True)
                ],
            )
            personal = Account(
                "Personal",
                "imap.example.org",
                "personal@example.org",
                mailboxes=[
                    Mailbox(
                        "personal@example.org", folders=["INBOX"], archive_existing_messages=True
                    )
                ],
            )
            scoped = Rule("Scoped", "Selected", account_ids=[work.id])
            settings = Settings(
                str(Path(temporary) / "Archive"), accounts=[work, personal], rules=[scoped]
            )
            credentials = MemoryCredentialStore()
            for account in settings.accounts:
                credentials.set(account.id, "secret")
            state = ArchiveState(Path(temporary) / "state.sqlite3")
            service = ArchiveService(
                credentials, state, mailbox=FakeMailbox([RemoteMessage("message", sample_mail())])
            )

            first = service.run_once(settings)
            self.assertEqual(first[0].archived, 1)
            self.assertEqual(first[1].unmatched, 1)
            self.assertFalse(
                state.was_processed(personal.id, imap_namespace(personal, "validity-1"), "message")
            )
            scoped.account_ids.append(personal.id)
            second = service.run_once(settings)
            self.assertEqual(second[0].already_processed, 1)
            self.assertEqual(second[1].archived, 1)

    def test_state_database_relocation_updates_the_running_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ArchiveService(
                MemoryCredentialStore(),
                ArchiveState(root / "original.sqlite3"),
            )

            relocated = service.relocate_state_database(root / "custom" / "mail.db")

            self.assertIs(service.state, relocated)
            self.assertEqual(service.state.database_path, (root / "custom" / "mail.db").resolve())

    def test_archives_once_and_skips_same_uid_next_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            account.mailboxes[0].archive_existing_messages = True
            settings = Settings.defaults()
            settings.archive_root = str(Path(temporary) / "Archive")
            settings.accounts = [account]
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            events = []
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
                FakeMailbox([RemoteMessage("42", sample_mail())]),
            )

            first = service.run_once(settings)[0]
            second = service.run_once(settings)[0]

            self.assertEqual(first.archived, 1)
            self.assertEqual(second.archived, 0)
            self.assertEqual(second.already_processed, 1)
            self.assertTrue(any(event.level == EventLevel.SUCCESS for event in events))

    def test_missing_password_becomes_visible_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            events = []
            service = ArchiveService(
                MemoryCredentialStore(),
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
                FakeMailbox([]),
            )
            result = service.run_once(settings)[0]
            self.assertEqual(result.failed, 1)
            self.assertIn("No password", events[-1].message)
            self.assertEqual(events[-1].level, EventLevel.ERROR)

    def test_unmatched_mail_becomes_visible_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            account.mailboxes[0].archive_existing_messages = True
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            settings.rules = []
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            events = []
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
                FakeMailbox([RemoteMessage("8", sample_mail())]),
            )
            result = service.run_once(settings)[0]
            self.assertEqual(result.unmatched, 1)
            self.assertEqual(events[-1].level, EventLevel.WARNING)
            self.assertIn("without a matching rule", events[-1].message)

    def test_first_check_skips_existing_messages_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            mailbox = FakeMailbox([RemoteMessage("existing", sample_mail(subject="Existing"))])
            state = ArchiveState(Path(temporary) / "state.sqlite3")
            events = []
            service = ArchiveService(credentials, state, events.append, mailbox)

            first = service.run_once(settings)[0]
            mailbox.messages.append(RemoteMessage("new", sample_mail(subject="New")))
            second = service.run_once(settings)[0]

            self.assertEqual(first.skipped_existing, 1)
            self.assertEqual(first.archived, 0)
            self.assertEqual(second.archived, 1)
            self.assertEqual(second.already_processed, 1)
            self.assertTrue(
                state.was_processed(account.id, imap_namespace(account, "validity-1"), "new")
            )
            self.assertFalse(
                state.was_processed(account.id, imap_namespace(account, "validity-1"), "existing")
            )
            self.assertIn("existing email(s) skipped", events[1].message)

    def test_enabling_existing_messages_archives_messages_skipped_at_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            state = ArchiveState(Path(temporary) / "state.sqlite3")
            service = ArchiveService(
                credentials,
                state,
                mailbox=FakeMailbox([RemoteMessage("existing", sample_mail())]),
            )

            skipped = service.run_once(settings)[0]
            account.mailboxes[0].archive_existing_messages = True
            archived = service.run_once(settings)[0]

            self.assertEqual(skipped.skipped_existing, 1)
            self.assertEqual(archived.archived, 1)
            self.assertTrue(
                state.was_processed(account.id, imap_namespace(account, "validity-1"), "existing")
            )

    def test_new_message_after_an_empty_initial_check_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            mailbox = FakeMailbox([])
            state = ArchiveState(Path(temporary) / "state.sqlite3")
            service = ArchiveService(credentials, state, mailbox=mailbox)

            initial = service.run_once(settings)[0]
            mailbox.messages.append(RemoteMessage("new", sample_mail()))
            later = service.run_once(settings)[0]

            self.assertEqual(initial.skipped_existing, 0)
            self.assertTrue(
                state.has_completed_initial_scan(account.id, imap_namespace(account, "validity-1"))
            )
            self.assertEqual(later.archived, 1)

    def test_existing_message_choice_is_independent_per_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            include_existing = Account(
                "Include existing",
                "imap.example.org",
                "include@example.org",
                mailboxes=[
                    Mailbox(
                        "include@example.org", folders=["INBOX"], archive_existing_messages=True
                    )
                ],
            )
            new_only = Account(
                "New only",
                "imap.example.org",
                "new@example.org",
                mailboxes=[Mailbox("new@example.org", folders=["INBOX"])],
            )
            settings = Settings(
                str(Path(temporary) / "Archive"),
                accounts=[include_existing, new_only],
            )
            credentials = MemoryCredentialStore()
            credentials.set(include_existing.id, "secret")
            credentials.set(new_only.id, "secret")
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                mailbox=FakeMailbox([RemoteMessage("existing", sample_mail())]),
            )

            results = {result.account_id: result for result in service.run_once(settings)}

            self.assertEqual(results[include_existing.id].archived, 1)
            self.assertEqual(results[include_existing.id].skipped_existing, 0)
            self.assertEqual(results[new_only.id].archived, 0)
            self.assertEqual(results[new_only.id].skipped_existing, 1)

    def test_no_active_accounts_emits_information_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = []
            service = ArchiveService(
                MemoryCredentialStore(),
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
            )
            settings = Settings.defaults()
            settings.accounts = [
                Account("Disabled", enabled=False, mailboxes=[Mailbox("", folders=["INBOX"])])
            ]

            self.assertEqual(service.run_once(settings), [])
            self.assertEqual(events[-1].level, EventLevel.INFO)
            self.assertIn("No active", events[-1].message)

    def test_account_filter_runs_only_selected_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Account(
                "Selected",
                "imap.example.org",
                "selected@example.org",
                mailboxes=[Mailbox("selected@example.org", folders=["INBOX"])],
            )
            ignored = Account(
                "Ignored",
                "imap.example.org",
                "ignored@example.org",
                mailboxes=[Mailbox("ignored@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[selected, ignored])
            credentials = MemoryCredentialStore()
            credentials.set(selected.id, "secret")
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                mailbox=FakeMailbox([]),
            )

            results = service.run_once(settings, {selected.id})

            self.assertEqual([result.account_id for result in results], [selected.id])

    def test_concurrent_run_and_relocation_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = []
            service = ArchiveService(
                MemoryCredentialStore(),
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
            )
            service._run_lock.acquire()
            try:
                with self.assertRaises(ArchiveRunBusyError):
                    service.run_once(Settings.defaults())
                self.assertEqual(events[-1].level, EventLevel.WARNING)
                with self.assertRaisesRegex(RuntimeError, "cannot be changed"):
                    service.relocate_state_database(Path(temporary) / "other.sqlite3")
            finally:
                service._run_lock.release()

    def test_mailbox_failure_becomes_account_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account(
                "Personal",
                "imap.example.org",
                "me@example.org",
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            settings = Settings(str(Path(temporary) / "Archive"), accounts=[account])
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "secret")
            events = []
            service = ArchiveService(
                credentials,
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
                FailingMailbox(),
            )

            result = service.run_once(settings)[0]

            self.assertEqual(result.failed, 1)
            self.assertEqual(events[-1].level, EventLevel.ERROR)
            self.assertIn("mailbox unavailable", events[-1].message)


class UnmatchedMailTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[
                Mailbox("me@example.org", folders=["INBOX"], archive_existing_messages=True)
            ],
        )
        self.settings = Settings(str(self.root / "Archive"), accounts=[self.account], rules=[])
        self.credentials = MemoryCredentialStore()
        self.credentials.set(self.account.id, "secret")
        self.state = ArchiveState(self.root / "state.sqlite3")
        self.mailbox = FakeMailbox(
            [RemoteMessage("1", sample_mail(subject="Other")), RemoteMessage("2", sample_mail())]
        )
        self.events = []
        self.service = ArchiveService(
            self.credentials, self.state, self.events.append, self.mailbox
        )

    def test_known_unmatched_messages_are_not_downloaded_or_warned_again_after_restart(
        self,
    ) -> None:
        first = self.service.run_once(self.settings)[0]
        self.assertEqual(first.unmatched, 2)
        self.assertEqual(self.events[-1].level, EventLevel.WARNING)
        self.assertIn("2 without a matching rule in this check", self.events[-1].message)

        second = self.service.run_once(self.settings)[0]
        self.assertEqual((second.unmatched, second.skipped_unmatched), (0, 2))
        self.assertEqual(self.events[-1].level, EventLevel.SUCCESS)
        self.assertNotIn("without a matching rule", self.events[-1].message)

        restarted = ArchiveService(
            self.credentials,
            ArchiveState(self.state.database_path),
            self.events.append,
            self.mailbox,
        )
        third = restarted.run_once(self.settings)[0]
        self.assertEqual((third.unmatched, third.skipped_unmatched), (0, 2))
        self.assertEqual(len(self.mailbox.downloaded), 2)

        self.mailbox.messages.append(RemoteMessage("3", sample_mail(subject="New")))
        fourth = restarted.run_once(self.settings)[0]
        self.assertEqual((fourth.unmatched, fourth.skipped_unmatched), (1, 2))
        self.assertIn("1 without a matching rule", self.events[-1].message)
        self.assertEqual(len(self.mailbox.downloaded), 3)

    def test_changed_condition_retries_unmatched_messages_and_can_archive_them(self) -> None:
        self.settings.rules = [
            Rule("Other", "Other", [Condition(MailField.SUBJECT, value="missing")])
        ]
        self.assertEqual(self.service.run_once(self.settings)[0].unmatched, 2)
        # Serializing/reloading the same rules must preserve the saved check.
        self.settings.rules = [Rule.from_dict(rule.to_dict()) for rule in self.settings.rules]
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_unmatched, 2)
        self.settings.rules[0].conditions[0].value = "Other"
        changed = self.service.run_once(self.settings)[0]
        self.assertEqual((changed.archived, changed.unmatched), (1, 1))
        fingerprint = matching_rules_fingerprint(self.settings.rules, self.account.id)
        self.assertEqual(
            self.state.unmatched_message_ids(
                self.account.id, imap_namespace(self.account, "validity-1"), fingerprint
            ),
            {"2"},
        )
        next_run = self.service.run_once(self.settings)[0]
        self.assertEqual(
            (next_run.archived, next_run.unmatched, next_run.skipped_unmatched), (0, 0, 1)
        )
        self.assertEqual(next_run.already_processed, 1)
        self.assertEqual(len(self.mailbox.downloaded), 4)

    def test_other_accounts_disabled_rules_and_archive_options_do_not_trigger_redownloads(
        self,
    ) -> None:
        current = Rule("Current", "Inbox", [Condition(MailField.SUBJECT, value="missing")])
        disabled = Rule("Disabled", "Disabled", enabled=False)
        self.settings.rules = [current, disabled]
        self.assertEqual(self.service.run_once(self.settings)[0].unmatched, 2)

        self.settings.rules.append(Rule("Other account", "Other", account_ids=["other-account"]))
        current.name = "Renamed"
        current.destination = "New destination"
        current.save_mode = SaveMode.EMAIL_ONLY
        current.date_folder_position = DateFolderPosition.BEFORE_SUBFOLDER
        self.settings.rules.reverse()
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_unmatched, 2)
        self.assertEqual(len(self.mailbox.downloaded), 2)

        disabled.enabled = True
        self.assertEqual(self.service.run_once(self.settings)[0].archived, 2)
        self.assertEqual(len(self.mailbox.downloaded), 4)

    def test_failed_archiving_is_retried_instead_of_marked_unmatched(self) -> None:
        self.settings.rules = [Rule("All", "Inbox")]
        with patch("mailarchive.service.ArchiveStorage.archive", side_effect=OSError("disk full")):
            failed = self.service.run_once(self.settings)[0]
        self.assertEqual((failed.failed, failed.unmatched), (2, 0))
        retried = self.service.run_once(self.settings)[0]
        self.assertEqual((retried.archived, retried.skipped_unmatched), (2, 0))
        self.assertEqual(len(self.mailbox.downloaded), 4)

    def test_failed_unmatched_record_is_retried(self) -> None:
        with patch.object(
            self.state, "record_unmatched", side_effect=sqlite3.OperationalError("database locked")
        ):
            failed = self.service.run_once(self.settings)[0]
        self.assertEqual((failed.failed, failed.unmatched), (2, 0))
        self.assertEqual(self.service.run_once(self.settings)[0].unmatched, 2)
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_unmatched, 2)
        self.assertEqual(len(self.mailbox.downloaded), 4)

    def test_rules_edited_during_fetch_are_applied_on_the_next_run(self) -> None:
        fetch = self.mailbox.fetch_messages

        def edit_rules(account, password, should_fetch, *, sync=None):
            self.settings.rules = [Rule("All", "Inbox")]
            return fetch(account, password, should_fetch, sync=sync)

        with patch.object(self.mailbox, "fetch_messages", side_effect=edit_rules):
            first = self.service.run_once(self.settings)[0]
        self.assertEqual((first.archived, first.unmatched), (0, 2))
        second = self.service.run_once(self.settings)[0]
        self.assertEqual((second.archived, second.skipped_unmatched), (2, 0))


if __name__ == "__main__":
    unittest.main()
