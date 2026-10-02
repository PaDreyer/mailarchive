"""Configuration revisions and source bindings share one SQLite transaction."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from mailarchive.application.errors import WorkspaceError
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.configuration_repository import ConfigurationRepository
from mailarchive.infrastructure.persistence_time import now
from tests.workspace_fixture import WorkspaceStore


class ConfigurationRepositoryTests(unittest.TestCase):
    def test_save_binds_source_and_stale_run_snapshot_keeps_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = WorkspaceStore(root / "workspace.sqlite3")
            repository = ConfigurationRepository(
                state.connection, error_type=WorkspaceError, now=now
            )
            self.assertEqual(repository.load_settings(), Settings.defaults())
            mailbox = Mailbox("owner@example.org", folders=["INBOX"])
            account = Account("Mail", "imap.example.org", mailbox.address, mailboxes=[mailbox])
            first = Settings(
                accounts=[account],
                rules=[Rule("Archive", targets=[RuleTarget(str(root / "archive"))])],
            )
            first_revision = repository.save_settings(first)
            self.assertEqual(repository.load_settings().accounts, first.accounts)
            with state.connection() as db:
                source = db.execute("SELECT * FROM source WHERE id=?", (mailbox.id,)).fetchone()
                self.assertEqual(source["account_id"], account.id)
                self.assertEqual(source["enabled"], 1)

            stale = deepcopy(first)
            current = deepcopy(first)
            current.default_poll_minutes = 10
            current_revision = repository.save_settings(current)
            self.assertNotEqual(first_revision, current_revision)
            self.assertEqual(repository.prepare_run_settings(stale), first_revision)
            self.assertEqual(repository.configuration_revision(), current_revision)
            self.assertEqual(repository.load_settings().default_poll_minutes, 10)


if __name__ == "__main__":
    unittest.main()
