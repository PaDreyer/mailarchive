"""User commands run through real persistence, execution, files and activity queries."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import update_credential_data
from mailarchive.application.events import EventLevel
from mailarchive.application.execution import NO_RULES_NOTICE
from mailarchive.application.source_port import RemoteMessage
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.oauth import MICROSOFT_MAIL_READ_SCOPE
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.registry import MessageSourceRegistry
from tests.concurrency import THREAD_TIMEOUT
from tests.oauth_fixture import microsoft_cache
from tests.test_mail_sources import FakeOAuth
from tests.test_restart_core import FakeSource, Registry, raw_mail
from tests.test_synchronization import ScriptedHttp


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

    def search_messages(self, target, should_fetch, start, end, *, range_sync, cancellation=None):
        scope, messages = self.fetch_messages(target, should_fetch)
        range_sync.start(scope.processing_namespace)

        def iterate():
            self.entered.set()
            if not self.release.wait(THREAD_TIMEOUT):
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
        self.finished = threading.Event()
        self.progress = []
        self.app.set_observers(lambda event: None, self.receive_progress)
        self.app.start()

    def receive_progress(self, progress):
        self.progress.append(progress)
        if not progress.active:
            self.finished.set()

    def wait_for_finished(self):
        self.assertTrue(self.finished.wait(THREAD_TIMEOUT), "The operation did not finish")

    def test_rule_edit_during_manual_work_does_not_change_saved_selection_or_outputs(self):
        operation_id = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
        key = "operation:" + operation_id
        self.assertTrue(self.source.entered.wait(THREAD_TIMEOUT))
        self.assertEqual([item.key for item in self.app.current_jobs()], [key])
        changed = self.app.settings.rules[0]
        changed.name = "Changed rule"
        changed.targets[0].path = str(self.root / "changed")
        self.app.save_rules([changed])
        self.source.release.set()

        self.wait_for_finished()
        detail = self.app.activity_detail(key)
        self.assertEqual(detail.item.status, "completed")
        self.assertEqual(detail.item.rule_name, "Original rule")
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(len(list((self.root / "original").glob("*.eml"))), 1)
        self.assertFalse((self.root / "changed").exists())
        self.assertEqual(self.app.settings.rules[0].name, "Changed rule")

    def test_stop_via_facade_prevents_download_and_records_stopped_selection(self):
        operation_id = self.app.apply_rule_to_past_mail(self.rule.id, None, None, "UTC")
        self.assertTrue(self.source.entered.wait(THREAD_TIMEOUT))
        self.app.stop_operation(operation_id)
        self.source.release.set()

        self.wait_for_finished()
        detail = self.app.activity_detail("operation:" + operation_id)
        self.assertEqual(detail.item.status, "stopped")
        self.assertEqual(self.app.current_jobs(), ())
        self.assertFalse((self.root / "original").exists())

    def test_checks_update_health_and_only_new_mail_creates_archive_activity(self):
        def check():
            self.finished.clear()
            self.assertTrue(self.app.check_now())
            self.wait_for_finished()

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

    def test_mailbox_check_without_rules_returns_notice_without_starting(self):
        self.app.save_rules([])
        events, progress = [], []
        self.app.set_observers(events.append, progress.append)
        for _ in range(2):
            self.assertIsNone(self.app.check_now())
        self.assertEqual([event.message for event in events], [NO_RULES_NOTICE] * 2)
        self.assertTrue(all(event.level == EventLevel.INFO for event in events))
        self.assertEqual(progress, [])
        self.assertTrue(self.app._context.execution.is_idle())
        self.assertEqual(self.app.monitoring_status(self.mailbox.id).status, "setting_up")
        self.assertEqual(self.source.fetch_count, 0)
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(self.app.activity_page().items, ())
        self.assertFalse((self.root / "original").exists())

    def test_microsoft_check_now_accepts_canonical_delta_link_and_checks_again(self):
        account = self.app.settings.accounts[0]
        account.label = "gmail"
        account.provider = MailProvider.MICROSOFT_GRAPH
        account.auth_mode = AuthMode.OAUTH_USER
        account.client_id = "client"
        saved = self.app.save_account(AccountSubmission(account, {}, True), replacing_id=account.id)
        self.assertTrue(self.app._background.wait(THREAD_TIMEOUT))
        update_credential_data(
            self.app._credentials,
            account.id,
            msal_cache=microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE]),
        )
        self.app.refresh_account_authorization(account.id)
        self.assertTrue(self.app._background.wait(THREAD_TIMEOUT))
        mailbox = saved.accounts[0].mailboxes[0]
        cursor = (
            "https://graph.microsoft.com/v1.0/me/mailfolders('resolved-folder')/messages/delta"
            "?$deltatoken=opaque%2Btoken"
        )
        folder_lookup = ("json", "/mailFolders/INBOX?$select=id", {"id": "resolved-folder"})
        http = ScriptedHttp(
            [
                ("json", "/messages/delta?", {"value": [], "@odata.deltaLink": cursor}),
                folder_lookup,
                folder_lookup,
                ("json", cursor, {"value": [], "@odata.deltaLink": cursor}),
            ]
        )
        registry = MessageSourceRegistry(MemoryCredentialStore(), http=http)
        registry.sources[MailProvider.MICROSOFT_GRAPH].oauth = FakeOAuth()
        self.app._context.execution.service.source_registry = registry
        for _ in range(2):
            self.progress.clear()
            self.finished.clear()
            self.assertTrue(self.app.check_now())
            self.wait_for_finished()
            self.assertEqual(self.progress[-1].message, "Mail check finished.")
            self.assertEqual(self.app.monitoring_status(mailbox.id).status, "active")

        self.assertEqual(http.steps, [])
        self.assertEqual(http.calls[-1][1], cursor)
        self.assertFalse(
            any(event.level == EventLevel.ERROR for event in self.app.activity_log_page().events)
        )
        self.assertEqual(self.app.activity_page().items, ())
