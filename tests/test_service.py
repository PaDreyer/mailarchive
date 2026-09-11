import tempfile
import unittest
from pathlib import Path

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import RemoteMessage
from mailarchive.models import Account, Settings
from mailarchive.service import ArchiveService, EventLevel
from mailarchive.storage import ArchiveState
from tests.helpers import sample_mail


class FakeMailbox:
    def __init__(self, messages: list[RemoteMessage]) -> None:
        self.messages = messages

    def fetch_messages(self, account: Account, password: str, should_fetch=None):
        messages = (
            message
            for message in self.messages
            if should_fetch is None or should_fetch("validity-1", message.id)
        )
        return "validity-1", messages


class FailingMailbox:
    def fetch_messages(self, account: Account, password: str, should_fetch=None):
        raise RuntimeError("mailbox unavailable")


class ServiceTests(unittest.TestCase):
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
            account = Account("Personal", "imap.example.org", "me@example.org")
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
            account = Account("Personal", "imap.example.org", "me@example.org")
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
            account = Account("Personal", "imap.example.org", "me@example.org")
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

    def test_no_active_accounts_emits_information_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = []
            service = ArchiveService(
                MemoryCredentialStore(),
                ArchiveState(Path(temporary) / "state.sqlite3"),
                events.append,
            )
            settings = Settings.defaults()
            settings.accounts = [Account("Disabled", enabled=False)]

            self.assertEqual(service.run_once(settings), [])
            self.assertEqual(events[-1].level, EventLevel.INFO)
            self.assertIn("No active", events[-1].message)

    def test_account_filter_runs_only_selected_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Account("Selected", "imap.example.org", "selected@example.org")
            ignored = Account("Ignored", "imap.example.org", "ignored@example.org")
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
                self.assertEqual(service.run_once(Settings.defaults()), [])
                self.assertEqual(events[-1].level, EventLevel.WARNING)
                with self.assertRaisesRegex(RuntimeError, "cannot be changed"):
                    service.relocate_state_database(Path(temporary) / "other.sqlite3")
            finally:
                service._run_lock.release()

    def test_mailbox_failure_becomes_account_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Account("Personal", "imap.example.org", "me@example.org")
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


if __name__ == "__main__":
    unittest.main()
