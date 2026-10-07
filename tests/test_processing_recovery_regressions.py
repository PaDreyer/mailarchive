"""Initial local cancellation and scoped retained-message namespace recovery."""

import imaplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.cancellation import ProcessingStopped
from mailarchive.application.errors import RunNotActiveError
from mailarchive.application.events import EventLevel, ExecutionState
from mailarchive.application.source_port import MailboxError, RemoteMessageNamespaceChanged
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
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests import test_execution_outcomes as execution_outcomes
from tests.helpers import sample_mail
from tests.past_mail_fixture import provider_source
from tests.test_imap_client import FakeImapConnection
from tests.test_mail_sources import FakeOAuth
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service


class InitialPreparationCancellationTests(unittest.TestCase):
    def fixture(self):
        fixture = execution_outcomes.ExecutionOutcomeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def assert_retained_without_failure(self, fixture):
        service = fixture.app._context.execution.service
        with service.delivery.connection() as db:
            plan = db.execute("SELECT * FROM plan").fetchone()
            self.assertIsNone(plan["error"])
            self.assertTrue(Path(plan["raw_path"]).exists())
            self.assertEqual(db.execute("SELECT count(*) FROM output_attempt").fetchone()[0], 0)
            intake = db.execute("SELECT error, attempts FROM intake").fetchone()
            self.assertEqual(tuple(intake), (None, 0))
        self.assertFalse(any(event.level == EventLevel.ERROR for event in fixture.events))

    def stop_first_read(self, fixture, *, manual=False, shutdown=False):
        service = fixture.app._context.execution.service
        original = service.engine.spool.read
        reads = 0

        def read(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                if shutdown:
                    service.request_shutdown()
                elif manual:
                    plan = service.delivery.open_plans()[0]
                    operation = service.operations.run_operation_id(plan["run_id"])
                    fixture.app.stop_operation("operation:" + operation)
                else:
                    request = fixture.app._context.execution._check
                    self.assertTrue(fixture.app.stop_check(request.id))
                raise RuntimeError("The retained directory went offline during stop")
            return original(*args, **kwargs)

        return patch.object(service.engine.spool, "read", side_effect=read)

    def test_first_check_preparation_stop_preserves_retryable_local_work(self):
        fixture = self.fixture()
        with self.stop_first_read(fixture):
            self.assertEqual(fixture.check().state, ExecutionState.STOPPED)
        self.assert_retained_without_failure(fixture)
        key = fixture.app.current_jobs()[0].key
        downloads = fixture.source.fetch_count
        self.assertEqual(fixture.retry(key).state, ExecutionState.COMPLETED)
        self.assertEqual(fixture.source.fetch_count, downloads)

    def test_first_past_mail_preparation_stop_preserves_accepted_raw(self):
        fixture = self.fixture()
        start = len(fixture.progress)
        with self.stop_first_read(fixture, manual=True):
            operation = fixture.app.apply_rule_to_past_mail(fixture.rule.id, None, None, "UTC")
            self.assertEqual(fixture.terminal(start, "operation").state, ExecutionState.STOPPED)
        self.assert_retained_without_failure(fixture)
        detail = fixture.app.activity_detail("operation:" + operation)
        self.assertEqual(detail.item.status, "stopped")
        self.assertEqual(detail.mail[0].status, "paused")

    def test_first_automatic_preparation_shutdown_preserves_accepted_raw(self):
        fixture = self.fixture()
        service = fixture.app._context.execution.service
        with self.stop_first_read(fixture, shutdown=True), self.assertRaises(ProcessingStopped):
            service.run_once(fixture.app.settings)
        self.assert_retained_without_failure(fixture)

    def test_inactive_first_plan_does_not_create_a_preparation_error(self):
        fixture = self.fixture()
        service = fixture.app._context.execution.service
        with patch.object(service.engine, "execute", side_effect=RunNotActiveError("Run inactive")):
            self.assertEqual(fixture.check().state, ExecutionState.FAILED)
        with service.delivery.connection() as db:
            self.assertIsNone(db.execute("SELECT error FROM plan").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM output_attempt").fetchone()[0], 0)

    def test_stop_dominates_inactive_first_plan(self):
        fixture = self.fixture()
        service = fixture.app._context.execution.service

        def inactive(*_args, **_kwargs):
            request = fixture.app._context.execution._check
            self.assertTrue(fixture.app.stop_check(request.id))
            raise RunNotActiveError("Run inactive during stop")

        with patch.object(service.engine, "execute", side_effect=inactive):
            self.assertEqual(fixture.check().state, ExecutionState.STOPPED)
        self.assert_retained_without_failure(fixture)


class RetainedNamespaceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mailbox = Mailbox("owner@example.org", ["INBOX"], archive_existing_messages=True)
        self.account = Account(
            "Owner", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        self.rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.source, self.server = provider_source(
            MailProvider.GENERIC_IMAP, self.account, {"1": sample_mail()}
        )
        store = ConfigStore(self.root / "profile")
        store.save(
            Settings(
                accounts=[self.account],
                rules=[self.rule],
                automatic_monitoring_paused=True,
                start_at_login=False,
            )
        )
        with (
            patch(
                "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(self.source)
            ),
            patch("mailarchive.bootstrap.set_start_at_login"),
        ):
            self.app = create_application(store, MemoryCredentialStore())
        self.addCleanup(self.app.close)
        self.service = self.app._context.execution.service
        self.assertEqual(self.service.run_once(self.app.settings)[0].failed, 0)
        self.server.messages[b"2"] = sample_mail()
        with patch.object(
            self.service.engine.spool, "stage", side_effect=OSError("Temporary disk failure")
        ):
            self.assertEqual(self.service.run_once(self.app.settings)[0].failed, 1)
        self.intake_id = self.service.discovery.pending_automatic_intakes()[0]["id"]
        self.app.save_rules([])
        self.server.response = lambda name: (name, [b"9002"])

    def assert_paused_without_repeated_attempts(self):
        result = self.service.run_once(self.app.settings, force_retry=True)[0]
        self.assertEqual(result.failed, 1)
        paused = self.app.paused_scopes(self.account.id)
        self.assertEqual(
            [(item.source_id, item.scope_key) for item in paused], [(self.mailbox.id, "INBOX")]
        )
        self.assertIn("UIDVALIDITY", paused[0].error)
        intake = dict(self.service.discovery.pending_automatic_intakes()[0])
        self.assertEqual(intake["id"], self.intake_id)
        self.assertFalse(self.service.has_automatic_work(self.app.settings))
        calls = len(self.server.calls)
        self.assertEqual(self.service.run_once(self.app.settings, force_retry=True), [])
        self.assertEqual(len(self.server.calls), calls)
        self.assertEqual(dict(self.service.discovery.pending_automatic_intakes()[0]), intake)
        return paused[0]

    def test_changed_namespace_pauses_saved_scope_without_a_current_rule(self):
        paused = self.assert_paused_without_repeated_attempts()
        with self.service.delivery.connection() as db:
            receipts = [tuple(row) for row in db.execute("SELECT * FROM receipt")]
            healthy_outputs = [
                tuple(row) for row in db.execute("SELECT * FROM output WHERE status='done'")
            ]
        self.assertEqual(self.app.reset_scope_baseline(paused.source_id, paused.scope_key), 1)
        self.app.save_rules([self.rule])
        ProfileDatabase(self.app.database_path, recover=True)
        result = self.service.run_once(self.app.settings)[0]
        self.assertEqual((result.archived, result.skipped_existing, result.failed), (0, 2, 0))
        self.assertEqual(self.app.paused_scopes(self.account.id), ())
        self.assertEqual(self.service.discovery.pending_automatic_intakes(), [])
        with self.service.delivery.connection() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM receipt")], receipts)
            self.assertEqual(
                [tuple(row) for row in db.execute("SELECT * FROM output WHERE status='done'")],
                healthy_outputs,
            )
        self.server.messages[b"3"] = sample_mail()
        self.assertEqual(self.service.run_once(self.app.settings)[0].archived, 1)

    def test_removed_scope_retains_a_reset_marker_for_the_saved_namespace(self):
        settings = self.app.settings
        settings.accounts[0].mailboxes[0].folders = ["Other"]
        self.app.save_account(AccountSubmission(settings.accounts[0], {}, False), self.account.id)
        self.assertIsNone(self.service.discovery.scope(self.mailbox.id, "INBOX"))
        self.assert_paused_without_repeated_attempts()
        scope = self.service.discovery.scope(self.mailbox.id, "INBOX")
        self.assertIsNotNone(scope["processing_namespace"])
        self.assertEqual(self.app.reset_scope_baseline(self.mailbox.id, "INBOX"), 1)
        self.assertIsNotNone(
            self.service.discovery.scope(self.mailbox.id, "INBOX")["processing_namespace"]
        )
        account = self.app.settings.accounts[0]
        account.mailboxes[0].folders = ["INBOX"]
        self.app.save_account(AccountSubmission(account, {}, False), self.account.id)
        self.app.save_rules([self.rule])
        ProfileDatabase(self.app.database_path, recover=True)
        result = self.service.run_once(self.app.settings)[0]
        self.assertEqual((result.archived, result.skipped_existing, result.failed), (0, 2, 0))

    def test_namespace_pause_and_reset_preserve_an_unrelated_folder(self):
        self.server.response = lambda name: (name, [b"9001"])
        account = self.app.settings.accounts[0]
        account.mailboxes[0].folders.append("Other")
        self.app.save_account(AccountSubmission(account, {}, False), self.account.id)
        self.app.save_rules([self.rule])
        self.assertEqual(self.service.run_once(self.app.settings)[0].failed, 0)
        other = dict(self.service.discovery.scope(self.mailbox.id, "Other"))
        self.app.save_rules([])
        self.server.response = lambda name: (name, [b"9002"])
        paused = self.assert_paused_without_repeated_attempts()
        self.assertEqual(dict(self.service.discovery.scope(self.mailbox.id, "Other")), other)
        self.app.reset_scope_baseline(paused.source_id, paused.scope_key)
        self.assertEqual(dict(self.service.discovery.scope(self.mailbox.id, "Other")), other)

    def test_transient_retry_failure_does_not_pause_the_scope(self):
        self.server.response = lambda name: (name, [b"9001"])
        with patch.object(
            self.service.engine.spool, "stage", side_effect=OSError("Temporary disk failure")
        ):
            self.assertEqual(
                self.service.run_once(self.app.settings, force_retry=True)[0].failed, 1
            )
        self.assertEqual(self.app.paused_scopes(self.account.id), ())
        self.assertFalse(self.service.has_automatic_work(self.app.settings))
        self.assertEqual(self.service.discovery.scope(self.mailbox.id, "INBOX")["status"], "active")

    def test_changed_namespace_during_retained_raw_stream_pauses_scope(self):
        self.server.response = lambda name: (name, [b"9001"])
        original = self.server.uid

        def change_during_body(command, *args):
            result = original(command, *args)
            if command == "fetch" and "BODY.PEEK[]" in args[-1]:
                self.server.response = lambda name: (name, [b"9002"])
            return result

        with patch.object(self.server, "uid", side_effect=change_during_body):
            self.assert_paused_without_repeated_attempts()
        self.assertEqual(list((self.root / "profile" / "work").glob("*.eml")), [])


class NamespaceClassificationTests(unittest.TestCase):
    def test_first_baseline_change_before_reservation_has_a_durable_reset_marker(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mailbox = Mailbox("owner@example.org", ["INBOX"], archive_existing_messages=True)
        account = Account("Owner", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        rule = Rule("Archive", targets=[RuleTarget(str(root / "archive"))])
        source, server = provider_source(MailProvider.GENERIC_IMAP, account, {"1": sample_mail()})
        store = ConfigStore(root / "profile")
        store.save(
            Settings(
                accounts=[account],
                rules=[rule],
                start_at_login=False,
                automatic_monitoring_paused=True,
            )
        )
        with patch("mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(source)):
            app = create_application(store, MemoryCredentialStore())
        self.addCleanup(app.close)
        service = app._context.execution.service
        original = server.fetch

        def change_during_high_water(*args):
            result = original(*args)
            server.response = lambda name: (name, [b"9002"])
            return result

        with patch.object(server, "fetch", side_effect=change_during_high_water):
            self.assertEqual(service.run_once(app.settings)[0].failed, 1)
        scope = service.discovery.scope(mailbox.id, "INBOX")
        self.assertEqual((scope["status"], scope["baseline_done"]), ("paused", 0))
        self.assertIn('"9001"', scope["processing_namespace"])
        self.assertEqual(len(app.paused_scopes(account.id)), 1)
        with service.discovery.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM output").fetchone()[0], 0)
        calls = len(server.calls)
        self.assertEqual(service.run_once(app.settings)[0].failed, 1)
        self.assertEqual(len(server.calls), calls)
        self.assertEqual(app.reset_scope_baseline(mailbox.id, "INBOX"), 0)
        ProfileDatabase(app.database_path, recover=True)
        result = service.run_once(app.settings)[0]
        self.assertEqual((result.archived, result.skipped_existing, result.failed), (0, 1, 0))
        self.assertEqual(app.paused_scopes(account.id), ())
        self.assertFalse((root / "archive").exists())

    def test_reconnect_namespace_change_is_scoped_and_stops_retained_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            account = Account(
                "Outlook",
                "outlook.office365.com",
                "me@example.org",
                auth_mode=AuthMode.OAUTH_USER,
                mailboxes=[Mailbox("me@example.org", ["INBOX"], archive_existing_messages=True)],
            )
            rule = Rule("Archive", targets=[RuleTarget(str(root / "archive"))])
            settings = Settings(accounts=[account], rules=[rule])
            state = WorkspaceStore(root / "profile" / "workspace.sqlite3")
            mailbox = ImapMailbox()
            oauth = FakeOAuth()
            source = ImapMessageSource(MemoryCredentialStore(), mailbox, oauth)
            service = make_service(state, Registry(source))
            baseline = FakeImapConnection(uids=b"77")
            failed = FakeImapConnection(uids=b"78")
            old = FakeImapConnection(uids=b"78")
            replacement = FakeImapConnection(uids=b"78", validity_data=[b"9002"])
            connections = iter((baseline, failed, old, replacement))
            mailbox._connect = lambda *_args, **_kwargs: next(connections)
            self.assertEqual(service.run_once(settings)[0].failed, 0)
            with patch.object(state.spool, "stage", side_effect=OSError("Temporary disk failure")):
                self.assertEqual(service.run_once(settings)[0].failed, 1)
            settings.rules = []
            state.save_settings(settings)
            original = old.uid

            def expire_during_body(command, *args):
                if command == "fetch" and "BODY.PEEK[]" in args[-1]:
                    raise imaplib.IMAP4.abort("Session invalidated - AccessTokenExpired")
                return original(command, *args)

            old.uid = expire_during_body
            self.assertEqual(service.run_once(settings, force_retry=True)[0].failed, 1)
            scope = state.scope(account.mailboxes[0].id, "INBOX")
            self.assertEqual(scope["status"], "paused")
            self.assertIn("reconnecting", scope["error"])
            self.assertEqual(service.run_once(settings, force_retry=True), [])
            self.assertFalse(service.has_automatic_work(settings))
            self.assertFalse(any(call[0] == "uid" for call in replacement.calls))
            self.assertTrue(old.logged_out and replacement.logged_out)

    def test_invalid_uidvalidity_is_not_a_confirmed_namespace_change(self):
        for values in ([b"invalid"], [b"9001", b"9002"], [9002], [b"0"]):
            with self.subTest(values=values):
                connection = FakeImapConnection(validity_data=values)
                with self.assertRaises(MailboxError) as caught:
                    ImapMailbox._check_uidvalidity(connection, "9001")
                self.assertNotIsInstance(caught.exception, RemoteMessageNamespaceChanged)
