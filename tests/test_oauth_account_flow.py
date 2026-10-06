"""OAuth onboarding and readiness through the real application and profile."""

import json
import tempfile
import threading
import unittest
import weakref
from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    account_credential_lock,
    update_credential_data,
)
from mailarchive.application.account_status import (
    AccountAction,
    AccountState,
    AuthorizationOutcome,
    AuthorizationState,
)
from mailarchive.application.credential_port import CredentialError
from mailarchive.application.errors import AuthorizationRequiredError
from mailarchive.application.events import EventLevel, ExecutionState
from mailarchive.application.source_port import MailboxError, RemoteMessage
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import (
    MICROSOFT_IMAP_HOST,
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.domain.source_identity import MailTarget, api_scope, imap_scope
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.oauth import (
    GOOGLE_GMAIL_READONLY_SCOPE,
    MICROSOFT_IMAP_ACCESS_SCOPE,
    MICROSOFT_MAIL_READ_SCOPE,
    MICROSOFT_MAIL_READ_SHARED_SCOPE,
    OAuthManager,
)
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.presentation.account_form import AccountFormValues, build_account_submission
from tests.concurrency import THREAD_TIMEOUT, ObservedLock
from tests.oauth_fixture import MicrosoftRefreshHttp, MicrosoftRequestsTransport, microsoft_cache
from tests.test_restart_core import FakeSource, PagedRangeSource, Registry, raw_mail


class EmptyRangeSource(FakeSource):
    def search_messages(self, target, should_fetch, start, end, *, range_sync, cancellation):
        self.folders_seen.append(target.folder)
        scope = (
            imap_scope(target, "1")
            if target.account.provider == MailProvider.GENERIC_IMAP
            else api_scope(target)
        )
        range_sync.start(scope.processing_namespace)
        range_sync.finish()
        return scope, iter(())


class PagedOAuthSource(PagedRangeSource):
    def _namespace_for(self, target):
        return (
            imap_scope(target, "1")
            if target.account.provider == MailProvider.GENERIC_IMAP
            else api_scope(target)
        ).processing_namespace


class OAuthAccountFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConfigStore(self.root)
        self.store.save(Settings(start_at_login=False))
        self.credentials = MemoryCredentialStore()
        with patch("mailarchive.bootstrap.set_start_at_login"):
            self.app = create_application(self.store, self.credentials)
        self.addCleanup(self.app.close)

    def new_submission(self, provider=MailProvider.MICROSOFT_GRAPH, *, enabled=True):
        account = Account(
            "Owner",
            username="owner@example.org",
            provider=provider,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
            enabled=enabled,
            host=MICROSOFT_IMAP_HOST if provider == MailProvider.GENERIC_IMAP else "",
            mailboxes=[Mailbox("owner@example.org", ["INBOX"])],
        )
        return AccountSubmission(account, {}, False)

    def save_account(self, provider=MailProvider.MICROSOFT_GRAPH, *, enabled=True):
        submission = self.new_submission(provider, enabled=enabled)
        account = submission.account
        self.app.save_account(submission)
        self.wait_tasks()
        return account

    def wait_tasks(self):
        self.assertTrue(self.app._background.wait(THREAD_TIMEOUT))
        self.app.dispatch_callbacks()

    def grant(self, account, credentials, *, cancelled):
        if account.provider == MailProvider.GMAIL_API:
            update_credential_data(
                credentials,
                account.id,
                google_credentials={
                    "client_id": account.client_id,
                    "refresh_token": "synthetic",
                    "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
                },
            )
        else:
            scopes = (
                [MICROSOFT_IMAP_ACCESS_SCOPE]
                if account.provider == MailProvider.GENERIC_IMAP
                else [MICROSOFT_MAIL_READ_SCOPE]
            )
            if account.provider == MailProvider.MICROSOFT_GRAPH and len(account.mailboxes) > 1:
                scopes.append(MICROSOFT_MAIL_READ_SHARED_SCOPE)
            update_credential_data(
                credentials, account.id, msal_cache=microsoft_cache(account, scopes)
            )

    def authorize(self, account):
        self.app._authorize = self.grant
        outcomes = []
        self.assertTrue(self.app.authorize_account(account.id, on_complete=outcomes.append))
        self.wait_tasks()
        self.assertEqual(outcomes[0].outcome, AuthorizationOutcome.COMPLETED)

    def retry_operation(self, operation_id):
        finished = threading.Event()
        progress, events = [], []

        def on_progress(update):
            if update.origin == "operation" and not update.active and not finished.is_set():
                progress.append(update)
                finished.set()

        self.app.set_observers(events.append, on_progress)
        self.app.set_automatic_monitoring_paused(True)
        self.app.retry_activity("operation:" + operation_id)
        self.app._context.execution.start()
        self.assertTrue(finished.wait(THREAD_TIMEOUT))
        return progress[0], events

    def change_tenant(self, account):
        submission = build_account_submission(
            AccountFormValues(
                label=account.label,
                provider=account.provider,
                auth_mode=account.auth_mode,
                username=account.username,
                client_id=account.client_id,
                tenant_id="12345678-1234-1234-1234-123456789abc",
                mailboxes=account.mailboxes,
            ),
            existing=account,
        )
        self.assertTrue(submission.replace_credentials)
        self.app.save_account(submission, replacing_id=account.id)
        self.wait_tasks()
        self.authorize(submission.account)
        return submission.account

    def prepare_partial_oauth_operation(self, provider=MailProvider.GENERIC_IMAP, *, offline=None):
        account = self.save_account(provider)
        self.authorize(account)
        service = self.app._context.execution.service
        manager = service.source_registry.get(account).oauth
        origin = {
            MailProvider.GENERIC_IMAP: "imap_internaldate",
            MailProvider.GMAIL_API: "gmail_internal_date",
            MailProvider.MICROSOFT_GRAPH: "graph_received_date_time",
        }[provider]
        source = PagedOAuthSource(
            {
                message_id: RemoteMessage(
                    message_id,
                    raw_mail(),
                    datetime(2026, 1, int(message_id), tzinfo=timezone.utc),
                    origin,
                )
                for message_id in ("1", "2")
            }
        )
        service.source_registry = Registry(source)
        targets = [RuleTarget(str(self.root / "archive"))]
        if offline is not None:
            targets.append(RuleTarget(str(offline / "archive")))
        rule = Rule("Archive", targets=targets)
        self.app.save_rules([rule])
        operation_id = service.prepare_range_operation(
            self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
        )
        service.run_range_operation(operation_id)
        run = dict(service.operations.manual_run_for_source(operation_id, account.mailboxes[0].id))
        self.assertEqual(run["status"], "failed")
        self.assertEqual(source.enumerated, ["1"])
        return account, service, source, manager, operation_id, run

    def revoke_credentials(self, account, manager):
        method = (
            "google_access_token"
            if account.provider == MailProvider.GMAIL_API
            else "microsoft_access_token"
        )
        with patch.object(
            manager, "_" + method, side_effect=AuthorizationRequiredError("Revoked grant")
        ):
            with self.assertRaises(AuthorizationRequiredError):
                getattr(manager, method)(account)
        self.assertIsNone(self.credentials.get(account.id))
        self.assertEqual(
            self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
        )

    def reauthorize_and_save(self, account):
        editor = self.app.account_editor(account.id)
        draft = AccountSubmission(account, {}, False)
        self.assertTrue(editor.authorize(draft))
        self.wait_tasks()
        self.assertEqual(editor.status(draft).authorization.state, AuthorizationState.AUTHORIZED)
        editor.save(draft)
        self.wait_tasks()

    def test_graph_partial_scan_remains_resumable_after_blocked_retries(self):
        self._assert_partial_scan_recovery(MailProvider.MICROSOFT_GRAPH)

    def test_google_partial_scan_remains_resumable_after_blocked_retries(self):
        self._assert_partial_scan_recovery(MailProvider.GMAIL_API)

    def test_imap_partial_scan_remains_resumable_after_blocked_retries(self):
        self._assert_partial_scan_recovery(MailProvider.GENERIC_IMAP)

    def test_interrupted_partial_scan_remains_resumable_after_blocked_retries(self):
        self._assert_partial_scan_recovery(MailProvider.MICROSOFT_GRAPH, interrupted=True)

    def _assert_partial_scan_recovery(self, provider, *, interrupted=False):
        account, service, source, manager, operation_id, run = self.prepare_partial_oauth_operation(
            provider
        )
        if interrupted:
            service.operations.restart_run(run["id"])
            service.operations.interrupt_run(run["id"], "The process was interrupted")
            run = dict(
                service.operations.manual_run_for_source(operation_id, account.mailboxes[0].id)
            )
        self.revoke_credentials(account, manager)
        self.app.save_rules([])
        with patch.object(source, "targets", wraps=source.targets) as targets:
            for _ in range(2):
                terminal, _events = self.retry_operation(operation_id)
                self.assertEqual(terminal.state, ExecutionState.FAILED)
                self.assertEqual(
                    dict(
                        service.operations.manual_run_for_source(
                            operation_id, account.mailboxes[0].id
                        )
                    ),
                    run,
                )
            targets.assert_not_called()
        self.assertEqual(source.enumerated, ["1"])
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)
        self.reauthorize_and_save(account)
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.COMPLETED)
        self.assertEqual(service.operations.run_status(run["id"]), "completed")
        self.assertEqual(source.enumerated, ["1", "2"])
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)

    def test_target_authorization_failure_preserves_partial_scan_until_reauthorization(self):
        account, service, source, manager, operation_id, run = (
            self.prepare_partial_oauth_operation()
        )

        def search(*_args, **_kwargs):
            self.revoke_credentials(account, manager)
            raise AuthorizationRequiredError("Revoked grant")

        with patch.object(source, "search_messages", side_effect=search):
            terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        retried = dict(
            service.operations.manual_run_for_source(operation_id, account.mailboxes[0].id)
        )
        self.assertEqual(retried["status"], "failed")
        self.assertEqual(retried["checkpoint"], run["checkpoint"])
        self.assertEqual(retried["selection_json"], run["selection_json"])
        self.assertIn("Revoked grant", retried["error"])
        self.assertIsNone(service.active_range_run_id)
        self.reauthorize_and_save(account)
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.COMPLETED)
        self.assertEqual(source.enumerated, ["1", "2"])

    def test_public_retry_keeps_discovered_folders_after_reauthorization(self):
        service = self.app._context.execution.service
        registry = service.source_registry
        for provider in (MailProvider.MICROSOFT_GRAPH, MailProvider.GENERIC_IMAP):
            with self.subTest(provider=provider):
                service.source_registry = registry
                account = self.save_account(provider)
                self.authorize(account)
                account = deepcopy(account)
                account.mailboxes[0].folders = []
                self.app.save_account(
                    AccountSubmission(account, {}, False), replacing_id=account.id
                )
                self.wait_tasks()
                service = self.app._context.execution.service
                manager = service.source_registry.get(account).oauth
                source = PagedOAuthSource(
                    {
                        message_id: RemoteMessage(
                            message_id,
                            raw_mail(),
                            datetime(2026, 1, int(message_id), tzinfo=timezone.utc),
                            "imap_internaldate"
                            if provider == MailProvider.GENERIC_IMAP
                            else "graph_received_date_time",
                        )
                        for message_id in ("1", "2")
                    }
                )
                old = MailTarget(account, account.mailboxes[0], "old-folder")
                source.targets = Mock(return_value=[old])
                service.source_registry = Registry(source)
                rule = Rule("Archive", targets=[RuleTarget(str(self.root / account.id))])
                self.app.save_rules([rule])
                operation_id = service.prepare_range_operation(
                    self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
                )
                service.run_range_operation(operation_id)
                run = service.operations.manual_run_for_source(
                    operation_id, account.mailboxes[0].id
                )
                self.assertEqual(json.loads(run["selection_json"])["folders"], ["old-folder"])
                self.assertEqual(source.enumerated, ["1"])
                self.revoke_credentials(account, manager)
                self.reauthorize_and_save(account)
                source.targets.reset_mock()
                source.targets.return_value = [
                    old,
                    MailTarget(account, account.mailboxes[0], "new-folder"),
                ]
                terminal, _events = self.retry_operation(operation_id)
                self.assertEqual(terminal.state, ExecutionState.COMPLETED)
                source.targets.assert_not_called()
                resumed = service.operations.manual_run_for_source(
                    operation_id, account.mailboxes[0].id
                )
                self.assertEqual(resumed["selection_json"], run["selection_json"])
                self.assertEqual(resumed["status"], "completed")
                self.assertEqual(source.enumerated, ["1", "2"])
                self.assertEqual(len(list((self.root / account.id).glob("*.eml"))), 2)
                service.source_registry = registry

    def test_real_graph_refresh_failure_stops_before_the_next_folder(self):
        submission = self.new_submission()
        account = submission.account
        account.client_id = "00000000-0000-0000-0000-000000000001"
        account.tenant_id = "12345678-1234-1234-1234-123456789abc"
        account.mailboxes[0].folders = ["INBOX", "Archive"]
        self.app.save_account(submission)
        self.wait_tasks()
        self.authorize(account)
        service = self.app._context.execution.service
        source = service.source_registry.get(account)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        operation_id = service.prepare_range_operation(
            self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
        )
        service.operations.interrupt_queued_manual_operations()
        transport = MicrosoftRequestsTransport(
            {"error": "invalid_grant", "error_description": "The grant was revoked"}
        )
        with (
            patch(
                "requests.sessions.Session.request", autospec=True, side_effect=transport.request
            ),
            patch.object(source, "search_messages", wraps=source.search_messages) as search,
        ):
            terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(
            self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
        )
        self.assertEqual([call.args[0].folder for call in search.call_args_list], ["INBOX"])
        self.assertEqual(sum(method == "POST" for method, _url, _timeout in transport.requests), 1)

    def test_automatic_scan_stops_folders_when_credentials_are_revoked(self):
        for provider in (MailProvider.MICROSOFT_GRAPH, MailProvider.GENERIC_IMAP):
            with self.subTest(provider=provider):
                self._assert_scan_stops_after_credential_failure(provider, legacy=False)

    def test_legacy_resume_stops_folders_when_credentials_are_revoked(self):
        for provider in (MailProvider.MICROSOFT_GRAPH, MailProvider.GENERIC_IMAP):
            with self.subTest(provider=provider):
                self._assert_scan_stops_after_credential_failure(provider, legacy=True)

    def _assert_scan_stops_after_credential_failure(self, provider, *, legacy):
        account = self.save_account(provider)
        self.authorize(account)
        account = deepcopy(account)
        account.mailboxes[0].folders = ["INBOX", "Archive"]
        self.app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)
        self.wait_tasks()
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / account.id))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        manager = service.source_registry.get(account).oauth
        source = EmptyRangeSource({})

        def fail(*_args, **_kwargs):
            self.revoke_credentials(account, manager)
            raise AuthorizationRequiredError("Revoked grant")

        method = "search_messages" if legacy else "fetch_messages"
        with (
            patch.object(service, "source_registry", Registry(source)),
            patch.object(source, method, side_effect=fail) as remote,
        ):
            if legacy:
                settings = self.app.settings
                revision = service.configuration.prepare_run_settings(settings)
                run_id = service.operations.start_run(
                    account.mailboxes[0].id,
                    "manual",
                    {"folders": ["INBOX", "Archive"], "start_utc": None, "end_utc": None},
                    settings,
                    revision,
                )
                service.operations.finish_run(run_id, error="Interrupted provider search")
                result = service.resume_range_run(run_id)
                self.assertEqual(service.operations.run_status(run_id), "failed")
                self.assertIsNone(service.active_range_run_id)
            else:
                result = service.run_once(self.app.settings, {account.id})[0]
            self.assertEqual(result.failed, 1)
            self.assertEqual([call.args[0].folder for call in remote.call_args_list], ["INBOX"])
        self.assertEqual(
            self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
        )

    def test_folder_failure_and_transient_failure_allow_the_other_folder(self):
        account = self.save_account()
        self.authorize(account)
        account = deepcopy(account)
        account.mailboxes[0].folders = ["INBOX", "Archive"]
        self.app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)
        self.wait_tasks()
        for error in (MailboxError("Folder unavailable"), RuntimeError("Network unavailable")):
            with self.subTest(error=error):
                source = EmptyRangeSource({})
                service = self.app._context.execution.service
                service.source_registry = Registry(source)
                rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
                self.app.save_rules([rule])
                operation_id = service.prepare_range_operation(
                    self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
                )
                service.operations.interrupt_queued_manual_operations()
                original = source.search_messages

                def search(target, *args, error=error, original=original, **kwargs):
                    if target.folder == "INBOX":
                        raise error
                    return original(target, *args, **kwargs)

                with patch.object(source, "search_messages", side_effect=search) as searches:
                    terminal, _events = self.retry_operation(operation_id)
                self.assertEqual(terminal.state, ExecutionState.FAILED)
                self.assertEqual(
                    [call.args[0].folder for call in searches.call_args_list], ["INBOX", "Archive"]
                )
                self.assertEqual(
                    self.app.account_status(account.id).authorization.state,
                    AuthorizationState.AUTHORIZED,
                )

    def test_waiting_outputs_do_not_hide_authorization_failure_and_continue_locally(self):
        offline = self.root / "offline"
        offline.write_text("Unavailable destination")
        account, service, source, manager, operation_id, run = self.prepare_partial_oauth_operation(
            offline=offline
        )
        self.revoke_credentials(account, manager)
        terminal, _events = self.retry_operation(operation_id)
        operation = service.operations.manual_operation(operation_id)
        self.assertEqual(operation["status"], "waiting")
        self.assertIn("Revoked grant", operation["error"])
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(service.operations.run_status(run["id"]), "failed")
        offline.unlink()
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(len(list((offline / "archive").glob("*.eml"))), 1)
        self.assertEqual(source.enumerated, ["1"])
        self.reauthorize_and_save(account)
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.COMPLETED)
        self.assertEqual(source.enumerated, ["1", "2"])
        self.assertEqual(len(list((offline / "archive").glob("*.eml"))), 2)

    def test_unexpected_scan_finalization_error_closes_run_and_preserves_completed_checkpoint(self):
        self._assert_scan_finalization_recovery("discovery", "unresolved_intakes")

    def test_completion_write_failure_closes_run_and_preserves_completed_checkpoint(self):
        self._assert_scan_finalization_recovery("operations", "finish_run")

    def _assert_scan_finalization_recovery(self, port, method):
        account, service, source, _manager, operation_id, run = (
            self.prepare_partial_oauth_operation()
        )
        with patch.object(
            getattr(service, port), method, side_effect=RuntimeError("Checkpoint unavailable")
        ):
            terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(service.operations.run_status(run["id"]), "interrupted")
        self.assertIsNone(service.active_range_run_id)
        self.assertEqual(source.enumerated, ["1", "2"])
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.COMPLETED)
        self.assertEqual(service.operations.run_status(run["id"]), "completed")
        self.assertEqual(source.enumerated, ["1", "2"])
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 2)

    def test_stopping_resumed_scan_reports_stopped_and_clears_active_run(self):
        account, service, source, _manager, operation_id, run = (
            self.prepare_partial_oauth_operation()
        )
        search = source.search_messages

        def stopping_search(*args, **kwargs):
            scope, messages = search(*args, **kwargs)

            def iterate():
                for remote in messages:
                    self.app.stop_operation(operation_id)
                    yield remote

            return scope, iterate()

        with patch.object(source, "search_messages", side_effect=stopping_search):
            terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.STOPPED)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "stopped")
        self.assertEqual(service.operations.run_status(run["id"]), "cancelled")
        self.assertIsNone(service.active_range_run_id)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)

    def test_graph_retry_with_old_authority_updates_live_status_after_revocation(self):
        self._assert_automatic_retry_revocation(MailProvider.MICROSOFT_GRAPH)

    def test_imap_retry_with_old_authority_updates_live_status_after_revocation(self):
        self._assert_automatic_retry_revocation(MailProvider.GENERIC_IMAP)

    def prepare_retry_with_tenant_change(self, provider):
        submission = self.new_submission(provider)
        submission.account.client_id = "00000000-0000-0000-0000-000000000001"
        self.app.save_account(submission)
        self.wait_tasks()
        original = submission.account
        self.authorize(original)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        frozen = self.app.settings
        revision = service.configuration.prepare_run_settings(frozen)
        source_id = original.mailboxes[0].id
        run_id = service.operations.start_run(
            source_id, "automatic", {"folders": ["INBOX"]}, frozen, revision
        )
        target = MailTarget(original, original.mailboxes[0], "INBOX")
        scope = (
            imap_scope(target, "1") if provider == MailProvider.GENERIC_IMAP else api_scope(target)
        )
        self.assertIsNotNone(
            service.discovery.reserve(
                source_id,
                scope.processing_namespace + "\0" + "1",
                run_id,
                automatic=True,
                scope_key="INBOX",
                remote_id="1",
            )
        )
        current = self.change_tenant(original)
        self.app.save_rules([])
        return current, service

    def _assert_automatic_retry_revocation(self, provider):
        import msal

        current, service = self.prepare_retry_with_tenant_change(provider)
        http = MicrosoftRefreshHttp(
            {"error": "invalid_grant", "error_description": "The refresh token was revoked"}
        )
        with patch(
            "msal.PublicClientApplication",
            partial(msal.PublicClientApplication, http_client=http, instance_discovery=False),
        ):
            results = service.run_once(self.app.settings, set(), force_retry=True)
        self.assertEqual(results[0].failed, 1)
        self.assertEqual(len(http.refreshes), 1)
        self.assertIsNone(self.credentials.get(current.id))
        cached = self.app.account_status(current.id)
        self.assertEqual(cached.state, AccountState.AUTHORIZATION_REQUIRED)
        self.assertEqual(cached.authorization.detail, "The refresh token was revoked")
        self.assertEqual(
            cached.authorization.state,
            OAuthManager(self.credentials).authorization_status(current).state,
        )
        self.assertFalse(cached.allows(AccountAction.CHECK_MAIL))
        self.assertEqual(service.run_once(self.app.settings, set(), force_retry=True), [])
        self.authorize(current)
        self.assertEqual(self.app.account_status(current.id).state, AccountState.WAITING_FOR_RULE)

    def test_old_authority_storage_failure_updates_live_status_and_can_recover(self):
        current, service = self.prepare_retry_with_tenant_change(MailProvider.MICROSOFT_GRAPH)
        previous = self.credentials.get(current.id)
        with patch.object(
            OAuthManager, "_microsoft_access_token", side_effect=CredentialError("Keyring locked")
        ):
            results = service.run_once(self.app.settings, set(), force_retry=True)
        self.assertEqual(results[0].failed, 1)
        self.assertEqual(self.credentials.get(current.id), previous)
        cached = self.app.account_status(current.id)
        self.assertEqual(cached.state, AccountState.CREDENTIALS_UNAVAILABLE)
        self.assertEqual(cached.authorization.detail, "Keyring locked")
        service.account_statuses.refresh(current)
        self.assertEqual(
            self.app.account_status(current.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_public_retry_inspects_compatible_old_authority_on_execution_worker(self):
        original = self.save_account()
        self.authorize(original)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        source = EmptyRangeSource({})
        service.source_registry = Registry(source)
        operation_id = service.prepare_range_operation(
            self.app.settings, {original.mailboxes[0].id}, rule_id=rule.id
        )
        service.operations.interrupt_queued_manual_operations()
        current = self.change_tenant(original)
        inspector = Mock(wraps=service.account_statuses._inspect)
        inspected_threads = []

        def inspect(account):
            inspected_threads.append(threading.current_thread())
            return inspector(account)

        service.account_statuses._inspect = inspect
        self.app.retry_activity("operation:" + operation_id)
        inspector.assert_not_called()
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.COMPLETED)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "completed")
        self.assertEqual(source.folders_seen, ["INBOX"])
        self.assertTrue(inspected_threads)
        self.assertTrue(
            all(thread is not threading.current_thread() for thread in inspected_threads)
        )
        self.assertEqual(
            self.app.account_status(current.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_public_retry_rejects_incompatible_old_identity_on_worker_before_remote_access(self):
        original = self.save_account()
        self.authorize(original)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        operation_id = service.prepare_range_operation(
            self.app.settings, {original.mailboxes[0].id}, rule_id=rule.id
        )
        service.operations.interrupt_queued_manual_operations()
        submission = build_account_submission(
            AccountFormValues(
                label=original.label,
                provider=original.provider,
                auth_mode=original.auth_mode,
                username=original.username,
                client_id="different-client",
                mailboxes=original.mailboxes,
            ),
            existing=original,
        )
        self.app.save_account(submission, replacing_id=original.id)
        self.wait_tasks()
        self.authorize(submission.account)
        source = Mock()
        service.source_registry = Registry(source)
        terminal, events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "failed")
        source.targets.assert_not_called()
        self.assertTrue(
            any(
                event.level == EventLevel.ERROR and "Authorize" in event.message for event in events
            )
        )
        self.assertEqual(
            self.app.account_status(original.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_save_is_separate_from_authorize_for_every_provider(self):
        for provider in (
            MailProvider.GMAIL_API,
            MailProvider.MICROSOFT_GRAPH,
            MailProvider.GENERIC_IMAP,
        ):
            with self.subTest(provider=provider):
                self.app._authorize = Mock()
                account = self.save_account(provider)
                self.app._authorize.assert_not_called()
                self.assertEqual(
                    self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
                )
                self.authorize(account)
                self.assertEqual(
                    self.app.account_status(account.id).state, AccountState.WAITING_FOR_RULE
                )

    def test_unauthed_accounts_never_reach_provider_even_with_global_rule(self):
        account = self.save_account()
        source = FakeSource({})
        service = self.app._context.execution.service
        service.source_registry = Registry(source)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        self.assertIsNone(self.app.check_now())
        for _ in range(2):
            self.assertEqual(service.run_once(self.app.settings), [])
        self.assertEqual(source.folders_seen, [])
        self.assertEqual(self.app.current_jobs(), ())
        self.assertEqual(
            self.app.account_status(account.id), service.account_status(account, self.app.settings)
        )
        with self.assertRaisesRegex(ValueError, "Authorize"):
            self.app.apply_rule_to_past_mail(rule.id, None, None, "UTC")
        self.assertEqual(self.app.current_jobs(), ())
        self.authorize(account)
        self.assertTrue(self.app.account_status(account.id).allows(AccountAction.CHECK_MAIL))
        service.run_once(self.app.settings)
        self.assertTrue(source.folders_seen)

    def test_authorization_keeps_a_manually_paused_account_paused(self):
        account = self.save_account(enabled=False)
        self.authorize(account)
        self.assertEqual(self.app.account_status(account.id).state, AccountState.PAUSED)
        self.assertFalse(self.app.settings.accounts[0].enabled)

    def test_lost_authorization_blocks_remote_retry_and_preserves_operation(self):
        account = self.save_account(MailProvider.GENERIC_IMAP)
        self.authorize(account)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        source = EmptyRangeSource({})
        service.source_registry = Registry(source)
        operation_id = service.prepare_range_operation(
            self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
        )
        service.account_statuses.require_authorization(account)
        results = service.run_range_operation(operation_id)
        self.assertEqual(results[0].failed, 1)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "failed")
        self.assertEqual(source.folders_seen, [])
        terminal, events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertTrue(
            any(
                event.level == EventLevel.ERROR and "Authorize" in event.message for event in events
            )
        )
        self.assertEqual(source.folders_seen, [])
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "failed")
        self.authorize(account)
        service.run_range_operation(operation_id)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "completed")

    def _operation_with_changed_shared_mailbox(self):
        submission = self.new_submission()
        submission.account.mailboxes.append(Mailbox("shared@example.org"))
        self.app.save_account(submission)
        self.wait_tasks()
        self.authorize(submission.account)
        rule = Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        operation_id = service.prepare_range_operation(
            self.app.settings, {submission.account.mailboxes[0].id}, rule_id=rule.id
        )
        current = deepcopy(submission.account)
        current.mailboxes[-1].enabled = False
        self.app.save_account(AccountSubmission(current, {}, False), replacing_id=current.id)
        self.wait_tasks()
        self.assertTrue(
            service.account_status(submission.account, self.app.settings, inspect=True).allows(
                AccountAction.RETRY_REMOTE
            )
        )
        return current, service, operation_id

    def test_revoked_credentials_in_frozen_operation_allow_live_reauthorization(self):
        current, service, operation_id = self._operation_with_changed_shared_mailbox()
        with patch.object(
            OAuthManager,
            "_microsoft_access_token",
            side_effect=AuthorizationRequiredError("The grant was revoked"),
        ):
            results = service.run_range_operation(operation_id)
        self.assertEqual(results[0].failed, 1)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "failed")
        self.assertIsNone(self.credentials.get(current.id))
        self.assertEqual(
            self.app.account_status(current.id).state, AccountState.AUTHORIZATION_REQUIRED
        )
        self.app.account_statuses.refresh(current)
        self.assertEqual(
            self.app.account_status(current.id).state, AccountState.AUTHORIZATION_REQUIRED
        )
        editor = self.app.account_editor(current.id)
        draft = AccountSubmission(current, {}, False)
        self.assertTrue(editor.status(draft).allows(AccountAction.AUTHORIZE))
        editor.authorize(draft)
        self.wait_tasks()
        editor.save(draft)
        self.wait_tasks()
        self.assertTrue(self.app.account_status(current.id).allows(AccountAction.CHECK_MAIL))
        service.source_registry = Registry(EmptyRangeSource({}))
        terminal, _events = self.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.COMPLETED)

    def test_frozen_operation_credential_failure_recovers_after_unlocking_store(self):
        current, service, operation_id = self._operation_with_changed_shared_mailbox()
        with patch.object(
            OAuthManager,
            "_microsoft_access_token",
            side_effect=CredentialError("Keyring locked"),
        ):
            results = service.run_range_operation(operation_id)
        self.assertEqual(results[0].failed, 1)
        self.assertEqual(service.operations.manual_operation(operation_id)["status"], "failed")
        self.assertEqual(
            self.app.account_status(current.id).state, AccountState.CREDENTIALS_UNAVAILABLE
        )
        self.assertEqual(self.app.account_status(current.id).authorization.detail, "Keyring locked")
        self.app.account_statuses.refresh(current)
        self.assertEqual(
            self.app.account_status(current.id).authorization.state, AuthorizationState.AUTHORIZED
        )
        self.assertTrue(self.app.account_status(current.id).allows(AccountAction.CHECK_MAIL))

    def test_failure_and_cancellation_keep_configuration_and_allow_retry(self):
        account = self.save_account()
        self.app._authorize = Mock(side_effect=TimeoutError("Browser sign-in timed out"))
        results = []
        self.app.authorize_account(account.id, on_complete=results.append)
        self.wait_tasks()
        self.assertEqual(results[0].outcome, AuthorizationOutcome.FAILED)
        self.assertEqual(
            self.app.account_status(account.id).authorization.detail, "Browser sign-in timed out"
        )
        entered = threading.Event()

        def wait_for_cancel(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(THREAD_TIMEOUT))

        self.app._authorize = wait_for_cancel
        self.app.authorize_account(account.id, on_complete=results.append)
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.assertEqual(self.app.account_status(account.id).state, AccountState.AUTHORIZING)
        self.assertFalse(self.app.authorize_account(account.id))
        self.app.cancel_authorization(account.id)
        self.wait_tasks()
        self.assertEqual(results[-1].outcome, AuthorizationOutcome.CANCELLED)
        self.assertEqual(
            self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
        )
        self.assertEqual(len(self.app.settings.accounts), 1)
        self.authorize(account)

    def test_cancelled_late_credential_write_restores_previous_authorization(self):
        account = self.save_account(MailProvider.GMAIL_API)
        for previously_authorized in (False, True):
            with self.subTest(previously_authorized=previously_authorized):
                if previously_authorized:
                    self.authorize(account)
                previous = self.credentials.get(account.id)
                entered, release = threading.Event(), threading.Event()

                def late_grant(
                    account, credentials, *, cancelled, entered=entered, release=release
                ):
                    entered.set()
                    self.assertTrue(release.wait(THREAD_TIMEOUT))
                    self.grant(account, credentials, cancelled=cancelled)

                self.app._authorize = late_grant
                self.app.authorize_account(account.id)
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                with self.assertRaisesRegex(RuntimeError, "authorization first"):
                    self.app.save_account(
                        AccountSubmission(account, {}, False), replacing_id=account.id
                    )
                self.app.cancel_authorization(account.id)
                release.set()
                self.wait_tasks()
                self.assertEqual(self.credentials.get(account.id), previous)
                expected = (
                    AuthorizationState.AUTHORIZED
                    if previously_authorized
                    else AuthorizationState.REQUIRED
                )
                self.assertEqual(self.app.account_status(account.id).authorization.state, expected)

    def test_rename_preserves_authorization_and_new_shared_grant_requires_it(self):
        account = self.save_account()
        self.authorize(account)
        edited = deepcopy(account)
        edited.label, edited.poll_minutes = "Renamed", 20
        draft = AccountSubmission(edited, {}, False)
        self.assertEqual(
            self.app.preview_account_status(draft).authorization.state,
            AuthorizationState.AUTHORIZED,
        )
        self.app.save_account(draft, replacing_id=account.id)
        self.wait_tasks()
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )
        edited.mailboxes.append(Mailbox("shared@example.org"))
        draft = AccountSubmission(edited, {}, False)
        self.assertEqual(
            self.app.preview_account_status(draft).authorization.state, AuthorizationState.REQUIRED
        )
        self.app.save_account(draft, replacing_id=account.id)
        self.wait_tasks()
        self.assertEqual(
            self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
        )
        self.authorize(edited)

    def test_restart_recovers_authorization_from_protected_credentials(self):
        account = self.save_account(MailProvider.GMAIL_API)
        self.authorize(account)
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            restarted = create_application(self.store, self.credentials)
        self.addCleanup(restarted.close)
        restarted.start()
        self.assertTrue(restarted._background.wait(THREAD_TIMEOUT))
        self.assertEqual(restarted.account_status(account.id).state, AccountState.WAITING_FOR_RULE)

    def test_failed_profile_switch_rechecks_restored_paused_profile_credentials(self):
        account = self.save_account(MailProvider.GMAIL_API)
        self.authorize(account)
        self.app.set_automatic_monitoring_paused(True)
        self.app.start()
        self.wait_tasks()
        original_open = self.app._profiles.open
        invalid_path = self.root / "invalid.sqlite3"

        def open_profile(path, *args):
            if path == invalid_path:
                raise ValueError("Invalid destination profile")
            return original_open(path, *args)

        with patch.object(self.app._profiles, "open", side_effect=open_profile):
            with self.assertRaisesRegex(ValueError, "Invalid destination"):
                self.app.switch_profile(invalid_path)
        self.wait_tasks()
        self.assertTrue(self.app.settings.automatic_monitoring_paused)
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_changed_sign_in_identity_requires_new_credentials_for_every_provider(self):
        for provider in (
            MailProvider.GMAIL_API,
            MailProvider.MICROSOFT_GRAPH,
            MailProvider.GENERIC_IMAP,
        ):
            with self.subTest(provider=provider):
                account = self.save_account(provider)
                self.authorize(account)
                submission = build_account_submission(
                    AccountFormValues(
                        label=account.label,
                        provider=provider,
                        auth_mode=AuthMode.OAUTH_USER,
                        username="replacement@example.org",
                        client_id=account.client_id,
                        mailboxes=[Mailbox("replacement@example.org", ["INBOX"])],
                    ),
                    existing=account,
                )
                self.assertTrue(submission.replace_credentials)
                self.app.save_account(submission, replacing_id=account.id)
                self.wait_tasks()
                self.assertEqual(
                    self.app.account_status(account.id).state, AccountState.AUTHORIZATION_REQUIRED
                )
                self.authorize(submission.account)
                self.assertEqual(
                    self.app.account_status(account.id).authorization.state,
                    AuthorizationState.AUTHORIZED,
                )

    def test_tenant_domain_sign_in_is_saved_and_recovers_locally_for_graph_and_imap(self):
        import msal

        from tests.test_oauth import FakeBrowserAuthorization, FakeMsalModule

        realm = "12345678-1234-1234-1234-123456789abc"

        def authorize(account, credentials, *, cancelled):
            canonical = deepcopy(account)
            canonical.tenant_id = realm
            scopes = (
                [MICROSOFT_MAIL_READ_SCOPE]
                if account.provider == MailProvider.MICROSOFT_GRAPH
                else [MICROSOFT_IMAP_ACCESS_SCOPE]
            )
            module = FakeMsalModule(
                accounts=[{"username": account.username, "realm": realm}],
                interactive_hook=lambda: module.applications[-1].token_cache.deserialize(
                    microsoft_cache(canonical, scopes)
                ),
            )
            module.SerializableTokenCache = msal.SerializableTokenCache
            with patch(
                "mailarchive.infrastructure.oauth.BrowserAuthorization", FakeBrowserAuthorization
            ):
                OAuthManager(
                    credentials, cancelled=cancelled, microsoft_msal_module=module
                ).authorize_microsoft(account)

        for provider in (MailProvider.MICROSOFT_GRAPH, MailProvider.GENERIC_IMAP):
            with self.subTest(provider=provider):
                submission = self.new_submission(provider)
                submission.account.tenant_id = "Contoso.onmicrosoft.com"
                self.app._authorize = authorize
                editor = self.app.account_editor()
                editor.authorize(submission)
                self.wait_tasks()
                self.assertEqual(
                    editor.result_for(submission).outcome, AuthorizationOutcome.COMPLETED
                )
                self.assertEqual(
                    editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
                )
                self.assertIsNone(self.credentials.get(submission.account.id))
                editor.save(submission)
                self.wait_tasks()
                self.app.set_automatic_monitoring_paused(True)
                self.assertTrue(self.app.close())
                with patch("mailarchive.bootstrap.set_start_at_login"):
                    self.app = create_application(self.store, self.credentials)
                self.addCleanup(self.app.close)
                with patch("msal.PublicClientApplication") as network_client:
                    self.app.start()
                    self.wait_tasks()
                    self.assertEqual(
                        self.app.account_status(submission.account.id).authorization.state,
                        AuthorizationState.AUTHORIZED,
                    )
                    network_client.assert_not_called()
                wrong_tenant = deepcopy(submission.account)
                wrong_tenant.tenant_id = "other.onmicrosoft.com"
                self.assertEqual(
                    OAuthManager(self.credentials).authorization_status(wrong_tenant).state,
                    AuthorizationState.REQUIRED,
                )

    def test_unsaved_authorization_is_isolated_until_save_for_every_provider(self):
        self.app._authorize = self.grant
        for provider in (
            MailProvider.GMAIL_API,
            MailProvider.MICROSOFT_GRAPH,
            MailProvider.GENERIC_IMAP,
        ):
            with self.subTest(provider=provider):
                submission = self.new_submission(provider)
                before = self.app.settings
                editor = self.app.account_editor()
                self.assertTrue(editor.authorize(submission))
                self.wait_tasks()
                self.assertEqual(
                    editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
                )
                self.assertEqual(self.app.settings, before)
                self.assertIsNone(self.credentials.get(submission.account.id))
                submission.account.label, submission.account.poll_minutes = "Renamed draft", 20
                self.assertEqual(
                    editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
                )
                editor.save(submission)
                self.wait_tasks()
                saved = next(a for a in self.app.settings.accounts if a.id == submission.account.id)
                self.assertEqual((saved.label, saved.poll_minutes), ("Renamed draft", 20))
                self.assertEqual(
                    self.app.account_status(saved.id).state, AccountState.WAITING_FOR_RULE
                )

    def test_edit_authorization_does_not_replace_live_credentials_before_save(self):
        account = self.save_account(MailProvider.GMAIL_API, enabled=False)
        self.authorize(account)
        before = self.app.settings
        previous = self.credentials.get(account.id)
        submission = build_account_submission(
            AccountFormValues(
                label="Changed",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_USER,
                username="replacement@example.org",
                client_id="replacement-client",
                mailboxes=[Mailbox("replacement@example.org", ["INBOX"])],
                enabled=False,
            ),
            existing=account,
        )
        editor = self.app.account_editor(account.id)
        editor.authorize(submission)
        self.wait_tasks()
        self.assertEqual(
            editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
        )
        self.assertEqual(self.app.settings, before)
        self.assertEqual(self.credentials.get(account.id), previous)
        self.assertEqual(self.app.account_status(account.id).state, AccountState.PAUSED)
        editor.save(submission)
        self.wait_tasks()
        self.assertEqual(self.app.settings.accounts[0].username, "replacement@example.org")
        self.assertNotEqual(self.credentials.get(account.id), previous)
        self.assertEqual(self.app.account_status(account.id).state, AccountState.PAUSED)

    def test_changed_draft_identity_does_not_save_an_unrelated_grant(self):
        submission = self.new_submission(MailProvider.GMAIL_API)
        editor = self.app.account_editor()
        self.app._authorize = self.grant
        editor.authorize(submission)
        self.wait_tasks()
        submission.account.client_id = "different-client"
        self.assertEqual(editor.status(submission).authorization.state, AuthorizationState.REQUIRED)
        editor.save(submission)
        self.wait_tasks()
        self.assertIsNone(self.credentials.get(submission.account.id))
        self.assertEqual(
            self.app.account_status(submission.account.id).state,
            AccountState.AUTHORIZATION_REQUIRED,
        )

    def test_failed_draft_save_keeps_authorization_for_retry(self):
        submission = self.new_submission(MailProvider.GMAIL_API)
        editor = self.app.account_editor()
        self.app._authorize = self.grant
        editor.authorize(submission)
        self.wait_tasks()
        with patch.object(self.app._context, "save", side_effect=OSError("Disk full")):
            with self.assertRaisesRegex(OSError, "Disk full"):
                editor.save(submission)
        self.assertEqual(self.app.settings.accounts, [])
        self.assertIsNone(self.credentials.get(submission.account.id))
        self.assertEqual(
            editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
        )
        editor.save(submission)
        self.wait_tasks()
        self.assertEqual(
            self.app.account_status(submission.account.id).state, AccountState.WAITING_FOR_RULE
        )

    def test_draft_cancel_and_late_reply_do_not_publish_credentials(self):
        submission = self.new_submission(MailProvider.GMAIL_API)
        editor = self.app.account_editor()
        entered, release = threading.Event(), threading.Event()

        def late_grant(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            self.grant(account, credentials, cancelled=cancelled)

        self.app._authorize = late_grant
        editor.authorize(submission)
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.assertFalse(editor.authorize(submission))
        with self.assertRaisesRegex(RuntimeError, "cancel authorization"):
            editor.save(submission)
        editor.cancel_authorization()
        release.set()
        self.wait_tasks()
        self.assertEqual(editor.result_for(submission).outcome, AuthorizationOutcome.CANCELLED)
        self.assertEqual(editor.status(submission).authorization.state, AuthorizationState.REQUIRED)
        self.assertEqual(self.app.settings.accounts, [])
        self.assertIsNone(self.credentials.get(submission.account.id))
        self.app._authorize = self.grant
        editor.authorize(submission)
        self.wait_tasks()
        editor.close()
        self.assertEqual(self.app.settings.accounts, [])
        self.assertIsNone(self.credentials.get(submission.account.id))
        with self.assertRaisesRegex(RuntimeError, "editor is closed"):
            editor.save(submission)

    def test_closed_editor_discards_late_reply(self):
        submission = self.new_submission()
        editor = self.app.account_editor()
        entered, release = threading.Event(), threading.Event()

        def late_grant(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            self.grant(account, credentials, cancelled=cancelled)

        self.app._authorize = late_grant
        editor.authorize(submission)
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        editor.close()
        release.set()
        self.wait_tasks()
        self.assertIsNone(editor.result_for(submission))
        self.assertEqual(self.app.settings.accounts, [])
        self.assertIsNone(self.credentials.get(submission.account.id))

    def test_cancel_and_close_interrupt_a_draft_waiting_for_live_credentials(self):
        for provider in MailProvider:
            account = self.save_account(provider)
            self.authorize(account)
            for close in (False, True):
                with self.subTest(provider=provider, close=close):
                    retained = self.credentials.get(account.id)
                    editor = self.app.account_editor(account.id)
                    submission = AccountSubmission(account, {}, False)
                    waiting = threading.Event()
                    lock = account_credential_lock(account.id)
                    self.app._authorize = Mock(wraps=self.grant)
                    with (
                        lock,
                        patch(
                            "mailarchive.application.account_edit.account_credential_lock",
                            return_value=ObservedLock(lock, waiting),
                        ),
                        patch.object(self.credentials, "get", wraps=self.credentials.get) as read,
                    ):
                        self.assertTrue(editor.authorize(submission))
                        self.assertTrue(waiting.wait(THREAD_TIMEOUT))
                        if close:
                            editor.close()
                        else:
                            editor.cancel_authorization()
                        self.assertTrue(self.app._background.wait(1))
                        read.assert_not_called()
                        self.app._authorize.assert_not_called()
                    self.app.dispatch_callbacks()
                    self.assertEqual(self.credentials.get(account.id), retained)
                    if close:
                        self.assertIsNone(editor.result_for(submission))
                    else:
                        self.assertEqual(
                            editor.result_for(submission).outcome, AuthorizationOutcome.CANCELLED
                        )
                        self.assertTrue(editor.authorize(submission))
                        self.wait_tasks()
                        self.assertEqual(
                            editor.status(submission).authorization.state,
                            AuthorizationState.AUTHORIZED,
                        )
                        editor.close()

    def test_closed_editor_finishes_while_a_real_msal_refresh_is_still_waiting(self):
        from requests.exceptions import Timeout

        submission = self.new_submission()
        account = submission.account
        account.client_id = "00000000-0000-0000-0000-000000000001"
        account.tenant_id = "12345678-1234-1234-1234-123456789abc"
        self.app.save_account(submission)
        self.wait_tasks()
        self.authorize(account)
        manager = self.app._context.execution.service.source_registry.get(account).oauth
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def token_request():
            entered.set()
            if not release.wait(THREAD_TIMEOUT * 2):
                raise AssertionError("The controlled token request was not released")
            raise Timeout("Token request timed out")

        def refresh():
            try:
                manager.microsoft_access_token(account, force_refresh=True)
            except Timeout as exc:
                errors.append(exc)

        transport = MicrosoftRequestsTransport({}, token_request=token_request)
        worker = threading.Thread(target=refresh)
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            worker.start()
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                editor = self.app.account_editor(account.id)
                self.app._authorize = Mock(wraps=self.grant)
                with patch(
                    "mailarchive.application.account_edit.account_credential_lock",
                    side_effect=lambda account_id: ObservedLock(
                        account_credential_lock(account_id), waiting
                    ),
                ):
                    self.assertTrue(editor.authorize(AccountSubmission(account, {}, False)))
                    self.assertTrue(waiting.wait(THREAD_TIMEOUT))
                    editor.close()
                    self.assertTrue(self.app._background.wait(1))
                    self.assertTrue(worker.is_alive())
                    self.app._authorize.assert_not_called()
            finally:
                release.set()
                worker.join(THREAD_TIMEOUT)
                self.wait_tasks()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(editor.result_for(submission))
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_closed_editors_release_temporary_credential_locks(self):
        references = []

        def grant(account, credentials, *, cancelled):
            lock = account_credential_lock(account.id)
            references.append(weakref.ref(lock))
            self.grant(account, credentials, cancelled=cancelled)

        self.app._authorize = grant
        for _ in range(3):
            editor = self.app.account_editor()
            editor.authorize(self.new_submission())
            self.wait_tasks()
            editor.close()
        self.assertTrue(all(reference() is None for reference in references))

    def test_draft_timeout_is_retryable_without_saving(self):
        submission = self.new_submission()
        editor = self.app.account_editor()
        self.app._authorize = Mock(side_effect=TimeoutError("Browser sign-in timed out"))
        editor.authorize(submission)
        self.wait_tasks()
        self.assertEqual(
            editor.status(submission).authorization.detail, "Browser sign-in timed out"
        )
        self.assertEqual(editor.result_for(submission).outcome, AuthorizationOutcome.FAILED)
        self.assertEqual(self.app.settings.accounts, [])
        self.app._authorize = self.grant
        editor.authorize(submission)
        self.wait_tasks()
        self.assertEqual(
            editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_cancelled_reauthorization_observes_a_revoked_live_grant(self):
        account = self.save_account()
        self.authorize(account)
        editor = self.app.account_editor(account.id)
        submission = AccountSubmission(account, {}, False)
        entered = threading.Event()

        def wait_for_cancel(account, credentials, *, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(THREAD_TIMEOUT))

        self.app._authorize = wait_for_cancel
        editor.authorize(submission)
        try:
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.app.account_statuses.require_authorization(account, "The grant was revoked.")
        finally:
            editor.cancel_authorization()
        self.wait_tasks()
        self.assertEqual(editor.result_for(submission).outcome, AuthorizationOutcome.CANCELLED)
        self.assertEqual(
            editor.status(submission).authorization,
            self.app.account_status(account.id).authorization,
        )

    def test_saved_shared_grant_stays_authorized_when_shared_mailbox_is_disabled(self):
        submission = self.new_submission()
        submission.account.mailboxes.append(Mailbox("shared@example.org"))
        self.app.save_account(submission)
        self.wait_tasks()
        self.authorize(submission.account)
        editor = self.app.account_editor(submission.account.id)
        draft = deepcopy(submission)
        draft.account.mailboxes[-1].enabled = False
        self.assertEqual(editor.status(draft).authorization.state, AuthorizationState.AUTHORIZED)

    def test_reenabling_saved_shared_mailbox_keeps_its_existing_grant_after_restart(self):
        submission = self.new_submission()
        submission.account.mailboxes.append(Mailbox("shared@example.org"))
        self.app.save_account(submission)
        self.wait_tasks()
        self.authorize(submission.account)
        draft = deepcopy(submission)
        draft.account.mailboxes[-1].enabled = False
        self.app.save_account(draft, replacing_id=draft.account.id)
        self.wait_tasks()
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            self.app = create_application(self.store, self.credentials)
        self.addCleanup(self.app.close)
        self.app.start()
        self.wait_tasks()
        editor = self.app.account_editor(draft.account.id)
        draft.account.mailboxes[-1].enabled = True
        self.assertEqual(editor.status(draft).authorization.state, AuthorizationState.AUTHORIZED)
        editor.save(draft)
        self.wait_tasks()
        self.assertEqual(
            self.app.account_status(draft.account.id).authorization.state,
            AuthorizationState.AUTHORIZED,
        )

    def test_draft_grant_requires_added_shared_access_but_survives_removing_it(self):
        submission = self.new_submission()
        editor = self.app.account_editor()
        self.app._authorize = self.grant
        editor.authorize(submission)
        self.wait_tasks()
        submission.account.mailboxes.append(Mailbox("shared@example.org"))
        self.assertEqual(editor.status(submission).authorization.state, AuthorizationState.REQUIRED)
        editor.authorize(submission)
        self.wait_tasks()
        submission.account.mailboxes.pop()
        self.assertEqual(
            editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
        )
        editor.save(submission)
        self.wait_tasks()
        self.assertEqual(
            self.app.account_status(submission.account.id).state, AccountState.WAITING_FOR_RULE
        )

    def test_unsaved_grant_preserves_verified_shared_capability_when_mailbox_is_reenabled(self):
        submission = self.new_submission()
        submission.account.mailboxes.append(Mailbox("shared@example.org", enabled=False))
        self.app._authorize = self.grant
        editor = self.app.account_editor()
        editor.authorize(submission)
        self.wait_tasks()
        submission.account.mailboxes[-1].enabled = True
        self.assertEqual(
            editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
        )
        editor.save(submission)
        self.wait_tasks()
        self.assertEqual(
            self.app.account_status(submission.account.id).authorization.state,
            AuthorizationState.AUTHORIZED,
        )
