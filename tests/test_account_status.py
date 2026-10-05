"""Shared action policy, cache races, and local provider credential inspection."""

import json
import threading
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

from mailarchive.application.account_credentials import update_credential_data
from mailarchive.application.account_status import (
    AccountAction,
    AccountBlocker,
    AccountState,
    AccountStatusService,
    AuthorizationState,
    AuthorizationStatus,
    account_status,
)
from mailarchive.domain.configuration import Account, AuthMode, Mailbox, MailProvider, Rule
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.oauth import (
    GOOGLE_GMAIL_READONLY_SCOPE,
    MICROSOFT_IMAP_ACCESS_SCOPE,
    MICROSOFT_MAIL_READ_SCOPE,
    MICROSOFT_MAIL_READ_SHARED_SCOPE,
    OAuthManager,
)
from tests.concurrency import THREAD_TIMEOUT
from tests.oauth_fixture import microsoft_cache


def oauth_account(provider=MailProvider.MICROSOFT_GRAPH):
    return Account(
        "Owner",
        username="owner@example.org",
        provider=provider,
        auth_mode=AuthMode.OAUTH_USER,
        client_id="client",
    )


class AccountStatusTests(unittest.TestCase):
    def test_authorization_has_priority_and_preserves_all_other_blockers(self):
        account = oauth_account()
        account.enabled = False
        account.mailboxes[0].enabled = False
        for authorization, state, blocker in (
            (
                AuthorizationState.CHECKING,
                AccountState.CHECKING_AUTHORIZATION,
                AccountBlocker.CHECKING_AUTHORIZATION,
            ),
            (AuthorizationState.AUTHORIZING, AccountState.AUTHORIZING, AccountBlocker.AUTHORIZING),
            (
                AuthorizationState.REQUIRED,
                AccountState.AUTHORIZATION_REQUIRED,
                AccountBlocker.AUTHORIZATION_REQUIRED,
            ),
            (
                AuthorizationState.UNAVAILABLE,
                AccountState.CREDENTIALS_UNAVAILABLE,
                AccountBlocker.CREDENTIALS_UNAVAILABLE,
            ),
        ):
            with self.subTest(authorization=authorization):
                status = account_status(account, [], AuthorizationStatus(authorization))
                self.assertEqual(status.state, state)
                self.assertEqual(
                    status.blockers,
                    {
                        blocker,
                        AccountBlocker.PAUSED,
                        AccountBlocker.NO_ACTIVE_MAILBOXES,
                        AccountBlocker.NO_ACTIVE_RULE,
                    },
                )
                for action in (
                    AccountAction.CHECK_MAIL,
                    AccountAction.READ_PAST_MAIL,
                    AccountAction.RETRY_REMOTE,
                ):
                    self.assertFalse(status.allows(action))
                self.assertEqual(
                    status.allows(AccountAction.CANCEL_AUTHORIZATION),
                    authorization == AuthorizationState.AUTHORIZING,
                )

    def test_action_policy_handles_independent_activation_and_rule_states(self):
        for enabled in (False, True):
            for mailbox_enabled in (False, True):
                for has_rule in (False, True):
                    with self.subTest(enabled=enabled, mailbox=mailbox_enabled, rule=has_rule):
                        account = oauth_account()
                        account.enabled = enabled
                        account.mailboxes[0].enabled = mailbox_enabled
                        status = account_status(
                            account,
                            [Rule("Archive")] if has_rule else [],
                            AuthorizationStatus(AuthorizationState.AUTHORIZED),
                        )
                        self.assertEqual(
                            status.allows(AccountAction.CHECK_MAIL),
                            enabled and mailbox_enabled and has_rule,
                        )
                        self.assertEqual(
                            status.allows(AccountAction.READ_PAST_MAIL), enabled and mailbox_enabled
                        )
                        self.assertTrue(status.allows(AccountAction.RETRY_REMOTE))
                        self.assertTrue(status.allows(AccountAction.AUTHORIZE))
                        self.assertFalse(status.allows(AccountAction.CANCEL_AUTHORIZATION))

    def test_monitoring_attention_does_not_block_other_folders(self):
        account = oauth_account()
        for monitoring, expected in (
            ([], AccountState.ACTIVE),
            (["setting_up"], AccountState.SETTING_UP),
            (["paused", "active"], AccountState.ATTENTION),
        ):
            status = account_status(
                account,
                [Rule("Archive")],
                AuthorizationStatus(AuthorizationState.AUTHORIZED),
                monitoring,
            )
            self.assertEqual(status.state, expected)
            self.assertTrue(status.allows(AccountAction.CHECK_MAIL))
        with self.assertRaises(FrozenInstanceError):
            status.state = AccountState.PAUSED

    def test_non_interactive_authentication_has_no_authorization_actions(self):
        for mode in (AuthMode.PASSWORD, AuthMode.OAUTH_APPLICATION):
            account = oauth_account()
            account.auth_mode = mode
            status = AccountStatusService().resolve(account, [Rule("Archive")])
            self.assertTrue(status.allows(AccountAction.CHECK_MAIL))
            self.assertFalse(status.allows(AccountAction.AUTHORIZE))

    def test_cached_queries_do_not_inspect_credentials_and_edits_invalidate_binding(self):
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect)
        account = oauth_account()
        self.assertEqual(statuses.authorization(account).state, AuthorizationState.CHECKING)
        inspect.assert_not_called()
        statuses.refresh(account)
        inspect.assert_called_once()
        renamed = deepcopy(account)
        renamed.label, renamed.poll_minutes = "Renamed", 10
        self.assertEqual(statuses.authorization(renamed).state, AuthorizationState.AUTHORIZED)
        renamed.username = "other@example.org"
        self.assertEqual(statuses.authorization(renamed).state, AuthorizationState.CHECKING)
        shared = deepcopy(account)
        shared.mailboxes.append(Mailbox("shared@example.org"))
        self.assertEqual(statuses.authorization(shared).state, AuthorizationState.CHECKING)
        self.assertEqual(inspect.call_count, 1)

    def test_late_inspection_cannot_overwrite_authorizing_or_required_state(self):
        entered, release = threading.Event(), threading.Event()

        def inspect(account):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return AuthorizationStatus(AuthorizationState.AUTHORIZED)

        account = oauth_account()
        statuses = AccountStatusService(inspect)
        worker = threading.Thread(target=lambda: statuses.refresh(account))
        worker.start()
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        statuses.set_authorization(account, AuthorizationStatus(AuthorizationState.AUTHORIZING))
        release.set()
        worker.join(THREAD_TIMEOUT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(statuses.authorization(account).state, AuthorizationState.AUTHORIZING)

    def test_inspection_failure_is_unavailable_instead_of_authorized(self):
        statuses = AccountStatusService(Mock(side_effect=RuntimeError("Keyring locked")))
        status = statuses.refresh(oauth_account())
        self.assertEqual(
            (status.state, status.detail), (AuthorizationState.UNAVAILABLE, "Keyring locked")
        )

    def test_inspecting_old_retry_configuration_preserves_the_live_account_status(self):
        account = oauth_account()
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect)
        statuses.refresh(account)
        revision = statuses.revision
        previous = deepcopy(account)
        previous.client_id = "previous-client"
        inspect.return_value = AuthorizationStatus(AuthorizationState.REQUIRED)
        self.assertFalse(
            statuses.resolve(previous, [Rule("Archive")], inspect=True).allows(
                AccountAction.RETRY_REMOTE
            )
        )
        self.assertEqual(statuses.authorization(account).state, AuthorizationState.AUTHORIZED)
        self.assertEqual(statuses.revision, revision)


class CredentialReadinessTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryCredentialStore()
        self.oauth = OAuthManager(self.store)

    def test_google_requires_matching_refreshable_credentials(self):
        account = oauth_account(MailProvider.GMAIL_API)
        for credentials, expected in (
            (None, AuthorizationState.REQUIRED),
            ({"client_id": "client", "token": "expired-access"}, AuthorizationState.REQUIRED),
            ({"client_id": "wrong", "refresh_token": "synthetic"}, AuthorizationState.REQUIRED),
            (
                {"client_id": "client", "refresh_token": "synthetic", "scopes": ["openid"]},
                AuthorizationState.REQUIRED,
            ),
            (
                {"client_id": "client", "refresh_token": "synthetic"},
                AuthorizationState.REQUIRED,
            ),
            (
                {
                    "client_id": "client",
                    "refresh_token": "synthetic",
                    "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
                    "account": "other@example.org",
                },
                AuthorizationState.REQUIRED,
            ),
            (
                {
                    "client_id": "client",
                    "refresh_token": "synthetic",
                    "token": "expired-access",
                    "expiry": "2000-01-01T00:00:00Z",
                    "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
                },
                AuthorizationState.AUTHORIZED,
            ),
        ):
            with self.subTest(credentials=credentials):
                update_credential_data(self.store, account.id, google_credentials=credentials)
                self.assertEqual(self.oauth.authorization_status(account).state, expected)

    def test_microsoft_inspection_requires_identity_client_and_requested_scopes(self):
        account = oauth_account()
        serialized = microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE])
        for change, expected in (
            (lambda data: None, AuthorizationState.AUTHORIZED),
            (lambda data: data.clear(), AuthorizationState.REQUIRED),
            (
                lambda data: data["Account"]["identity"].update(username="wrong@example.org"),
                AuthorizationState.REQUIRED,
            ),
            (
                lambda data: data["RefreshToken"]["refresh"].update(client_id="wrong"),
                AuthorizationState.REQUIRED,
            ),
            (
                lambda data: data["RefreshToken"]["refresh"].update(target="openid"),
                AuthorizationState.REQUIRED,
            ),
        ):
            with self.subTest(expected=expected):
                data = json.loads(serialized)
                change(data)
                update_credential_data(self.store, account.id, msal_cache=json.dumps(data))
                with patch("msal.PublicClientApplication") as application:
                    self.assertEqual(self.oauth.authorization_status(account).state, expected)
                application.assert_not_called()

    def test_shared_graph_access_and_imap_require_their_own_grants(self):
        for provider, scopes in (
            (
                MailProvider.MICROSOFT_GRAPH,
                [MICROSOFT_MAIL_READ_SCOPE, MICROSOFT_MAIL_READ_SHARED_SCOPE],
            ),
            (MailProvider.GENERIC_IMAP, [MICROSOFT_IMAP_ACCESS_SCOPE]),
        ):
            account = oauth_account(provider)
            account.mailboxes.append(Mailbox("shared@example.org"))
            update_credential_data(
                self.store,
                account.id,
                msal_cache=microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE]),
            )
            self.assertEqual(
                self.oauth.authorization_status(account).state, AuthorizationState.REQUIRED
            )
            update_credential_data(
                self.store, account.id, msal_cache=microsoft_cache(account, scopes)
            )
            self.assertEqual(
                self.oauth.authorization_status(account).state, AuthorizationState.AUTHORIZED
            )
