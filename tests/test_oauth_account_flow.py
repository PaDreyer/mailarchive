"""OAuth onboarding and readiness through the real application and profile."""

import tempfile
import threading
import unittest
import weakref
from copy import deepcopy
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
from tests.concurrency import THREAD_TIMEOUT
from tests.oauth_fixture import microsoft_cache
from tests.test_restart_core import FakeSource, Registry


class EmptyRangeSource(FakeSource):
    def search_messages(self, target, should_fetch, start, end, *, range_sync, cancellation):
        scope, messages = self.fetch_messages(target, should_fetch, cancellation=cancellation)
        range_sync.start(scope.processing_namespace)
        range_sync.finish()
        return scope, messages


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
        with self.assertRaisesRegex(ValueError, "Authorize"):
            self.app.retry_activity("operation:" + operation_id)
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
        service.require_operation_authorization(operation_id)
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
        service.require_operation_authorization(operation_id)

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
