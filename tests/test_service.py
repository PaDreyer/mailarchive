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


if __name__ == "__main__":
    unittest.main()
