"""Profile reopen validates frozen manual selections and preserves crash chronology."""

import json
import tempfile
import unittest
from pathlib import Path

from mailarchive.application.errors import WorkspaceError
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.profile_database import ProfileDatabase


class ProfileIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "profile.sqlite3"
        self.profile = ProfileDatabase(self.path)
        self.mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = Account(
            "Mail", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        rule = Rule("Archive", targets=[RuleTarget(str(self.path.parent / "Archive"))])
        settings = Settings(accounts=[account], rules=[rule])
        self.selection = {
            "source_ids": [self.mailbox.id],
            "timezone": "UTC",
            "start_utc": None,
            "end_utc": None,
        }
        self.operation = self.profile.operations.create_manual_operation(
            settings, [self.mailbox.id], rule.id, self.selection
        )

    def test_reopen_recovers_then_reopens_with_same_finished_attempt(self):
        self.profile.operations.claim_manual_operation(self.operation)
        self.profile.operations.mark_operation_source(self.operation, self.mailbox.id, "running")
        recovered = ProfileDatabase(self.path, recover=True)
        with recovered.connection() as db:
            first = dict(db.execute("SELECT * FROM manual_operation_attempt").fetchone())
        self.assertEqual(first["status"], "interrupted")
        self.assertIsNotNone(first["finished_at"])
        self.assertEqual(json.loads(first["sources_json"])[0]["status"], "failed")
        reopened = ProfileDatabase(self.path, recover=True)
        with reopened.connection() as db:
            self.assertEqual(
                dict(db.execute("SELECT * FROM manual_operation_attempt").fetchone()), first
            )
        self.assertEqual(
            reopened.operations.manual_operation(self.operation)["status"], "interrupted"
        )

    def test_reopen_rejects_selection_detached_from_its_source_rows(self):
        self.selection["source_ids"] = []
        with self.profile.connection() as db, db:
            db.execute(
                "UPDATE manual_operation SET selection_json=?", (json.dumps(self.selection),)
            )
        with self.assertRaisesRegex(WorkspaceError, "operation snapshot"):
            ProfileDatabase(self.path, recover=True)

    def test_reopen_rejects_finished_attempt_with_foreign_source(self):
        self.profile.operations.claim_manual_operation(self.operation)
        self.profile.operations.finish_manual_operation(self.operation, "Network failure")
        with self.profile.connection() as db, db:
            snapshot = json.loads(
                db.execute("SELECT sources_json FROM manual_operation_attempt").fetchone()[0]
            )
            snapshot[0]["source_id"] = "other-mailbox"
            db.execute(
                "UPDATE manual_operation_attempt SET sources_json=?", (json.dumps(snapshot),)
            )
        with self.assertRaisesRegex(WorkspaceError, "operation snapshot"):
            ProfileDatabase(self.path, recover=True)
