"""A dismissed protected-store prompt aborts only this account's remote check."""

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.cancellation import Cancellation, ProcessingStopped
from mailarchive.application.credential_port import CredentialError
from mailarchive.application.events import ExecutionState
from mailarchive.application.service import AccountRunResult
from mailarchive.application.source_port import MailboxError, RemoteMessage
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
from mailarchive.domain.source_identity import api_scope
from mailarchive.infrastructure.credentials import KeyringCredentialStore, MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from tests.concurrency import THREAD_TIMEOUT
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_restart_core import FakeSource, Registry, raw_mail
from tests.workspace_fixture import WorkspaceStore, make_service


class ProtectedSource(FakeSource):
    """A source port that can lose protected credentials at each remote boundary."""

    def __init__(self):
        super().__init__(
            {
                str(number): RemoteMessage(
                    str(number), raw_mail(), datetime(2026, 1, number, tzinfo=timezone.utc), "api"
                )
                for number in (1, 2)
            }
        )
        self.failure = None
        self.stage = "fetch"
        self.blocked_account = None
        self.calls = []
        self.before_failure = lambda: None

    def _namespace_for(self, target):
        return api_scope(target).processing_namespace

    def protected_read(self, target, stage):
        self.calls.append((target.account.id, target.mailbox.id, target.folder, stage))
        if (
            target.account.id == self.blocked_account
            and stage == self.stage
            and self.failure is not None
        ):
            self.before_failure()
            raise self.failure

    def targets(self, account, mailbox, *, cancellation=None):
        targets = super().targets(account, mailbox, cancellation=cancellation)
        self.protected_read(targets[0], "targets")
        return targets

    def fetch_messages(self, target, should_fetch, *, sync=None, cancellation=None):
        self.protected_read(target, "fetch")
        scope, messages = super().fetch_messages(
            target, should_fetch, sync=sync, cancellation=cancellation
        )

        def iterate():
            for message in messages:
                if self.stage == "raw" and target.account.id == self.blocked_account:

                    def chunks():
                        self.protected_read(target, "raw")
                        yield raw_mail()

                    yield RemoteMessage(
                        message.id,
                        received_at=message.received_at,
                        received_origin=message.received_origin,
                        raw_chunks=chunks,
                    )
                else:
                    yield message

        return scope, iterate()

    def fetch_message(self, target, remote_id, processing_namespace, *, cancellation=None):
        self.protected_read(target, "retry")
        return super().fetch_message(
            target, remote_id, processing_namespace, cancellation=cancellation
        )


class CredentialRunFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = self.account_config("Affected", ("first", "second"))
        self.healthy = self.account_config("Healthy", ("healthy",))
        self.settings = Settings(
            accounts=[self.account, self.healthy],
            rules=[Rule("All", targets=[RuleTarget(str(self.root / "archive"))])],
            automatic_monitoring_paused=True,
            start_at_login=False,
        )
        self.source = ProtectedSource()
        self.source.blocked_account = self.account.id
        self.state = WorkspaceStore(self.root / "profile" / "workspace.sqlite3")
        self.service = make_service(self.state, Registry(self.source))

    @staticmethod
    def account_config(label, addresses):
        account = Account(
            label,
            username=addresses[0] + "@example.org",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="synthetic-client",
            tenant_id="synthetic-tenant",
            mailboxes=[
                Mailbox(
                    address + "@example.org", ["Inbox", "Archive"], archive_existing_messages=True
                )
                for address in addresses
            ],
        )
        account.validate()
        return account

    def run_once(self):
        return {
            result.account_id: result
            for result in self.service.run_once(self.settings, force_retry=True)
        }

    def test_failure_aborts_all_mailboxes_of_account_then_next_check_can_retry(self):
        for stage in ("targets", "fetch", "raw"):
            with self.subTest(stage=stage):
                self.source.stage = stage
                self.source.failure = CredentialError("Protected store unlock was dismissed")
                self.source.calls.clear()
                results = self.run_once()
                affected = results[self.account.id]
                self.assertEqual(affected.failed, 1)
                self.assertEqual(affected.archived, 0)
                self.assertEqual(len(affected.errors), 1)
                failed_calls = [
                    call
                    for call in self.source.calls
                    if call[0] == self.account.id and call[-1] == stage
                ]
                self.assertEqual(len(failed_calls), 1)
                self.assertFalse(
                    any(call[1] == self.account.mailboxes[1].id for call in self.source.calls)
                )
                healthy = results[self.healthy.id]
                self.assertEqual(healthy.failed, 0)
                self.assertEqual(
                    {call[2] for call in self.source.calls if call[0] == self.healthy.id},
                    {"Inbox", "Archive"},
                )
                self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)
        self.source.failure = None
        self.source.stage = "fetch"
        results = self.run_once()
        self.assertEqual(results[self.account.id].failed, 0)
        self.assertEqual(results[self.account.id].archived, 4)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 6)

    def test_ordinary_folder_error_keeps_other_folders_and_mailboxes(self):
        original = self.source.protected_read

        def one_folder_error(target, stage):
            original(target, stage)
            if (
                target.mailbox.id == self.account.mailboxes[0].id
                and target.folder == "Inbox"
                and stage == "fetch"
            ):
                raise MailboxError("Folder temporarily inaccessible")

        with patch.object(self.source, "protected_read", side_effect=one_folder_error):
            results = self.run_once()
        self.assertEqual(results[self.account.id].failed, 1)
        self.assertEqual(results[self.account.id].archived, 4)
        self.assertEqual(results[self.healthy.id].archived, 2)

    def test_frozen_retry_failure_blocks_rest_but_preserves_intakes_for_unlock(self):
        for message in self.source.messages.values():
            message.error = MailboxError("Temporary download error")
        initial = self.run_once()
        self.assertGreater(initial[self.account.id].failed, 1)
        pending_before = len(self.service.discovery.pending_automatic_intakes(due_only=False))
        self.settings.rules = []
        for message in self.source.messages.values():
            message.error = None
        self.source.stage = "retry"
        self.source.failure = CredentialError("Protected store unlock was dismissed")
        self.source.calls.clear()
        results = self.run_once()
        self.assertEqual(results[self.account.id].failed, 1)
        self.assertEqual(results[self.healthy.id].failed, 0)
        self.assertEqual(results[self.healthy.id].archived, 2)
        self.assertEqual(len([call for call in self.source.calls if call[0] == self.account.id]), 1)
        remaining = self.service.discovery.pending_automatic_intakes(due_only=False)
        self.assertEqual(len(remaining), pending_before - 2)
        self.source.failure = None
        results = self.run_once()
        self.assertEqual(results[self.account.id].archived, 4)
        self.assertEqual(results[self.account.id].failed, 0)
        self.assertEqual(self.service.discovery.pending_automatic_intakes(due_only=False), [])

    def test_accepted_local_outputs_resume_before_credential_gate(self):
        obstruction = self.root / "offline"
        obstruction.write_text("not a directory")
        self.settings.rules[0].targets = [RuleTarget(str(obstruction / "archive"))]
        self.assertGreater(self.run_once()[self.account.id].failed, 0)
        self.assertTrue(self.service.delivery.open_plans())
        obstruction.unlink()
        self.source.failure = CredentialError("Protected store unlock was dismissed")
        results = self.run_once()
        self.assertEqual(results[self.account.id].archived, 4)
        self.assertEqual(results[self.account.id].failed, 1)
        self.assertEqual(self.service.delivery.open_plans(), [])
        self.assertEqual(len(list((obstruction / "archive").glob("*.eml"))), 6)

    def test_lazy_frozen_download_failure_stops_further_account_retries(self):
        for message in self.source.messages.values():
            message.error = MailboxError("Temporary download error")
        self.run_once()
        self.settings.rules = []
        for message in self.source.messages.values():
            message.error = None
        original = self.source.fetch_message
        unlocks = []

        def fetch(target, remote_id, namespace, *, cancellation=None):
            message = original(target, remote_id, namespace, cancellation=cancellation)
            if target.account.id != self.account.id:
                return message

            def chunks():
                unlocks.append(True)
                raise CredentialError("Protected store unlock was dismissed")
                yield b""  # Make the failed lazy download an iterator.

            return RemoteMessage(
                message.id,
                received_at=message.received_at,
                received_origin=message.received_origin,
                raw_chunks=chunks,
            )

        self.source.calls.clear()
        with patch.object(self.source, "fetch_message", side_effect=fetch):
            results = self.run_once()
        self.assertEqual((len(unlocks), results[self.account.id].failed), (1, 1))
        self.assertEqual(len([call for call in self.source.calls if call[0] == self.account.id]), 1)
        self.assertEqual(results[self.healthy.id].archived, 2)
        self.assertEqual(len(self.service.discovery.pending_automatic_intakes(due_only=False)), 4)
        self.assertEqual(self.run_once()[self.account.id].archived, 4)

    def test_manual_operation_remains_failed_and_retryable_until_unlock(self):
        selected = {
            mailbox.id for account in self.settings.accounts for mailbox in account.mailboxes
        }
        operation = self.service.prepare_range_operation(self.settings, selected)
        self.source.failure = CredentialError("Protected store unlock was dismissed")
        results = {
            result.account_id: result for result in self.service.run_range_operation(operation)
        }
        self.assertEqual(results[self.account.id].failed, 1)
        self.assertEqual(results[self.healthy.id].archived, 2)
        self.assertEqual(self.service.operations.manual_operation(operation)["status"], "failed")
        sources = {
            source["source_id"]: source["status"]
            for source in self.service.operations.manual_operation_sources(operation)
        }
        self.assertEqual(sources[self.account.mailboxes[0].id], "failed")
        self.assertEqual(sources[self.account.mailboxes[1].id], "queued")
        self.source.failure = None
        self.assertEqual(
            sum(result.archived for result in self.service.run_range_operation(operation)), 4
        )
        self.assertEqual(self.service.operations.manual_operation(operation)["status"], "completed")

    def test_stop_and_shutdown_dominate_credential_failure(self):
        self.source.failure = CredentialError("Protected store unlock was dismissed")
        stopped = threading.Event()
        self.source.before_failure = stopped.set
        with self.assertRaises(ProcessingStopped):
            self.service.run_once(self.settings, cancellation=Cancellation(stopped.is_set))
        self.source.before_failure = self.service.request_shutdown
        with self.assertRaises(ProcessingStopped):
            self.service.run_once(self.settings)
        self.assertEqual(self.service.delivery.open_plans(), [])

    def test_account_result_merge_retains_credential_failure(self):
        result = AccountRunResult(self.account.id)
        partial = AccountRunResult(self.account.id, failed=1, errors=["Protected store failed"])
        partial._record_credential_failure(CredentialError("Protected store failed"))
        result.add(partial)
        self.assertTrue(result._credentials_failed)
        self.assertEqual((result.failed, result.errors), (1, ["Protected store failed"]))

    def test_public_check_reports_failure_and_keeps_healthy_account_outcome(self):
        config = ConfigStore(self.root / "public-profile")
        config.save(self.settings)
        with (
            patch(
                "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(self.source)
            ),
            patch("mailarchive.bootstrap.set_start_at_login"),
        ):
            app = create_application(config, MemoryCredentialStore())
            app.start()
        self.addCleanup(app.close)
        progress = []
        condition = threading.Condition()

        def observe(item):
            with condition:
                progress.append(item)
                condition.notify_all()

        app.set_observers(lambda _: None, observe)
        self.source.failure = CredentialError("Protected store unlock was dismissed")
        app.check_now()
        with condition:
            self.assertTrue(
                condition.wait_for(
                    lambda: any(not item.active for item in progress), THREAD_TIMEOUT
                )
            )
        self.assertEqual(progress[-1].state, ExecutionState.FAILED)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)


