"""Credential failures at real provider boundaries stop subsequent remote reads."""

import imaplib
import json
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import update_credential_data
from mailarchive.application.account_status import (
    AccountAction,
    AuthorizationState,
    AuthorizationStatus,
)
from mailarchive.application.cancellation import Cancellation, ProcessingStopped
from mailarchive.application.credential_port import CredentialError
from mailarchive.application.events import EventLevel, ExecutionState
from mailarchive.application.intake_limits import SpoolCapacityError
from mailarchive.application.source_port import ScanWideProviderError
from mailarchive.domain.configuration import Mailbox, MailProvider, Rule, RuleTarget
from mailarchive.domain.source_identity import MailTarget, api_scope, imap_scope
from mailarchive.infrastructure.providers.http import ProviderHttpError
from mailarchive.infrastructure.providers.imap_client import MESSAGE_CHUNK_BYTES
from tests import test_oauth_account_flow as flow
from tests.concurrency import THREAD_TIMEOUT
from tests.oauth_fixture import MicrosoftRequestsTransport
from tests.test_imap_client import FakeImapConnection
from tests.test_restart_core import raw_mail


class OAuthRemoteAccessTests(unittest.TestCase):
    def setUp(self):
        self.profile = flow.OAuthAccountFlowTests()
        self.profile.setUp()
        self.addCleanup(self.profile.doCleanups)
        self.app = self.profile.app
        self.service = self.app._context.execution.service

    def microsoft_account(self, provider=MailProvider.MICROSOFT_GRAPH):
        submission = self.profile.new_submission(provider)
        account = submission.account
        account.client_id = "00000000-0000-0000-0000-000000000001"
        account.tenant_id = "12345678-1234-1234-1234-123456789abc"
        self.app.save_account(submission)
        self.profile.wait_tasks()
        self.profile.authorize(account)
        return account

    def operation(self, account):
        rule = Rule("Archive", targets=[RuleTarget(str(self.profile.root / "archive"))])
        self.app.save_rules([rule])
        operation_id = self.service.prepare_range_operation(
            self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
        )
        self.service.operations.interrupt_queued_manual_operations()
        return operation_id

    def google_account(self):
        account = self.profile.save_account(MailProvider.GMAIL_API)
        self.profile.authorize(account)
        update_credential_data(
            self.profile.credentials,
            account.id,
            google_credentials={
                "client_id": account.client_id,
                "client_secret": "synthetic-secret",
                "refresh_token": "synthetic-refresh",
                "token": "synthetic-access",
                "expiry": "2099-01-01T00:00:00Z",
                "account": account.username,
                "scopes": [flow.GOOGLE_GMAIL_READONLY_SCOPE],
            },
        )
        return account

    def unfinished_automatic_intake(self, account):
        self.app.save_rules(
            [Rule("Archive", targets=[RuleTarget(str(self.profile.root / "archive"))])]
        )
        settings = self.app.settings
        revision = self.service.configuration.prepare_run_settings(settings)
        mailbox = account.mailboxes[0]
        run_id = self.service.operations.start_run(
            mailbox.id, "automatic", {"folders": ["INBOX"]}, settings, revision
        )
        target = MailTarget(account, mailbox, "INBOX")
        scope = (
            imap_scope(target, "9001")
            if account.provider == MailProvider.GENERIC_IMAP
            else api_scope(target)
        )
        intake_id = self.service.discovery.reserve(
            mailbox.id,
            scope.processing_namespace + "\0" + "1"
            if account.provider == MailProvider.GENERIC_IMAP
            else "1",
            run_id,
            automatic=True,
            scope_key="gmail-mailbox" if account.provider == MailProvider.GMAIL_API else "INBOX",
            remote_id="1",
        )
        self.assertIsNotNone(intake_id)
        self.service.discovery.mark_intake_error(intake_id, "Initial download failed")
        self.service.operations.finish_run(run_id, error="Initial download failed")
        self.app.save_rules([])
        return self.service.discovery.pending_automatic_intakes()[0]

    def stop_running_download(self, account, entered, release):
        operation_id = self.operation(account)
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
        try:
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.app.stop_operation(operation_id)
            self.assertEqual(
                self.service.operations.manual_operation(operation_id)["status"], "stopping"
            )
        finally:
            release.set()
        self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.assertEqual(progress[0].state, ExecutionState.STOPPED)
        self.assertEqual(
            self.service.operations.manual_operation(operation_id)["status"], "stopped"
        )
        run = self.service.operations.manual_run_for_source(operation_id, account.mailboxes[0].id)
        self.assertEqual(run["status"], "cancelled")
        scope_key = "gmail-mailbox" if account.provider == MailProvider.GMAIL_API else "INBOX"
        self.assertFalse(
            self.service.operations.range_target_checkpoint(run["id"], scope_key)["complete"]
        )
        self.assertIsNone(self.service.active_range_run_id)
        self.assertEqual(self.service.discovery.unresolved_intakes(run["id"]), [])
        self.assertEqual(list(self.service.engine.spool.path.glob("*")), [])
        self.assertEqual(list((self.profile.root / "archive").glob("*.eml")), [])
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )
        self.assertFalse(any(event.level == EventLevel.ERROR for event in events))

    def test_graph_past_mail_stop_closes_the_download(self):
        self._assert_api_download_stop(MailProvider.MICROSOFT_GRAPH)

    def test_google_past_mail_stop_closes_the_download(self):
        self._assert_api_download_stop(MailProvider.GMAIL_API)

    def _assert_api_download_stop(self, provider):
        account = (
            self.google_account()
            if provider == MailProvider.GMAIL_API
            else self.microsoft_account()
        )
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        chunks, urls = [], []
        raw = raw_mail()

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                urls.append(url)
                if provider == MailProvider.MICROSOFT_GRAPH:
                    return {
                        "value": [
                            {"id": key, "receivedDateTime": "2026-01-01T00:00:00Z"}
                            for key in ("1", "2")
                        ]
                    }
                if "/messages?" in url:
                    return {"messages": [{"id": key} for key in ("1", "2")]}
                return {"internalDate": "1767225600000", "labelIds": ["INBOX"]}

            def iter_bytes(self, url, token, headers=None, **kwargs):
                urls.append(url)
                try:
                    chunks.append(1)
                    yield raw[:20]
                    entered.set()
                    if not release.wait(THREAD_TIMEOUT):
                        raise AssertionError("The in-flight download was not released")
                    chunks.append(2)
                    yield raw[20:40]
                    chunks.append(3)
                    yield raw[40:]
                finally:
                    closed.set()

            iter_gmail_raw = iter_bytes

        self.service.source_registry.get(account).http = MailHttp()
        transport = MicrosoftRequestsTransport(
            {"access_token": "synthetic-token", "expires_in": 3600}
        )
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            self.stop_running_download(account, entered, release)
        self.assertEqual(chunks, [1, 2])
        self.assertTrue(closed.is_set())
        self.assertFalse(any("/messages/2" in url for url in urls))

    def test_imap_past_mail_stop_closes_the_connection(self):
        account = self.microsoft_account(MailProvider.GENERIC_IMAP)
        entered, release = threading.Event(), threading.Event()
        body_requests = []

        class DownloadConnection(FakeImapConnection):
            def uid(self, command, *arguments):
                response = super().uid(command, *arguments)
                if command == "fetch" and str(arguments[-1]).startswith("(BODY.PEEK[]<"):
                    body_requests.append(arguments[0])
                    entered.set()
                    if not release.wait(THREAD_TIMEOUT):
                        raise AssertionError("The in-flight IMAP read was not released")
                return response

        raw = raw_mail() + b"x" * (MESSAGE_CHUNK_BYTES * 2)
        connection = DownloadConnection(uids=b"1 2", raw_by_uid={b"1": raw, b"2": raw})
        source = self.service.source_registry.get(account)
        transport = MicrosoftRequestsTransport(
            {"access_token": "synthetic-token", "expires_in": 3600}
        )
        with (
            patch(
                "requests.sessions.Session.request", autospec=True, side_effect=transport.request
            ),
            patch.object(source.mailbox, "_connect", return_value=connection),
        ):
            self.stop_running_download(account, entered, release)
        self.assertEqual(body_requests, [b"1"])
        self.assertTrue(connection.closed and connection.logged_out)

    def test_graph_automatic_retry_revocation_counts_one_attempt(self):
        self._assert_automatic_retry_revocation(MailProvider.MICROSOFT_GRAPH)

    def test_google_automatic_retry_revocation_counts_one_attempt(self):
        self._assert_automatic_retry_revocation(MailProvider.GMAIL_API)

    def test_imap_automatic_retry_revocation_counts_one_attempt(self):
        self._assert_automatic_retry_revocation(MailProvider.GENERIC_IMAP)

    def _assert_automatic_retry_revocation(self, provider):
        account = (
            self.google_account()
            if provider == MailProvider.GMAIL_API
            else self.microsoft_account(provider)
        )
        before = self.unfinished_automatic_intake(account)
        self.assertEqual(before["attempts"], 1)
        calls = []

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                calls.append("metadata")
                return {
                    "receivedDateTime": "2026-01-01T00:00:00Z",
                    "internalDate": "1767225600000",
                    "labelIds": ["INBOX"],
                }

            def iter_bytes(self, url, token, headers=None, **kwargs):
                calls.append("body")
                raise ProviderHttpError(401, "Access token expired")
                yield b""

            iter_gmail_raw = iter_bytes

        class ExpiringConnection(FakeImapConnection):
            def uid(self, command, *arguments):
                if command == "fetch" and str(arguments[-1]).startswith("(BODY.PEEK[]<"):
                    calls.append("body")
                    raise imaplib.IMAP4.abort("Session invalidated - AccessTokenExpired")
                return super().uid(command, *arguments)

        def google_request(_session, method, url, **kwargs):
            self.assertEqual(url, "https://oauth2.googleapis.com/token")
            response = requests.Response()
            response.status_code = 400
            response._content = json.dumps({"error": "invalid_grant"}).encode()
            return response

        source = self.service.source_registry.get(account)
        source.http = MailHttp()
        transport = self.revoked_microsoft_transport()
        request = google_request if provider == MailProvider.GMAIL_API else transport.request
        current = datetime(2026, 10, 6, tzinfo=timezone.utc)
        with (
            patch("requests.sessions.Session.request", autospec=True, side_effect=request),
            patch.object(
                self.service.source_registry.sources[MailProvider.GENERIC_IMAP].mailbox,
                "_connect",
                return_value=ExpiringConnection(uids=b"1"),
            ),
            patch("mailarchive.infrastructure.persistence_time.datetime") as clock,
        ):
            clock.now.return_value = current
            results = self.service.run_once(self.app.settings, set(), force_retry=True)
        self.assertEqual((results[0].checked, results[0].failed), (1, 1))
        self.assertEqual(calls.count("body"), 1)
        after = self.service.discovery.pending_automatic_intakes()[0]
        self.assertEqual(after["attempts"], 2)
        self.assertEqual(after["retry_after"], (current + timedelta(seconds=60)).isoformat())
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.REQUIRED
        )
        self.assertEqual(list((self.profile.root / "archive").glob("*.eml")), [])
        retained_calls = list(calls)
        self.assertEqual(self.service.run_once(self.app.settings, set(), force_retry=True), [])
        self.assertEqual(calls, retained_calls)

    def test_scan_wide_fetch_failure_counts_one_attempt(self):
        self._assert_fetch_failure_count(ScanWideProviderError("HTTP 429: rate limited"))

    def test_capacity_failure_before_message_handoff_counts_one_attempt(self):
        self._assert_fetch_failure_count(SpoolCapacityError("The local work queue is full"))

    def test_metadata_failure_before_message_handoff_counts_one_attempt(self):
        self._assert_fetch_failure_count(RuntimeError("Message metadata failed"))

    def _assert_fetch_failure_count(self, error):
        account = self.microsoft_account()
        source = self.service.source_registry.get(account)
        before = self.unfinished_automatic_intake(account)
        with patch.object(source, "fetch_message", side_effect=error):
            results = self.service.run_once(self.app.settings, set(), force_retry=True)
        self.assertEqual(results[0].failed, 1)
        after = self.service.discovery.pending_automatic_intakes()[0]
        self.assertEqual(after["attempts"], before["attempts"] + 1)
        self.assertEqual(after["error"], str(error))

    @staticmethod
    def revoked_microsoft_transport():
        transport = MicrosoftRequestsTransport({})
        requests_seen = []

        def token_request():
            requests_seen.append(True)
            transport.result = (
                {"access_token": "synthetic-token", "expires_in": 3600}
                if len(requests_seen) == 1
                else {"error": "invalid_grant", "error_description": "Refresh grant revoked"}
            )

        transport.token_request = token_request
        return transport

    def test_graph_refresh_revocation_during_body_stops_the_same_folder(self):
        account = self.microsoft_account()
        calls = []
        app = self.app

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                calls.append(("metadata", app.account_status(account.id).authorization.state))
                return {
                    "value": [
                        {"id": key, "receivedDateTime": "2026-01-01T00:00:00Z", "subject": "Mail"}
                        for key in ("1", "2")
                    ]
                }

            def iter_bytes(self, url, token, headers=None, **kwargs):
                calls.append(("body", app.account_status(account.id).authorization.state))
                if "/messages/1/" in url:
                    raise ProviderHttpError(401, "Access token rejected")
                yield raw_mail()

        self.service.source_registry.get(account).http = MailHttp()
        operation_id = self.operation(account)
        transport = self.revoked_microsoft_transport()
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(
            calls,
            [("metadata", AuthorizationState.AUTHORIZED), ("body", AuthorizationState.AUTHORIZED)],
        )
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.REQUIRED
        )
        self.assertEqual(list((self.profile.root / "archive").glob("*.eml")), [])
        run = self.service.operations.manual_run_for_source(operation_id, account.mailboxes[0].id)
        self.assertEqual(run["status"], "failed")
        self.assertFalse(
            self.service.operations.range_target_checkpoint(run["id"], "INBOX")["complete"]
        )
        self.assertIsNone(self.service.active_range_run_id)

    def test_imap_refresh_revocation_during_body_stops_reconnections(self):
        account = self.microsoft_account(MailProvider.GENERIC_IMAP)
        source = self.service.source_registry.get(account)
        connections = []
        app = self.app

        class ExpiringConnection(FakeImapConnection):
            def uid(self, command, *arguments):
                if command == "fetch" and str(arguments[-1]).startswith("(BODY.PEEK[]<"):
                    raise imaplib.IMAP4.abort(
                        "command: UID => Session invalidated - AccessTokenExpired"
                    )
                return super().uid(command, *arguments)

        old, new = ExpiringConnection(uids=b"1 2"), FakeImapConnection(uids=b"1 2")
        clients = iter((old, new))

        def connect(*_args, **_kwargs):
            connections.append(app.account_status(account.id).authorization.state)
            return next(clients)

        operation_id = self.operation(account)
        transport = self.revoked_microsoft_transport()
        with (
            patch(
                "requests.sessions.Session.request", autospec=True, side_effect=transport.request
            ),
            patch.object(source.mailbox, "_connect", side_effect=connect),
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(connections, [AuthorizationState.AUTHORIZED])
        self.assertTrue(old.closed and old.logged_out)
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.REQUIRED
        )
        self.assertIsNone(self.service.active_range_run_id)

    def test_google_refresh_revocation_during_body_stops_the_same_mailbox(self):
        account = self.profile.save_account(MailProvider.GMAIL_API)
        self.profile.authorize(account)
        update_credential_data(
            self.profile.credentials,
            account.id,
            google_credentials={
                "client_id": account.client_id,
                "client_secret": "synthetic-secret",
                "refresh_token": "synthetic-refresh",
                "token": "synthetic-access",
                "expiry": "2099-01-01T00:00:00Z",
                "account": account.username,
                "scopes": [flow.GOOGLE_GMAIL_READONLY_SCOPE],
            },
        )
        calls = []
        app = self.app

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                calls.append(("metadata", app.account_status(account.id).authorization.state))
                if "/messages?" in url:
                    return {"messages": [{"id": key} for key in ("1", "2")]}
                return {"internalDate": "1767225600000", "labelIds": ["INBOX"]}

            def iter_gmail_raw(self, url, token, headers=None, **kwargs):
                calls.append(("body", app.account_status(account.id).authorization.state))
                if "/messages/1?" in url:
                    raise ProviderHttpError(401, "Access token rejected")
                yield raw_mail()

        self.service.source_registry.get(account).http = MailHttp()
        operation_id = self.operation(account)

        def token_request(_session, method, url, **kwargs):
            self.assertEqual(url, "https://oauth2.googleapis.com/token")
            response = requests.Response()
            response.status_code = 400
            response._content = json.dumps({"error": "invalid_grant"}).encode()
            return response

        with patch("requests.sessions.Session.request", autospec=True, side_effect=token_request):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(
            calls,
            [
                ("metadata", AuthorizationState.AUTHORIZED),
                ("metadata", AuthorizationState.AUTHORIZED),
                ("body", AuthorizationState.AUTHORIZED),
            ],
        )
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.REQUIRED
        )
        self.assertEqual(list((self.profile.root / "archive").glob("*.eml")), [])

    def test_deleted_credentials_block_real_provider_and_authority_discovery(self):
        account = self.microsoft_account()
        source = self.service.source_registry.get(account)
        operation_id = self.operation(account)
        self.app.delete_account(account.id)
        transport = MicrosoftRequestsTransport({})
        with (
            patch(
                "requests.sessions.Session.request", autospec=True, side_effect=transport.request
            ),
            patch.object(source, "search_messages", wraps=source.search_messages) as search,
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        search.assert_not_called()
        self.assertEqual(transport.requests, [])
        self.assertEqual(
            self.service.account_status(account, self.app.settings).authorization.state,
            AuthorizationState.REQUIRED,
        )

    def test_account_removal_preserves_partial_run_and_finishes_local_outputs(self):
        offline = self.profile.root / "offline"
        offline.write_text("Unavailable destination")
        account, service, source, _manager, operation_id, run = (
            self.profile.prepare_partial_oauth_operation(offline=offline)
        )
        self.app.delete_account(account.id)
        offline.unlink()
        with patch.object(source, "search_messages", wraps=source.search_messages) as search:
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        search.assert_not_called()
        self.assertEqual(
            dict(service.operations.manual_run_for_source(operation_id, account.mailboxes[0].id)),
            run,
        )
        self.assertEqual(source.enumerated, ["1"])
        self.assertEqual(len(list((offline / "archive").glob("*.eml"))), 1)

    def test_protected_store_failure_in_editor_blocks_remote_access_and_can_be_rechecked(self):
        for provider in MailProvider:
            with self.subTest(provider=provider):
                account = self.profile.save_account(provider)
                self.profile.authorize(account)
                retained = self.profile.credentials.get(account.id)
                editor = self.app.account_editor(account.id)
                submission = AccountSubmission(account, {}, False)
                self.app._authorize = Mock(wraps=self.profile.grant)
                with patch.object(
                    self.profile.credentials, "get", side_effect=CredentialError("Keyring locked")
                ):
                    editor.authorize(submission)
                    self.profile.wait_tasks()
                self.app._authorize.assert_not_called()
                self.assertEqual(
                    editor.status(submission).authorization.state, AuthorizationState.UNAVAILABLE
                )
                self.assertFalse(
                    self.app.account_status(account.id).allows(AccountAction.RETRY_REMOTE)
                )
                self.assertEqual(editor.result_for(submission).detail, "Keyring locked")
                editor.refresh_authorization()
                self.profile.wait_tasks()
                self.app._authorize.assert_not_called()
                self.assertEqual(
                    editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
                )
                self.assertEqual(self.profile.credentials.get(account.id), retained)
                editor.close()

    def test_isolated_sign_in_failure_keeps_live_credentials_authorized(self):
        account = self.profile.save_account()
        self.profile.authorize(account)
        editor = self.app.account_editor(account.id)
        self.app._authorize = Mock(side_effect=CredentialError("Temporary draft unavailable"))
        submission = AccountSubmission(account, {}, False)
        editor.authorize(submission)
        self.profile.wait_tasks()
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )
        self.assertEqual(editor.result_for(submission).detail, "Temporary draft unavailable")
        editor.close()

    def test_account_removal_failure_blocks_retained_remote_work(self):
        account = self.microsoft_account()
        operation_id = self.operation(account)
        self._assert_failed_deletion_blocks_native_retry(account, account, operation_id)

    def test_graph_failed_credential_deletion_blocks_a_retry_with_old_authority(self):
        self._assert_failed_deletion_blocks_old_authority(MailProvider.MICROSOFT_GRAPH)

    def test_imap_failed_credential_deletion_blocks_a_retry_with_old_authority(self):
        self._assert_failed_deletion_blocks_old_authority(MailProvider.GENERIC_IMAP)

    def _assert_failed_deletion_blocks_old_authority(self, provider):
        original = self.profile.save_account(provider)
        self.profile.authorize(original)
        operation_id = self.operation(original)
        current = self.profile.change_tenant(original)
        self._assert_failed_deletion_blocks_native_retry(current, original, operation_id)

    def test_failed_credential_deletion_blocks_a_retry_with_old_mailbox_permissions(self):
        current = self.microsoft_account()
        current.mailboxes.append(Mailbox("shared@example.org", ["INBOX"]))
        self.app.save_account(AccountSubmission(current, {}, False), replacing_id=current.id)
        self.profile.wait_tasks()
        self.profile.authorize(current)
        frozen = deepcopy(current)
        operation_id = self.operation(frozen)
        current.mailboxes[1].enabled = False
        self.app.save_account(AccountSubmission(current, {}, False), replacing_id=current.id)
        self.profile.wait_tasks()
        self._assert_failed_deletion_blocks_native_retry(current, frozen, operation_id)

    def _assert_failed_deletion_blocks_native_retry(self, current, frozen, operation_id):
        retained = self.profile.credentials.get(current.id)
        with patch.object(
            self.profile.credentials, "delete", side_effect=CredentialError("Deletion refused")
        ):
            self.app.delete_account(current.id)
        self.assertEqual(self.profile.credentials.get(current.id), retained)
        source = self.service.source_registry.get(frozen)
        http = Mock()
        http.get_json.side_effect = AssertionError("Remote mail must remain gated")
        transport = MicrosoftRequestsTransport(
            {"access_token": "synthetic-token", "expires_in": 3600}
        )
        with (
            patch(
                "requests.sessions.Session.request", autospec=True, side_effect=transport.request
            ),
            patch.object(source, "search_messages", wraps=source.search_messages) as search,
            patch.object(source, "http", http, create=True),
            patch.object(
                self.service.source_registry.sources[MailProvider.GENERIC_IMAP].mailbox,
                "_connect",
                side_effect=AssertionError("Remote IMAP must remain gated"),
            ),
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        search.assert_not_called()
        self.assertEqual(transport.requests, [])
        for account in (current, frozen):
            status = self.service.account_status(account, self.app.settings, inspect=True)
            self.assertEqual(status.authorization.state, AuthorizationState.UNAVAILABLE)
            self.assertEqual(status.authorization.detail, "Deletion refused")
        self.assertEqual(self.profile.credentials.get(current.id), retained)

    def test_automatic_retry_keeps_saved_rule_when_current_rule_was_removed(self):
        account = self.microsoft_account()
        intake = self.unfinished_automatic_intake(account)
        with self.service.discovery.connection() as db, db:
            db.execute(
                "UPDATE intake SET retry_after='2000-01-01T00:00:00+00:00' WHERE id=?",
                (intake["id"],),
            )
        bodies = []

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                return {"receivedDateTime": "2026-01-01T00:00:00Z"}

            def iter_bytes(self, url, token, headers=None, **kwargs):
                bodies.append(url)
                yield raw_mail()

        self.service.source_registry.get(account).http = MailHttp()
        transport = MicrosoftRequestsTransport(
            {"access_token": "synthetic-token", "expires_in": 3600}
        )
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            self.app._context.execution._poll(False)
        self.assertEqual(self.app.settings.rules, [])
        self.assertEqual(len(bodies), 1)
        self.assertEqual(len(list((self.profile.root / "archive").glob("*.eml"))), 1)
        self.assertEqual(self.service.discovery.pending_automatic_intakes(), [])

    def test_stop_during_retained_intake_capacity_failure_preserves_its_previous_attempt(self):
        account = self.microsoft_account()
        before = dict(self.unfinished_automatic_intake(account))
        self.app.save_rules(
            [Rule("Archive", targets=[RuleTarget(str(self.profile.root / "archive"))])]
        )
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        progress = []

        def on_progress(update):
            if update.origin == "check" and not update.active:
                progress.append(update)
                finished.set()

        def fetch(*args, **kwargs):
            kwargs["cancellation"].checkpoint()
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            raise SpoolCapacityError("The local work queue is full")

        source = self.service.source_registry.get(account)
        self.app.set_observers(lambda event: None, on_progress)
        self.app.set_automatic_monitoring_paused(True)
        with patch.object(source, "fetch_message", side_effect=fetch):
            check_id = self.app.check_now()
            self.assertIsNotNone(check_id)
            self.app._context.execution.start()
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                self.assertTrue(self.app.stop_check(check_id))
            finally:
                release.set()
            self.assertTrue(finished.wait(THREAD_TIMEOUT))
        self.assertEqual(progress[-1].state, ExecutionState.STOPPED)
        self.assertEqual(dict(self.service.discovery.pending_automatic_intakes()[0]), before)

    def test_protected_store_failure_saving_a_grant_preserves_editor_and_updates_live_status(self):
        account = self.microsoft_account()
        editor = self.app.account_editor(account.id)
        submission = AccountSubmission(account, {}, False)
        editor.authorize(submission)
        self.profile.wait_tasks()
        retained = self.profile.credentials.get(account.id)
        with patch.object(
            self.profile.credentials, "get", side_effect=CredentialError("Keyring locked")
        ):
            with self.assertRaises(CredentialError):
                editor.save(submission)
        self.assertEqual(self.profile.credentials.get(account.id), retained)
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.UNAVAILABLE
        )
        self.assertEqual(
            editor.status(submission).authorization.state, AuthorizationState.AUTHORIZED
        )
        editor.save(submission)
        self.profile.wait_tasks()
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_account_status_change_stops_even_empty_provider_pages(self):
        account = self.microsoft_account()
        calls = []
        statuses = self.app.account_statuses

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                calls.append(url)
                statuses.credential_record_failed(
                    account.id, AuthorizationStatus(AuthorizationState.REQUIRED, "Grant revoked")
                )
                return {"value": [], "@odata.nextLink": url + "&skiptoken=next"}

        self.service.source_registry.get(account).http = MailHttp()
        operation_id = self.operation(account)
        transport = MicrosoftRequestsTransport({"access_token": "synthetic", "expires_in": 3600})
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.profile.root / "archive").glob("*.eml")), [])

    def test_credentials_becoming_unavailable_interrupts_chunks_and_releases_the_stream(self):
        account = self.microsoft_account()
        statuses = self.app.account_statuses
        chunks, closed = [], []
        raw = raw_mail()

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                return {"value": [{"id": "1", "receivedDateTime": "2026-01-01T00:00:00Z"}]}

            def iter_bytes(self, url, token, headers=None, **kwargs):
                try:
                    chunks.append(1)
                    yield raw[: len(raw) // 2]
                    statuses.credential_record_failed(
                        account.id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, "Locked")
                    )
                    chunks.append(2)
                    yield raw[len(raw) // 2 :]
                    chunks.append(3)
                finally:
                    closed.append(True)

        self.service.source_registry.get(account).http = MailHttp()
        operation_id = self.operation(account)
        transport = MicrosoftRequestsTransport({"access_token": "synthetic", "expires_in": 3600})
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(chunks, [1, 2])
        self.assertEqual(closed, [True])
        self.assertEqual(list((self.profile.root / "archive").glob("*.eml")), [])
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.UNAVAILABLE
        )

    def test_transient_body_refresh_timeout_keeps_authorization_and_other_messages_usable(self):
        account = self.microsoft_account()
        bodies = []
        app = self.app

        class MailHttp:
            def get_json(self, url, token, headers=None, **kwargs):
                return {
                    "value": [
                        {"id": key, "receivedDateTime": "2026-01-01T00:00:00Z"}
                        for key in ("1", "2")
                    ]
                }

            def iter_bytes(self, url, token, headers=None, **kwargs):
                bodies.append(app.account_status(account.id).authorization.state)
                if "/messages/1/" in url:
                    raise ProviderHttpError(401, "Access token rejected")
                yield raw_mail()

        transport = MicrosoftRequestsTransport({"access_token": "synthetic", "expires_in": 3600})
        requests_seen = []

        def token_request():
            requests_seen.append(True)
            if len(requests_seen) > 1:
                raise requests.exceptions.Timeout("Token endpoint unavailable")

        transport.token_request = token_request
        self.service.source_registry.get(account).http = MailHttp()
        operation_id = self.operation(account)
        with patch(
            "requests.sessions.Session.request", autospec=True, side_effect=transport.request
        ):
            terminal, _events = self.profile.retry_operation(operation_id)
        self.assertEqual(terminal.state, ExecutionState.FAILED)
        self.assertEqual(bodies, [AuthorizationState.AUTHORIZED, AuthorizationState.AUTHORIZED])
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )
        self.assertEqual(len(list((self.profile.root / "archive").glob("*.eml"))), 1)

    def test_remote_checkpoints_cache_permission_until_credential_revision_changes(self):
        account = self.microsoft_account()
        stopped = []
        access = self.service._remote_access(
            account, self.app.settings, Cancellation(lambda: bool(stopped))
        )
        with patch.object(
            self.service, "require_account_action", wraps=self.service.require_account_action
        ) as require:
            for _ in range(10):
                access.checkpoint()
            self.assertEqual(require.call_count, 1)
            self.app.account_statuses.require_authorization(account)
            with self.assertRaises(ValueError):
                access.checkpoint()
            self.assertEqual(require.call_count, 2)
            stopped.append(True)
            with self.assertRaises(ProcessingStopped):
                access.checkpoint()
            self.assertEqual(require.call_count, 2)
