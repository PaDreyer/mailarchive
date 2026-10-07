"""Case-sensitive IMAP logins keep credentials, cursors and frozen work separate."""

import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    CredentialIdentityError,
    credential_binding,
    load_account_credential_data,
)
from mailarchive.application.events import ExecutionState
from mailarchive.application.source_port import MailboxError
from mailarchive.application.synchronization import RangePagination
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, AuthMode
from mailarchive.domain.source_identity import (
    MailTarget,
    imap_scope,
    legacy_source_key,
    mailbox_namespace,
    source_key,
)
from mailarchive.infrastructure.profile_integrity import range_namespace_matches
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests import test_execution_outcomes as execution_fixture
from tests.test_imap_client import FakeImapConnection
from tests.test_restart_core import Registry, raw_mail


class ImapLoginIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = execution_fixture.ExecutionOutcomeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app = self.case.app
        self.service = self.app._context.execution.service
        self.credentials = self.app._credentials

    def save_login(self, username, *, password="fake-password", replace=True):
        account = self.app.settings.accounts[0]
        account.username = username
        account.mailboxes[0].address = username
        self.app.save_account(
            AccountSubmission(account, {"password": password}, replace), replacing_id=account.id
        )
        return self.app.settings.accounts[0]

    def legacy_record(self, account):
        binding = list(credential_binding(account))
        binding[-1] = account.username.strip().casefold()
        raw = json.dumps(
            {"format_version": 1, "credential_binding": binding, "password": "fake-legacy"}
        )
        self.credentials.set(account.id, raw)
        return raw

    def transport(self, app=None, *, validity="1"):
        app = app or self.app
        raw = raw_mail()
        connection = FakeImapConnection(
            uids=b"1",
            validity_data=[validity.encode()],
            raw_by_uid={b"1": raw},
            metadata_by_uid={
                b"1": (
                    b" RFC822.SIZE "
                    + str(len(raw)).encode()
                    + b' INTERNALDATE "01-Jan-2026 00:00:00 +0000"'
                )
            },
        )
        mailbox = ImapMailbox()
        opened = Mock(return_value=connection)
        mailbox._connect = opened
        context = app._context
        source = ImapMessageSource(
            self.credentials,
            mailbox,
            live_account=lambda account_id: next(
                (account for account in context.settings.accounts if account.id == account_id), None
            ),
        )
        context.execution.service.source_registry = Registry(source)
        return source, opened, connection

    def test_every_password_identity_preserves_exact_login_but_oauth_keeps_upn_semantics(self):
        upper = Account("Upper", "imap.example.org", "Alice")
        lower = deepcopy(upper)
        lower.username = "alice"
        # A legacy UI mailbox label may still differ only by case from the LOGIN.
        for identity in (
            credential_binding,
            lambda a: source_key(a, a.mailboxes[0]),
            lambda a: mailbox_namespace(a, a.mailboxes[0]),
            lambda a: imap_scope(MailTarget(a, a.mailboxes[0], "INBOX"), "1"),
        ):
            with self.subTest(identity=identity):
                self.assertNotEqual(identity(upper), identity(lower))
        upper.auth_mode = lower.auth_mode = AuthMode.OAUTH_USER
        upper.client_id = lower.client_id = "fake-client"
        self.assertEqual(credential_binding(upper), credential_binding(lower))
        self.assertEqual(
            source_key(upper, upper.mailboxes[0]), source_key(lower, lower.mailboxes[0])
        )

    def test_public_scheduler_never_sends_new_case_password_to_frozen_login(self):
        original = self.save_login("Alice", password="fake-Alice")
        self.case.source.messages["1"].error = MailboxError("Temporary body failure")
        self.assertEqual(self.case.check().state, ExecutionState.FAILED)
        old_intake = self.service.discovery.pending_automatic_intakes()[0]
        current = self.save_login("alice", password="fake-alice")
        self.assertNotEqual(current.mailboxes[0].id, original.mailboxes[0].id)
        self.app.save_rules([])
        _, opened, _ = self.transport()
        with self.service.discovery.connection() as db, db:
            db.execute("UPDATE intake SET retry_after='2000-01-01T00:00:00+00:00'")
        results = self.service.run_once(self.app.settings, force_retry=True)
        self.assertEqual(results[0].failed, 1)
        opened.assert_not_called()
        self.assertEqual(
            self.service.discovery.pending_automatic_intakes()[0]["id"], old_intake["id"]
        )
        self.assertEqual(
            load_account_credential_data(self.credentials, current)["password"], "fake-alice"
        )

    def test_public_manual_retry_after_restart_uses_frozen_exact_login(self):
        original = self.save_login("Alice")
        self.case.source.messages["1"].error = MailboxError("Temporary body failure")
        start = len(self.case.progress)
        operation = self.app.apply_rule_to_past_mail(self.case.rule.id, None, None, "UTC")
        self.assertEqual(self.case.terminal(start, "operation").state, ExecutionState.FAILED)
        current = self.save_login("alice", password="fake-alice")
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            restarted = create_application(
                ConfigStore(self.case.root / "profile"), self.credentials
            )
            restarted.start()
        self.addCleanup(restarted.close)
        _, opened, _ = self.transport(restarted)
        with self.assertRaises(CredentialIdentityError):
            load_account_credential_data(self.credentials, original)
        results = restarted._context.execution.service.run_range_operation(operation)
        self.assertEqual(results[0].failed, 1)
        opened.assert_not_called()
        self.assertEqual(restarted.settings.accounts[0].username, current.username)

    def test_old_binding_requires_live_exact_login_and_upgrades_without_network(self):
        original = self.save_login("Alice")
        raw = self.legacy_record(original)
        different = deepcopy(original)
        different.username = "alice"
        with self.assertRaises(CredentialIdentityError):
            load_account_credential_data(self.credentials, different, lambda _: original)
        self.assertEqual(self.credentials.get(original.id), raw)
        with self.assertRaises(CredentialIdentityError):
            load_account_credential_data(self.credentials, original)
        loaded = load_account_credential_data(self.credentials, original, lambda _: original)
        self.assertEqual(loaded, {"password": "fake-legacy"})
        record = json.loads(self.credentials.get(original.id))
        self.assertEqual(record["credential_binding_version"], 2)
        self.assertEqual(record["credential_binding"], list(credential_binding(original)))

    def test_identity_only_save_pins_old_binding_and_failed_save_restores_raw_record(self):
        original = self.save_login("Alice")
        raw = self.legacy_record(original)
        changed = deepcopy(original)
        changed.username = changed.mailboxes[0].address = "alice"
        with patch.object(self.app._context, "save", side_effect=OSError("Disk full")):
            with self.assertRaisesRegex(OSError, "Disk full"):
                self.app.save_account(
                    AccountSubmission(changed, {}, False), replacing_id=changed.id
                )
        self.assertEqual(self.credentials.get(original.id), raw)
        self.app.save_account(AccountSubmission(changed, {}, False), replacing_id=changed.id)
        self.assertEqual(
            load_account_credential_data(self.credentials, original)["password"], "fake-legacy"
        )
        with self.assertRaises(CredentialIdentityError):
            load_account_credential_data(self.credentials, changed)

    def test_legacy_source_cursor_receipt_and_id_survive_save_restart_and_case_change(self):
        original = self.save_login("Alice")
        mailbox = original.mailboxes[0]
        legacy_namespace = 'imap-v3:["imap.example.org",993,"alice","INBOX","1"]'
        self.case.source._namespace_for = lambda _target: legacy_namespace
        self.assertEqual(self.case.check().state, ExecutionState.COMPLETED)
        with self.service.discovery.connection() as db, db:
            db.execute(
                "UPDATE source SET mailbox_key=? WHERE id=?",
                (legacy_source_key(original, mailbox), mailbox.id),
            )
            receipts = [tuple(row) for row in db.execute("SELECT * FROM receipt")]
        self.assertTrue(receipts)
        self.legacy_record(original)
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            restarted = create_application(
                ConfigStore(self.case.root / "profile"), self.credentials
            )
            restarted.start()
        self.addCleanup(restarted.close)
        source, opened, connection = self.transport(restarted)
        results = restarted._context.execution.service.run_once(restarted.settings)
        self.assertEqual((results[0].archived, results[0].failed), (0, 0))
        self.assertEqual(restarted.settings.accounts[0].mailboxes[0].id, mailbox.id)
        state = restarted._context.execution.service.discovery
        saved = state.scope(mailbox.id, "INBOX")
        self.assertEqual(saved["processing_namespace"], legacy_namespace)
        self.assertEqual(saved["cursor"], "1")
        self.assertEqual(connection.calls[0], ("login", "Alice", "fake-legacy"))
        with state.connection() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM receipt")], receipts)
        changed = restarted.settings.accounts[0]
        changed.username = changed.mailboxes[0].address = "alice"
        restarted.save_account(
            AccountSubmission(changed, {"password": "fake-new"}, True), replacing_id=changed.id
        )
        new_id = restarted.settings.accounts[0].mailboxes[0].id
        self.assertNotEqual(new_id, mailbox.id)
        self.assertIsNone(state.scope(new_id, "INBOX"))
        results = restarted._context.execution.service.run_once(restarted.settings)
        self.assertEqual((results[0].archived, results[0].failed), (1, 0))
        self.assertTrue(state.scope(new_id, "INBOX")["processing_namespace"].startswith("imap-v4:"))
        self.assertEqual(opened.call_count, 2)
        self.assertIn(("login", "alice", "fake-new"), connection.calls)
        with state.connection() as db:
            self.assertTrue(
                set(receipts) <= {tuple(row) for row in db.execute("SELECT * FROM receipt")}
            )

    def test_legacy_case_collision_is_resolved_using_first_immutable_owner(self):
        original = self.save_login("Alice")
        changed = deepcopy(original)
        changed.username = changed.mailboxes[0].address = "alice"
        with self.service.discovery.connection() as db, db:
            db.execute(
                "UPDATE source SET mailbox_key=?, address=? WHERE id=?",
                (
                    legacy_source_key(original, original.mailboxes[0]),
                    "alice",
                    original.mailboxes[0].id,
                ),
            )
            payload = self.app.settings.to_dict()
            payload["accounts"][0] = changed.to_dict()
            db.execute("UPDATE config_revision SET active=0 WHERE active=1")
            db.execute(
                "INSERT INTO config_revision(payload,created_at,active) VALUES(?, '2026-01-01', 1)",
                (json.dumps(payload),),
            )
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            reopened = create_application(ConfigStore(self.case.root / "profile"), self.credentials)
        self.addCleanup(reopened.close)
        live = reopened.settings.accounts[0].mailboxes[0].id
        persisted = reopened._context.execution.service.configuration.load_settings()
        self.assertNotEqual(live, original.mailboxes[0].id)
        self.assertEqual(persisted.accounts[0].mailboxes[0].id, live)
        current = reopened.settings.accounts[0]
        reopened.save_account(
            AccountSubmission(current, {"password": "fake-current"}, True), replacing_id=current.id
        )
        self.transport(reopened)
        self.assertEqual(
            reopened._context.execution.service.run_once(reopened.settings)[0].failed, 0
        )
        self.assertEqual(reopened.settings.accounts[0].mailboxes[0].id, live)

    def test_legacy_manual_checkpoint_and_direct_retry_keep_their_namespace(self):
        original = self.save_login("Alice")
        source, _, _ = self.transport()
        target = MailTarget(original, original.mailboxes[0], "INBOX")
        legacy_namespace = 'imap-v3:["imap.example.org",993,"alice","INBOX","1"]'
        saved = []
        checkpoint = RangePagination(
            legacy_namespace, None, lambda *args: saved.append(args) or True
        )
        scope, messages = source.search_messages(
            target, lambda *_: False, None, None, range_sync=checkpoint
        )
        list(messages)
        self.assertEqual(scope.processing_namespace, legacy_namespace)
        self.assertFalse(any(item[3] for item in saved))
        remote = source.fetch_message(target, "1", legacy_namespace)
        remote.release_resources()
        self.assertTrue(
            range_namespace_matches(original, original.mailboxes[0], "INBOX", legacy_namespace)
        )
        namespace = imap_scope(target, "1").processing_namespace
        self.assertTrue(
            range_namespace_matches(original, original.mailboxes[0], "INBOX", namespace)
        )
        changed = deepcopy(original)
        changed.username = "alice"
        self.assertFalse(range_namespace_matches(changed, changed.mailboxes[0], "INBOX", namespace))

    def prepare_legacy_history(self, *, pending=False):
        original = self.save_login("Alice")
        namespace = 'imap-v3:["imap.example.org",993,"alice","INBOX","1"]'
        self.case.source._namespace_for = lambda _target: namespace
        if pending:
            self.case.source.messages["1"].error = MailboxError("Temporary body failure")
        self.assertEqual(
            self.case.check().state, ExecutionState.FAILED if pending else ExecutionState.COMPLETED
        )
        with self.service.discovery.connection() as db, db:
            db.execute(
                "UPDATE source SET mailbox_key=? WHERE id=?",
                (legacy_source_key(original, original.mailboxes[0]), original.mailboxes[0].id),
            )
        return original, namespace

    def lose_scope_by_reenabling(self):
        for enabled in (False, True):
            account = self.app.settings.accounts[0]
            account.enabled = enabled
            self.app.save_account(AccountSubmission(account, {}, False), account.id)
        self.assertIsNone(
            self.service.discovery.scope(self.app.settings.accounts[0].mailboxes[0].id, "INBOX")
        )

    def assert_single_legacy_receipt(self, namespace):
        with self.service.discovery.connection() as db:
            keys = [row[0] for row in db.execute("SELECT message_key FROM receipt")]
        self.assertEqual(keys, [namespace + "\0" + "1"])
        self.assertEqual(len(list((self.case.root / "archive").glob("*.eml"))), 1)

    def test_legacy_receipt_is_not_duplicated_after_reenable_and_real_restart(self):
        original, namespace = self.prepare_legacy_history()
        self.lose_scope_by_reenabling()
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            restarted = create_application(
                ConfigStore(self.case.root / "profile"), self.credentials
            )
            restarted.start()
        self.addCleanup(restarted.close)
        self.transport(restarted)
        self.service = restarted._context.execution.service
        result = self.service.run_once(restarted.settings)[0]
        self.assertEqual((result.archived, result.failed), (0, 0))
        self.assertEqual(
            self.service.discovery.scope(original.mailboxes[0].id, "INBOX")["processing_namespace"],
            namespace,
        )
        self.assert_single_legacy_receipt(namespace)

    def test_legacy_receipt_is_not_duplicated_after_folder_remove_and_readd(self):
        original, namespace = self.prepare_legacy_history()
        for folders in (["Other"], ["INBOX"]):
            account = self.app.settings.accounts[0]
            account.mailboxes[0].folders = folders
            self.app.save_account(AccountSubmission(account, {}, False), account.id)
        self.transport()
        result = self.service.run_once(self.app.settings)[0]
        self.assertEqual((result.archived, result.failed), (0, 0))
        self.assertEqual(
            self.service.discovery.scope(original.mailboxes[0].id, "INBOX")["processing_namespace"],
            namespace,
        )
        self.assert_single_legacy_receipt(namespace)

    def test_fresh_manual_range_reuses_legacy_receipt_after_scope_loss(self):
        original, namespace = self.prepare_legacy_history()
        self.lose_scope_by_reenabling()
        self.transport()
        start = len(self.case.progress)
        operation = self.app.apply_rule_to_past_mail(self.case.rule.id, None, None, "UTC")
        self.assertEqual(self.case.terminal(start, "operation").state, ExecutionState.COMPLETED)
        self.assert_single_legacy_receipt(namespace)
        with self.service.discovery.connection() as db:
            run = db.execute(
                "SELECT checkpoint FROM scan_run WHERE operation_id=?", (operation,)
            ).fetchone()
        checkpoint = json.loads(run[0])["range_targets"]["INBOX"]
        self.assertEqual(checkpoint["namespace"], namespace)
        self.assertIsNone(self.service.discovery.scope(original.mailboxes[0].id, "INBOX"))

    def test_retained_v3_intake_and_new_scan_after_scope_loss_share_identity(self):
        original, namespace = self.prepare_legacy_history(pending=True)
        self.lose_scope_by_reenabling()
        self.transport()
        result = self.service.run_once(self.app.settings, force_retry=True)[0]
        self.assertEqual((result.archived, result.failed), (1, 0))
        self.assertEqual(self.service.discovery.pending_automatic_intakes(), [])
        self.assert_single_legacy_receipt(namespace)
        self.assertEqual(
            self.service.discovery.scope(original.mailboxes[0].id, "INBOX")["processing_namespace"],
            namespace,
        )

    def test_scope_loss_does_not_alias_a_real_new_uidvalidity_to_old_receipts(self):
        original, namespace = self.prepare_legacy_history()
        self.lose_scope_by_reenabling()
        self.transport(validity="2")
        result = self.service.run_once(self.app.settings)[0]
        self.assertEqual((result.archived, result.failed), (1, 0))
        saved = self.service.discovery.scope(original.mailboxes[0].id, "INBOX")
        self.assertTrue(saved["processing_namespace"].startswith("imap-v4:"))
        with self.service.discovery.connection() as db:
            keys = {row[0] for row in db.execute("SELECT message_key FROM receipt")}
        self.assertEqual(keys, {namespace + "\0" + "1", saved["processing_namespace"] + "\0" + "1"})
