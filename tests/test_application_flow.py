"""User commands run through real persistence, execution, files and activity queries."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.source_port import RemoteMessage
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from tests.test_restart_core import FakeSource, Registry, raw_mail


class BlockingSource(FakeSource):
    def __init__(self):
        super().__init__(
            {
                "1": RemoteMessage(
                    "1", raw_mail(), datetime(2026, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
                )
            }
        )
        self.entered = threading.Event()
        self.release = threading.Event()

    def search_messages(self, target, should_fetch, start, end, *, range_sync):
        scope, messages = self.fetch_messages(target, should_fetch)
        range_sync.start(scope.processing_namespace)

        def iterate():
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("Test provider was not released")
            yield from messages
            range_sync.finish()

        return scope, iterate()


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConfigStore(self.root / "profile")
        self.mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = Account(
            "Mail", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        self.rule = Rule("Original rule", targets=[RuleTarget(str(self.root / "original"))])
        self.store.save(Settings(accounts=[account], rules=[self.rule], start_at_login=False))
        self.source = BlockingSource()

        with (
            patch(
                "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(self.source)
            ),
            patch("mailarchive.bootstrap.set_start_at_login"),
        ):
            self.app = create_application(self.store, MemoryCredentialStore())
        self.addCleanup(self.app.close)
        self.addCleanup(self.source.release.set)
        self.app.start()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.01)
        self.fail("The application did not reach the expected activity state")

    def test_rule_edit_during_manual_work_does_not_change_saved_selection_or_outputs(self):
        operation_id = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
        key = "operation:" + operation_id
        self.assertTrue(self.source.entered.wait(2))
        self.assertEqual([item.key for item in self.app.current_jobs()], [key])
        changed = self.app.settings.rules[0]
        changed.name = "Changed rule"
        changed.targets[0].path = str(self.root / "changed")
        self.app.save_rules([changed])
        self.source.release.set()

        self.wait_for(lambda: self.app.activity_page().items)
        detail = self.app.activity_detail(key)
        self.assertEqual(detail.item.status, "completed")
        self.assertEqual(detail.item.rule_name, "Original rule")
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(len(list((self.root / "original").glob("*.eml"))), 1)
        self.assertFalse((self.root / "changed").exists())
        self.assertEqual(self.app.settings.rules[0].name, "Changed rule")

    def test_stop_via_facade_prevents_download_and_records_stopped_selection(self):
        operation_id = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
        self.assertTrue(self.source.entered.wait(2))
        self.app.stop_operation(operation_id)
        self.source.release.set()

        self.wait_for(lambda: self.app.activity_page().items)
        detail = self.app.activity_detail("operation:" + operation_id)
        self.assertEqual(detail.item.status, "stopped")
        self.assertEqual(self.app.current_jobs(), ())
        self.assertFalse((self.root / "original").exists())

    def test_checks_update_health_and_only_new_mail_creates_archive_activity(self):
        idle = threading.Event()
        self.app.set_observers(
            lambda event: None,
            lambda progress: idle.set() if not progress.active else None,
        )

        def check():
            idle.clear()
            self.assertTrue(self.app.check_now())
            self.assertTrue(idle.wait(3))
            self.wait_for(lambda: self.app._context.execution.is_idle())

        check()  # First check records the existing mailbox baseline.
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(self.app.activity_page().items, ())
        self.assertEqual(self.app.monitoring_status(self.mailbox.id).status, "active")
        self.source.messages["2"] = RemoteMessage(
            "2", raw_mail(), datetime(2020, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
        )
        check()  # A newly discovered mail can have an old reception timestamp.
        page = self.app.activity_page()
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].kind, "mail")
        self.assertEqual(page.items[0].completed_outputs, 1)
        self.assertEqual(self.app.status().spool_bytes, 0)
        first_key = page.items[0].key
        check()  # Empty successful monitoring adds no visible job.
        self.assertEqual([item.key for item in self.app.activity_page().items], [first_key])
        self.assertEqual(len(list((self.root / "original").glob("*.eml"))), 1)

    def test_mailbox_check_without_rules_finishes_with_configuration_notice(self):
        self.app.save_rules([])
        progress = []
        self.app.set_observers(lambda event: None, progress.append)
        self.assertTrue(self.app.check_now())
        self.wait_for(
            lambda: progress and not progress[-1].active and self.app._context.execution.is_idle()
        )
        self.assertEqual(
            progress[-1].message, "Mail check finished. No enabled rules are configured."
        )
        self.assertEqual(self.app.monitoring_status(self.mailbox.id).status, "active")
        self.assertEqual(self.app.activity_page().items, ())
        self.assertFalse((self.root / "original").exists())
