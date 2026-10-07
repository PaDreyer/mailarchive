"""Protected credentials stay with their identity across frozen remote retries."""

import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    CredentialIdentityError,
    credential_binding,
    load_account_credential_data,
    load_credential_data,
    save_credential_data,
    store_account_credentials,
)
from mailarchive.application.account_status import AccountState
from mailarchive.application.events import ExecutionState
from mailarchive.application.source_port import MailboxError
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Account, AuthMode, MailProvider
from mailarchive.domain.source_identity import imap_scope
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.oauth import OAuthManager
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests import test_execution_outcomes as execution_fixture
from tests.test_imap_client import FakeImapConnection
from tests.test_restart_core import Registry


class CredentialIdentityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.case = execution_fixture.ExecutionOutcomeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app = self.case.app
        self.credentials = self.app._credentials
        self.service = self.app._context.execution.service

    def prepare_intake(self, *, manual=False):
        self.case.source.messages["1"].error = MailboxError("Temporary body download failure")
        if manual:
            start = len(self.case.progress)
            operation = self.app.apply_rule_to_past_mail(self.case.rule.id, None, None, "UTC")
            self.assertEqual(self.case.terminal(start, "operation").state, ExecutionState.FAILED)
            return operation
        self.assertEqual(self.case.check().state, ExecutionState.FAILED)
        self.assertEqual(len(self.service.discovery.pending_automatic_intakes()), 1)
        return None

    def replace_host(self):
        account = self.app.settings.accounts[0]
        original_source = account.mailboxes[0].id
        account.host = "new-server.example.org"
        self.app.save_account(
            AccountSubmission(account, {"password": "fake-new-password"}, True),
            replacing_id=account.id,
        )
        self.app.save_rules([])
        self.assertNotEqual(self.app.settings.accounts[0].mailboxes[0].id, original_source)
        return account

    def install_transport(self, app=None):
        app = app or self.app
        context = app._context
        connection = FakeImapConnection(uids=b"1", validity_data=[b"1"])
        mailbox = ImapMailbox()
        opened = Mock(return_value=connection)
        mailbox._connect = opened
        source = ImapMessageSource(
            self.credentials,
            mailbox,
            live_account=lambda account_id: next(
                (a for a in context.settings.accounts if a.id == account_id), None
            ),
        )
        context.execution.service.source_registry = Registry(source)
        return opened, connection

    def test_public_save_and_automatic_scheduler_never_send_new_password_to_old_host(self):
        self.prepare_intake()
        current = self.replace_host()
        retained = self.credentials.get(current.id)
        opened, connection = self.install_transport()
        with self.service.delivery.connection() as db, db:
            db.execute("UPDATE intake SET retry_after='2000-01-01T00:00:00+00:00'")
        start = len(self.case.progress)
        self.app.set_automatic_monitoring_paused(False)
        self.assertEqual(self.case.terminal(start, "automatic").state, ExecutionState.FAILED)
        opened.assert_not_called()
        self.assertEqual(connection.calls, [])
        self.assertEqual(self.credentials.get(current.id), retained)
        self.assertEqual(self.app.account_status(current.id).state, AccountState.WAITING_FOR_RULE)
        self.assertEqual(len(self.service.discovery.pending_automatic_intakes()), 1)

    def test_public_manual_operation_retry_retains_snapshot_without_using_new_credentials(self):
        operation = self.prepare_intake(manual=True)
        current = self.replace_host()
        retained = self.credentials.get(current.id)
        opened, _connection = self.install_transport()
        start = len(self.case.progress)
        self.app.retry_activity("operation:" + operation)
        self.assertEqual(self.case.terminal(start, "operation").state, ExecutionState.FAILED)
        opened.assert_not_called()
        self.assertEqual(self.credentials.get(current.id), retained)
        snapshot = self.service.operations.manual_operation(operation)["settings_json"]
        self.assertEqual(json.loads(snapshot)["accounts"][0]["host"], "imap.example.org")
        self.assertNotIn("fake-new-password", snapshot)

    def test_bound_credentials_protect_old_intakes_after_real_profile_restart(self):
        self.prepare_intake()
        current = self.replace_host()
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            restarted = create_application(
                ConfigStore(self.case.root / "profile"), self.credentials
            )
            opened, _connection = self.install_transport(restarted)
            restarted.start()
        self.addCleanup(restarted.close)
        results = restarted._context.execution.service.run_once(
            restarted.settings, force_retry=True
        )
        self.assertEqual(results[0].failed, 1)
        opened.assert_not_called()
        self.assertEqual(
            json.loads(self.credentials.get(current.id))["credential_binding"],
            list(credential_binding(current)),
        )

    def test_legacy_password_is_bound_to_previous_live_identity_before_identity_only_edit(self):
        original = self.app.settings.accounts[0]
        self.credentials.set(original.id, "fake-legacy-password")
        changed = deepcopy(original)
        changed.host = "other.example.org"
        self.app.save_account(AccountSubmission(changed, {}, False), replacing_id=changed.id)
        self.assertEqual(
            load_account_credential_data(self.credentials, original)["password"],
            "fake-legacy-password",
        )
        with self.assertRaises(CredentialIdentityError):
            load_account_credential_data(self.credentials, changed)

    def test_failed_save_rolls_back_legacy_binding_and_replacement_atomically(self):
        original = self.app.settings.accounts[0]
        self.credentials.set(original.id, "fake-legacy-password")
        changed = deepcopy(original)
        changed.host = "other.example.org"
        with patch.object(self.app._context, "save", side_effect=OSError("Config write failed")):
            with self.assertRaisesRegex(OSError, "Config write failed"):
                self.app.save_account(
                    AccountSubmission(changed, {"password": "fake-new-password"}, True),
                    replacing_id=changed.id,
                )
        self.assertEqual(self.credentials.get(original.id), "fake-legacy-password")
        self.assertEqual(self.app.settings.accounts[0].host, original.host)

    def test_same_identity_password_rotation_works_through_all_remote_entrypoints(self):
        account = self.app.settings.accounts[0]
        for password in ("fake-original-password", "fake-rotated-password"):
            self.app.save_account(
                AccountSubmission(account, {"password": password}, False), replacing_id=account.id
            )
        opened, connection = self.install_transport()
        source = self.service.source_registry.get(account)
        target = self.case.source.targets(account, account.mailboxes[0])[0]
        namespace = imap_scope(target, "1").processing_namespace
        connection.list = Mock(return_value=("OK", [b'() "/" "INBOX"']))
        source.list_folders(target)
        _, messages = source.fetch_messages(target, lambda *_: False)
        list(messages)
        _, messages = source.search_messages(target, lambda *_: False, None, None)
        list(messages)
        remote = source.fetch_message(target, "1", namespace)
        remote.release_resources()
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(
            [call for call in connection.calls if call[0] == "login"],
            [("login", account.username, "fake-rotated-password")] * 4,
        )

    def test_direct_source_cannot_adopt_unbound_password_without_live_settings(self):
        account = self.app.settings.accounts[0]
        self.credentials.set(account.id, "fake-legacy-password")
        mailbox = Mock()
        source = ImapMessageSource(self.credentials, mailbox)
        target = self.case.source.targets(account, account.mailboxes[0])[0]
        for access in (
            lambda: source.list_folders(target),
            lambda: source.fetch_messages(target, lambda *_: True),
            lambda: source.search_messages(target, lambda *_: True, None, None),
            lambda: source.fetch_message(target, "1", imap_scope(target, "1").processing_namespace),
        ):
            with self.assertRaises(CredentialIdentityError):
                access()
        self.assertEqual(mailbox.mock_calls, [])
        self.assertEqual(self.credentials.get(account.id), "fake-legacy-password")

    def test_credential_read_race_cannot_retag_replacement_identity(self):
        original = self.app.settings.accounts[0]
        changed = deepcopy(original)
        changed.host = "other.example.org"
        store = MemoryCredentialStore()
        store_account_credentials(store, original, {"password": "fake-old-password"})
        original_get = store.get

        def replace_before_read(account_id):
            store.get = original_get
            store_account_credentials(
                store, changed, {"password": "fake-new-password"}, replace=True
            )
            return original_get(account_id)

        store.get = replace_before_read
        with self.assertRaises(CredentialIdentityError):
            load_account_credential_data(store, original)
        self.assertEqual(load_credential_data(store, changed.id)["password"], "fake-new-password")

    def test_legacy_client_secret_reaches_explicit_public_authorization(self):
        account = Account(
            "Legacy Gmail",
            username="owner@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="fake-client-id",
        )
        self.app.save_account(AccountSubmission(account, {}, False))
        save_credential_data(
            self.credentials, account.id, {"oauth_client_secret": "fake-client-secret"}
        )
        retained = self.credentials.get(account.id)
        factory = Mock(side_effect=RuntimeError("Stopped before browser sign-in"))
        self.app._authorize = lambda account, credentials, **kwargs: OAuthManager(
            credentials, google_user_flow_factory=factory, **kwargs
        ).authorize_google(account)
        results = []
        self.assertTrue(self.app.authorize_account(account.id, on_complete=results.append))
        self.assertTrue(self.app._background.wait(5))
        self.app.dispatch_callbacks()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].detail, "Stopped before browser sign-in")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(
            factory.call_args.args[0]["installed"]["client_secret"], "fake-client-secret"
        )
        self.assertEqual(self.credentials.get(account.id), retained)
