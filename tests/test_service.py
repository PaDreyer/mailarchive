"""Service behavior around account selection, frozen rules, and scan failures."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.imap_client import RemoteMessage
from mailarchive.mail_identity import MessageScope
from mailarchive.mail_sources import ScanWideProviderError
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
from mailarchive.service import ArchiveRunBusyError, ArchiveService, RunProgress
from mailarchive.workspace import WorkspaceStore
from tests.test_restart_core import FakeSource, Registry, raw_mail


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.received = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
        self.mailbox = Mailbox("one@example.org", ["INBOX"])
        self.account = Account(
            "One", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        self.rule = Rule("All", targets=[RuleTarget(str(self.root / "A"))])
        self.settings = Settings("", accounts=[self.account], rules=[self.rule])
        self.source = FakeSource(
            {"1": RemoteMessage("1", raw_mail(), self.received, "imap_internaldate")}
        )
        self.progress = []
        self.service = ArchiveService(
            None,
            WorkspaceStore(self.root / "workspace.sqlite3"),
            source_registry=Registry(self.source),
            progress_handler=self.progress.append,
        )

    def test_account_change_excludes_runs_and_releases_lock(self):
        with self.service.account_change():
            with self.assertRaises(ArchiveRunBusyError):
                self.service.run_once(self.settings)
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

    def test_no_accounts_finishes_progress_once(self):
        self.settings.accounts = []
        self.assertEqual(self.service.run_once(self.settings), [])
        self.assertEqual(len(self.progress), 2)
        self.assertIsInstance(self.progress[0], RunProgress)
        self.assertFalse(self.progress[-1].active)

    def test_account_filter_runs_only_requested_owner(self):
        other_mailbox = Mailbox("two@example.org", ["INBOX"])
        other = Account("Two", "imap.example.org", other_mailbox.address, mailboxes=[other_mailbox])
        self.settings.accounts.append(other)
        results = self.service.run_once(self.settings, {other.id})
        self.assertEqual([result.account_id for result in results], [other.id])
        self.assertIsNone(self.service.state.scope(self.mailbox.id, "INBOX"))
        self.assertIsNotNone(self.service.state.scope(other_mailbox.id, "INBOX"))

    def test_unmatched_automatic_mail_waits_for_explicit_range_after_rule_change(self):
        self.settings.rules = []
        self.service.run_once(self.settings)  # baseline
        self.source.messages["2"] = RemoteMessage(
            "2", raw_mail(), self.received, "imap_internaldate"
        )
        self.assertEqual(self.service.run_once(self.settings)[0].unmatched, 1)
        self.settings.rules = [self.rule]
        self.assertEqual(self.service.run_once(self.settings)[0].archived, 0)
        self.assertEqual(self.service.run_range(self.settings, {self.mailbox.id})[0].archived, 2)

    def test_rules_are_frozen_before_provider_scan(self):
        original = self.source.fetch_messages
        changed = Rule("Changed", targets=[RuleTarget(str(self.root / "Wrong"))])

        def mutate_then_fetch(*args, **kwargs):
            self.settings.rules = [changed]
            return original(*args, **kwargs)

        self.source.fetch_messages = mutate_then_fetch
        self.assertEqual(self.service.run_range(self.settings, {self.mailbox.id})[0].archived, 1)
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.assertFalse((self.root / "Wrong").exists())

    def test_failed_folder_does_not_block_another_selected_folder(self):
        self.mailbox.folders = ["Broken", "Healthy"]
        original = self.source.fetch_messages

        def one_fails(target, *args, **kwargs):
            if target.folder == "Broken":
                raise OSError("folder offline")
            return original(target, *args, **kwargs)

        self.source.fetch_messages = one_fails
        result = self.service.run_range(self.settings, {self.mailbox.id})[0]
        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)

    def test_scan_wide_failure_stops_before_the_next_graph_folder(self):
        self.account.provider = MailProvider.MICROSOFT_GRAPH
        self.account.auth_mode = AuthMode.OAUTH_USER
        self.account.client_id = "client"
        self.mailbox.folders = ["one", "two"]
        calls = []

        def scan_folder(target, should_fetch, *, sync=None):
            calls.append(target.folder)
            scope = MessageScope("microsoft_graph-mailbox:one@example.org", target.folder)

            def messages():
                if should_fetch(scope, target.folder):
                    if target.folder == "one":
                        yield RemoteMessage(
                            "one",
                            received_at=self.received,
                            received_origin="graph_received_date_time",
                            raw_chunks=lambda: (_ for _ in ()).throw(
                                ScanWideProviderError("HTTP 429: rate limited")
                            ),
                        )
                    else:
                        yield RemoteMessage(
                            "two", raw_mail(), self.received, "graph_received_date_time"
                        )

            return scope, messages()

        self.source.fetch_messages = scan_folder

        result = self.service.run_range(self.settings, {self.mailbox.id})[0]

        self.assertEqual(calls, ["one"])
        self.assertEqual((result.archived, result.failed), (0, 1))

    def test_attachments_only_without_attachment_writes_nothing(self):
        self.rule.targets = [RuleTarget(str(self.root / "A"), SaveMode.ATTACHMENTS_ONLY)]
        result = self.service.run_range(self.settings, {self.mailbox.id})[0]
        self.assertEqual((result.archived, result.skipped_no_attachments), (0, 1))
        self.assertFalse((self.root / "A").exists())
        self.assertEqual(self.service.state.open_plans(), [])

    def test_receipt_satisfied_range_is_counted_as_already_processed(self):
        self.assertEqual(self.service.run_range(self.settings, {self.mailbox.id})[0].archived, 1)

        repeated = self.service.run_range(self.settings, {self.mailbox.id})[0]

        self.assertEqual(
            (repeated.archived, repeated.already_processed, repeated.skipped_no_attachments),
            (0, 1, 0),
        )

    def test_scan_wide_lazy_body_failure_stops_before_later_ids_and_cursor(self):
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def throttled():
            raise ScanWideProviderError("The mail provider returned HTTP 429: rate limited")
            yield b""

        self.source.messages.update(
            {
                "2": RemoteMessage(
                    "2",
                    received_at=self.received,
                    received_origin="imap_internaldate",
                    raw_chunks=throttled,
                ),
                "3": RemoteMessage("3", raw_mail(), self.received, "imap_internaldate"),
            }
        )

        result = self.service.run_once(self.settings)[0]

        self.assertEqual((result.archived, result.failed), (0, 1))
        self.assertEqual(self.service.state.scope(self.mailbox.id, "INBOX")["cursor"], "1")
        self.assertEqual(self.source.fetch_count, 1)
        error = self.service.state.intake_errors()[0]
        self.assertEqual(error["remote_id"], "2")
        self.assertIn("HTTP 429", error["error"])

    def test_intake_failure_emits_its_concrete_cause(self):
        events = []
        self.service.event_handler = events.append
        self.source.messages["1"] = RemoteMessage("1", raw_mail(), None, "")
        result = self.service.run_range(self.settings, {self.mailbox.id})[0]
        self.assertEqual(result.failed, 1)
        self.assertTrue(any("valid reception time" in event.message for event in events))

    def test_destination_failure_emits_destination_and_cause(self):
        events = []
        self.service.event_handler = events.append
        obstruction = self.root / "offline"
        obstruction.write_text("not a directory")
        target = obstruction / "archive"
        self.rule.targets.append(RuleTarget(str(target)))
        result = self.service.run_range(self.settings, {self.mailbox.id})[0]
        self.assertEqual(result.failed, 1)
        plan = self.service.state.open_plans()[0]
        target_error = next(
            row["error"]
            for row in self.service.state.plan_targets(plan["id"])
            if row["path"] == str(target)
        )
        self.assertTrue(target_error)
        self.assertTrue(
            any(str(target) in event.message and target_error in event.message for event in events)
        )

    def test_reserved_provider_message_that_is_not_returned_stays_visible(self):
        events = []
        self.service.event_handler = events.append

        def reserve_without_download(target, should_fetch, *, sync=None):
            namespace = self.source._namespace_for(target)
            scope = MessageScope(namespace, namespace)
            should_fetch(scope, "1")

            def empty():
                if False:
                    yield None

            return scope, empty()

        self.source.fetch_messages = reserve_without_download
        result = self.service.run_range(self.settings, {self.mailbox.id})[0]
        self.assertEqual(result.failed, 1)
        self.assertIn("did not return", self.service.state.intake_errors()[0]["error"])
        self.assertTrue(any("not returned" in event.message for event in events))

    def test_range_exactly_checks_provider_reception_time(self):
        self.source.messages["2"] = RemoteMessage(
            "2", raw_mail(), self.received + timedelta(hours=1), "imap_internaldate"
        )
        end = self.received + timedelta(hours=1)
        result = self.service.run_range(
            self.settings, {self.mailbox.id}, start=self.received, end=end
        )[0]
        self.assertEqual(result.archived, 1)
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        filtered = next(
            item
            for item in self.service.state.processing_history()
            if item["item_type"] == "intake" and item["status"] == "filtered"
        )
        self.assertEqual(filtered["received_at"], end.isoformat())
        self.assertEqual(filtered["received_origin"], "imap_internaldate")

    def test_cancellation_between_status_check_and_reservation_cannot_leave_intake(self):
        original = self.service.state.run_status
        checks = 0

        def cancel_after_stale_read(run_id):
            nonlocal checks
            status = original(run_id)
            if status == "running":
                checks += 1
                if checks == 2:
                    self.service.state.cancel_run(run_id)
            return status

        with patch.object(self.service.state, "run_status", side_effect=cancel_after_stale_read):
            result = self.service.run_range(self.settings, {self.mailbox.id})[0]

        self.assertEqual((result.archived, result.failed), (0, 0))
        with self.service.state.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM scan_run").fetchone()[0], "cancelled")
            self.assertEqual(db.execute("SELECT count(*) FROM intake").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

    def test_account_scoped_rules_choose_different_destinations(self):
        second_mailbox = Mailbox("two@example.org", ["INBOX"])
        second = Account(
            "Two", "imap.example.org", second_mailbox.address, mailboxes=[second_mailbox]
        )
        self.settings.accounts.append(second)
        self.rule.account_ids = [self.account.id]
        self.settings.rules.append(
            Rule("Second", account_ids=[second.id], targets=[RuleTarget(str(self.root / "B"))])
        )
        results = self.service.run_range(self.settings, {self.mailbox.id, second_mailbox.id})
        self.assertEqual([item.archived for item in results], [1, 1])
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.assertEqual(len(list((self.root / "B").glob("*.eml"))), 1)


if __name__ == "__main__":
    unittest.main()
