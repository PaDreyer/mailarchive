"""A user stops one check across the real worker, persistence and archive files."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.cancellation import NO_CANCELLATION
from mailarchive.application.events import EventLevel, ExecutionState
from mailarchive.application.source_port import RemoteMessage
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import FakeSource, Registry, raw_mail


class ControlledSource(FakeSource):
    def __init__(self):
        super().__init__({})
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.phase = "scan"
        self.downloads = 0
        self.raw = raw_mail()
        self.messages["1"] = RemoteMessage(
            "1",
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=self.chunks,
            release=self.closed.set,
        )

    def block(self):
        self.entered.set()
        if not self.release.wait(THREAD_TIMEOUT):
            raise AssertionError("The controlled provider was not released")

    def chunks(self):
        self.downloads += 1
        yield self.raw[:20]
        if self.phase == "download":
            self.block()
        yield self.raw[20:]

    def fetch_messages(self, target, should_fetch, *, sync=None, cancellation=NO_CANCELLATION):
        scope, messages = super().fetch_messages(target, should_fetch, sync=sync)

        def iterate():
            try:
                if self.phase == "scan":
                    self.block()
                yield from messages
                if self.phase == "after_mail":
                    self.block()
            finally:
                messages.close()
                self.closed.set()

        return scope, iterate()


class CheckCancellationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mailboxes = [
            Mailbox(f"owner{number}@example.org", ["INBOX"], archive_existing_messages=True)
            for number in (1, 2)
        ]
        self.accounts = [
            Account(
                f"Mail {number}",
                "imap.example.org",
                mailbox.address,
                mailboxes=[mailbox],
                poll_minutes=number,
            )
            for number, mailbox in enumerate(self.mailboxes, 1)
        ]
        self.rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.store = ConfigStore(self.root / "profile")
        self.store.save(Settings(accounts=self.accounts, rules=[self.rule], start_at_login=False))
        self.source = ControlledSource()
        with (
            patch(
                "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(self.source)
            ),
            patch("mailarchive.bootstrap.set_start_at_login"),
        ):
            self.app = create_application(self.store, MemoryCredentialStore())
        self.coordinator = self.app._context.execution
        self.service = self.coordinator.service
        self.progress = []
        self.events = []
        self.finished = threading.Event()
        self.app.set_observers(self.events.append, self.receive_progress)
        self.addCleanup(self.app.close)
        self.addCleanup(self.source.release.set)

    def receive_progress(self, progress):
        self.progress.append(progress)
        if progress.origin == "check" and not progress.active:
            self.finished.set()

    def start_check(self):
        self.finished.clear()
        self.app.start()
        check_id = self.app.check_now()
        self.assertIsInstance(check_id, str)
        return check_id

    def stop_blocked(self, check_id):
        self.assertTrue(self.source.entered.wait(THREAD_TIMEOUT))
        self.assertTrue(self.app.stop_check(check_id))
        self.assertTrue(self.app.stop_check(check_id))
        self.assertEqual(self.progress[-1].state, ExecutionState.STOPPING)
        self.assertFalse(self.finished.is_set())
        self.assertIsNone(self.app.check_now())
        self.source.release.set()
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertTrue(self.coordinator.is_idle())
        self.assertEqual(self.progress[-1].state, ExecutionState.STOPPED)
        self.assertEqual(self.progress[-1].message, "Mail check stopped.")
        self.assertEqual(
            len([p for p in self.progress if p.execution_id == check_id and not p.active]), 1
        )
        self.assertFalse(any(e.level == EventLevel.ERROR for e in self.events))

    def test_stop_queued_check_never_contacts_provider_and_old_id_is_harmless(self):
        first = self.app.check_now()
        self.assertTrue(self.app.stop_check(first))
        self.assertEqual(
            [p.state for p in self.progress], [ExecutionState.QUEUED, ExecutionState.STOPPED]
        )
        self.assertEqual(self.source.folders_seen, [])
        second = self.app.check_now()
        self.assertNotEqual(first, second)
        self.assertFalse(self.app.stop_check(first))
        self.assertTrue(self.app.stop_check(second))
        self.assertEqual(self.source.downloads, 0)

    def test_stop_scan_prevents_all_later_mailboxes_and_allows_a_fresh_check(self):
        first = self.start_check()
        self.stop_blocked(first)
        self.assertEqual(self.source.folders_seen, ["INBOX"])
        self.assertEqual(self.source.downloads, 0)
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(self.app.activity_page().items, ())
        self.source.phase = "none"
        second = self.start_check()
        self.assertFalse(self.app.stop_check(first))
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(self.progress[-1].execution_id, second)
        self.assertEqual(self.progress[-1].state, ExecutionState.COMPLETED)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)
        self.assertFalse(self.app.stop_check(second))

    def test_stop_download_cleans_partial_bytes_and_retains_retryable_intake(self):
        self.source.phase = "download"
        check_id = self.start_check()
        self.stop_blocked(check_id)
        self.assertTrue(self.source.closed.is_set())
        self.assertEqual(self.app.status().spool_bytes, 0)
        self.assertEqual(len(self.app.current_jobs()), 1)
        self.assertFalse((self.root / "archive").exists())
        self.assertIsNone(self.service.discovery.scope(self.mailboxes[0].id, "INBOX"))
        intake = self.service.discovery.pending_automatic_intakes()[0]
        self.assertEqual(intake["status"], "reserved")
        self.assertEqual(self.service.operations.run_status(intake["run_id"]), "interrupted")
        self.assertIsNone(intake["error"])

    def test_stop_after_first_mail_keeps_files_and_does_not_check_next_mailbox(self):
        self.source.phase = "after_mail"
        check_id = self.start_check()
        self.stop_blocked(check_id)
        self.assertEqual(self.source.downloads, 1)
        self.assertEqual(self.source.folders_seen, ["INBOX"])
        self.assertEqual(len(self.app.activity_page().items), 1)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)

    def test_stop_during_publication_records_receipt_and_defers_remaining_outputs(self):
        self.source.phase = "none"
        rule = self.app.settings.rules[0]
        rule.targets.append(RuleTarget(str(self.root / "second-target")))
        self.app.save_rules([rule])
        writer = self.service.engine.output_files
        publish = writer.publish

        def blocked_publish(path, content):
            self.source.block()
            publish(path, content)

        with patch.object(writer, "publish", side_effect=blocked_publish):
            check_id = self.start_check()
            self.stop_blocked(check_id)
        self.assertEqual(self.source.downloads, 1)
        plan = self.service.delivery.open_plans()[0]
        outputs = self.service.delivery.outputs(plan["id"])
        self.assertEqual([o["status"] for o in outputs].count("done"), 1)
        self.assertEqual([o["status"] for o in outputs].count("pending"), 1)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
        self.assertFalse((self.root / "second-target").exists())
        self.assertGreater(self.app.status().spool_bytes, 0)
        self.assert_polling_deferred()
        self.source.messages.clear()  # The accepted mail must finish from its retained copy.
        next_due = self.coordinator._deferred_sources[self.mailboxes[0].id]
        with patch("mailarchive.application.execution.time.monotonic", return_value=next_due):
            self.coordinator._poll(False)
        self.assertEqual(len(list((self.root / "second-target").glob("*.eml"))), 1)
        self.assertEqual(self.service.delivery.open_plans(), [])
        self.assertEqual(self.app.status().spool_bytes, 0)

    def assert_polling_deferred(self):
        deadlines = self.coordinator._deferred_sources
        self.assertAlmostEqual(
            deadlines[self.mailboxes[1].id] - deadlines[self.mailboxes[0].id], 60
        )
        calls = self.source.fetch_count
        with (
            patch(
                "mailarchive.application.execution.time.monotonic",
                return_value=min(deadlines.values()) - 1,
            ),
            patch.object(self.service, "run_once", wraps=self.service.run_once) as run,
        ):
            self.coordinator._poll(False)
            run.assert_not_called()
        self.assertEqual(self.source.fetch_count, calls)

    def test_saved_intake_waits_for_interval_even_after_settings_save(self):
        self.source.phase = "download"
        check_id = self.start_check()
        self.stop_blocked(check_id)
        self.app.save_settings(self.app.settings)
        self.assert_polling_deferred()
        self.source.phase = "none"
        next_due = self.coordinator._deferred_sources[self.mailboxes[0].id]
        with patch("mailarchive.application.execution.time.monotonic", return_value=next_due):
            self.coordinator._poll(False)
        self.assertEqual(self.source.downloads, 2)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
        self.assertEqual(self.app.current_jobs(), ())

    def test_stop_after_completion_does_not_change_completion_or_future_check(self):
        self.source.phase = "none"
        first = self.start_check()
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertFalse(self.app.stop_check(first))
        self.assertEqual(self.progress[-1].state, ExecutionState.COMPLETED)
        second = self.start_check()
        self.assertFalse(self.app.stop_check(first))
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT))
        self.assertEqual(self.progress[-1].execution_id, second)
        self.assertEqual(self.progress[-1].state, ExecutionState.COMPLETED)

    def test_retained_work_uses_saved_source_interval_after_account_removal(self):
        self.source.phase = "download"
        check_id = self.start_check()
        self.stop_blocked(check_id)
        self.app.delete_account(self.accounts[0].id)
        self.app.delete_account(self.accounts[1].id)
        settings = self.app.settings
        settings.default_poll_minutes = 17
        self.app.save_settings(settings)
        intervals = self.service.automatic_source_intervals(self.app.settings)
        self.assertEqual(intervals, {self.mailboxes[0].id: (self.accounts[0].id, 60)})
        self.assert_polling_deferred()
        self.source.phase = "none"
        self.assertIsNone(self.app.check_now())
        next_due = self.coordinator._deferred_sources[self.mailboxes[0].id]
        with patch("mailarchive.application.execution.time.monotonic", return_value=next_due):
            self.coordinator._poll(False)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