class SecretServiceRunFailureTests(unittest.TestCase):
    def test_dismissed_native_backend_unlock_occurs_once_for_three_imap_folders(self):
        try:
            from keyring.backends import SecretService
        except ImportError:
            self.skipTest("SecretService backend is not installed on this platform")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ConfigStore(root / "profile")
            config.save(Settings(start_at_login=False))
            values = {}
            backend = SecretService.Keyring()
            locked = False
            unlocks = []
            collection = SimpleNamespace(
                is_locked=lambda: True, unlock=lambda: unlocks.append(True)
            )

            def get_password(service, account_id):
                if not locked:
                    return values.get(account_id)
                with (
                    patch.object(SecretService.secretstorage, "dbus_init", return_value=None),
                    patch.object(
                        SecretService.secretstorage,
                        "get_default_collection",
                        return_value=collection,
                    ),
                ):
                    return backend.get_password(service, account_id)

            keyring = SimpleNamespace(
                get_keyring=lambda: SimpleNamespace(priority=1),
                get_password=get_password,
                set_password=lambda service, account_id, value: values.__setitem__(
                    account_id, value
                ),
                delete_password=lambda service, account_id: values.pop(account_id, None),
            )
            credentials = KeyringCredentialStore(keyring_module=keyring)
            with patch("mailarchive.bootstrap.set_start_at_login"):
                app = create_application(config, credentials)
            try:
                account = Account(
                    "Owner",
                    "imap.example.org",
                    "owner@example.org",
                    mailboxes=[
                        Mailbox(
                            "owner@example.org",
                            ["INBOX", "Archive", "Sent"],
                            archive_existing_messages=True,
                        )
                    ],
                )
                app.save_account(
                    AccountSubmission(account, {"password": "synthetic-password"}, True)
                )
                app.save_rules([Rule("All", targets=[RuleTarget(str(root / "archive"))])])
                connection = FakeImapConnection()
                source = ImapMessageSource(credentials, FakeImapMailbox(connection))
                service = app._context.execution.service
                service.source_registry = Registry(source)
                self.assertEqual(service.run_once(app.settings)[0].archived, 3)
                locked = True
                connection.calls.clear()
                failed = service.run_once(app.settings)[0]
                self.assertEqual((len(unlocks), failed.failed, len(failed.errors)), (1, 1, 1))
                self.assertFalse(connection.calls)
                locked = False
                self.assertEqual(service.run_once(app.settings)[0].failed, 0)
            finally:
                app.close()
