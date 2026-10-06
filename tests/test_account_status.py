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
    AuthorizationCapability,
    AuthorizationState,
    AuthorizationStatus,
    account_status,
)
from mailarchive.application.credential_port import CredentialError
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

    def test_cached_shared_capability_survives_disable_and_is_cleared_on_revocation(self):
        inspect = Mock(
            return_value=AuthorizationStatus(
                AuthorizationState.AUTHORIZED,
                capabilities=frozenset({AuthorizationCapability.SHARED_MAIL}),
            )
        )
        account = oauth_account()
        statuses = AccountStatusService(inspect)
        statuses.refresh(account)
        shared = deepcopy(account)
        shared.mailboxes.append(Mailbox("shared@example.org"))
        self.assertEqual(statuses.authorization(shared).state, AuthorizationState.AUTHORIZED)
        inspect.assert_called_once()
        statuses.require_authorization(account)
        self.assertNotEqual(statuses.authorization(shared).state, AuthorizationState.AUTHORIZED)

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

    def test_retry_credential_failures_preserve_live_binding_and_allow_reinspection(self):
        for current_shared in (False, True):
            for state in (AuthorizationState.REQUIRED, AuthorizationState.UNAVAILABLE):
                with self.subTest(state=state, current_shared=current_shared):
                    self._assert_retry_failure_recovery(current_shared, state)

    def _assert_retry_failure_recovery(self, current_shared, state):
        current = oauth_account()
        frozen = deepcopy(current)
        shared = current if current_shared else frozen
        shared.mailboxes.append(Mailbox("shared@example.org"))
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect)
        statuses.refresh(current)
        report = (
            statuses.require_authorization
            if state == AuthorizationState.REQUIRED
            else statuses.credentials_unavailable
        )
        report(frozen, "Credential failure")
        self.assertEqual(statuses.authorization(current).state, state)
        self.assertEqual(statuses.authorization(current).detail, "Credential failure")
        inspect.return_value = AuthorizationStatus(AuthorizationState.AUTHORIZED)
        statuses.refresh(current)
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.AUTHORIZED)

    def test_failure_for_previous_identity_does_not_change_live_status(self):
        current = oauth_account()
        statuses = AccountStatusService(
            Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        )
        statuses.set_authorization(current, AuthorizationStatus(AuthorizationState.AUTHORIZED))
        revision = statuses.revision
        for field, value in (
            ("username", "previous@example.org"),
            ("client_id", "previous-client"),
            ("tenant_id", "previous-tenant"),
        ):
            for report in (statuses.require_authorization, statuses.credentials_unavailable):
                with self.subTest(field=field, report=report.__name__):
                    previous = deepcopy(current)
                    setattr(previous, field, value)
                    report(previous, "Outdated failure")
                    self.assertEqual(
                        statuses.authorization(current).state, AuthorizationState.AUTHORIZED
                    )
                    self.assertEqual(statuses.revision, revision)

    def test_record_failure_uses_live_binding_and_supersedes_pending_inspection(self):
        for state in (AuthorizationState.REQUIRED, AuthorizationState.UNAVAILABLE):
            with self.subTest(state=state):
                entered, release = threading.Event(), threading.Event()

                def inspect(account, entered=entered, release=release):
                    entered.set()
                    self.assertTrue(release.wait(THREAD_TIMEOUT))
                    return AuthorizationStatus(AuthorizationState.AUTHORIZED)

                current = oauth_account()
                current.tenant_id = "12345678-1234-1234-1234-123456789abc"
                statuses = AccountStatusService(inspect, accounts=[current])
                worker = threading.Thread(target=statuses.refresh, args=(current,))
                worker.start()
                try:
                    self.assertTrue(entered.wait(THREAD_TIMEOUT))
                    statuses.credential_record_failed(
                        current.id, AuthorizationStatus(state, "Credential record failed")
                    )
                finally:
                    release.set()
                    worker.join(THREAD_TIMEOUT)
                self.assertFalse(worker.is_alive())
                self.assertEqual(statuses.authorization(current).state, state)
                self.assertEqual(statuses.authorization(current).detail, "Credential record failed")
                statuses.refresh(current)
                self.assertEqual(
                    statuses.authorization(current).state, AuthorizationState.AUTHORIZED
                )

    def test_record_failure_blocks_every_frozen_binding_until_live_reinspection(self):
        for state in (AuthorizationState.REQUIRED, AuthorizationState.UNAVAILABLE):
            for field, value in (
                ("tenant_id", "previous-tenant"),
                ("username", "previous@example.org"),
                ("client_id", "previous-client"),
                ("provider", MailProvider.GMAIL_API),
                ("shared_mailbox", "shared@example.org"),
            ):
                with self.subTest(state=state, field=field):
                    current = oauth_account()
                    frozen = deepcopy(current)
                    if field == "shared_mailbox":
                        frozen.mailboxes.append(Mailbox(value))
                    else:
                        setattr(frozen, field, value)
                    inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
                    statuses = AccountStatusService(inspect, accounts=[current])
                    statuses.refresh(current)
                    inspect.reset_mock()
                    failure = AuthorizationStatus(state, "Credential record failed")
                    statuses.credential_record_failed(current.id, failure)
                    revision = statuses.revision
                    for account in (current, frozen):
                        status = statuses.resolve(account, [Rule("Archive")], inspect=True)
                        self.assertEqual(status.authorization, failure)
                        self.assertFalse(status.allows(AccountAction.RETRY_REMOTE))
                    self.assertEqual(statuses.refresh(frozen), failure)
                    inspect.assert_not_called()
                    self.assertEqual(statuses.revision, revision)
                    statuses.refresh(current)
                    inspect.assert_called_once_with(current)
                    self.assertEqual(
                        statuses.authorization(current).state, AuthorizationState.AUTHORIZED
                    )
                    self.assertTrue(
                        statuses.resolve(frozen, [Rule("Archive")], inspect=True).allows(
                            AccountAction.RETRY_REMOTE
                        )
                    )

    def test_record_gate_survives_live_configuration_save_and_recheck_start(self):
        current = oauth_account()
        previous = deepcopy(current)
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect, accounts=[current])
        statuses.refresh(current)
        statuses.credential_record_failed(
            current.id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, "Store locked")
        )
        current.tenant_id = "new-tenant"
        statuses.set_authorization(current, AuthorizationStatus(AuthorizationState.REQUIRED))
        self.assertEqual(statuses.refresh(previous).state, AuthorizationState.REQUIRED)
        inspect.reset_mock()
        statuses.set_authorization(current, AuthorizationStatus(AuthorizationState.CHECKING))
        self.assertEqual(statuses.refresh(previous).state, AuthorizationState.CHECKING)
        inspect.assert_not_called()
        statuses.refresh(current)
        inspect.assert_called_once_with(current)
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.AUTHORIZED)

    def test_frozen_inspection_store_error_gates_live_and_other_frozen_bindings(self):
        current = oauth_account()
        frozen = deepcopy(current)
        frozen.tenant_id = "previous-tenant"
        inspect = Mock(side_effect=CredentialError("Keyring locked"))
        statuses = AccountStatusService(inspect, accounts=[current])
        status = statuses.resolve(frozen, [Rule("Archive")], inspect=True)
        self.assertEqual(status.authorization.state, AuthorizationState.UNAVAILABLE)
        self.assertEqual(statuses.authorization(current), status.authorization)
        inspect.side_effect = None
        inspect.return_value = AuthorizationStatus(AuthorizationState.AUTHORIZED)
        inspect.reset_mock()
        self.assertEqual(statuses.refresh(frozen).state, AuthorizationState.UNAVAILABLE)
        inspect.assert_not_called()
        statuses.refresh(current)
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.AUTHORIZED)

    def test_configuration_specific_requirement_does_not_gate_older_usable_grants(self):
        current = oauth_account()
        current.mailboxes.append(Mailbox("shared@example.org"))
        frozen = deepcopy(current)
        frozen.mailboxes.pop()
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect, accounts=[current])
        statuses.require_authorization(current, "Grant shared mailbox access")
        self.assertTrue(
            statuses.resolve(frozen, [Rule("Archive")], inspect=True).allows(
                AccountAction.RETRY_REMOTE
            )
        )
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.REQUIRED)

    def test_record_failure_cannot_publish_a_grant_for_every_configuration(self):
        current = oauth_account()
        statuses = AccountStatusService(accounts=[current])
        revision = statuses.revision
        with self.assertRaises(ValueError):
            statuses.credential_record_failed(
                current.id, AuthorizationStatus(AuthorizationState.AUTHORIZED)
            )
        self.assertEqual(statuses.revision, revision)

    def test_record_failure_before_registration_remains_blocked_until_live_check(self):
        current = oauth_account()
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect)
        statuses.credential_record_failed(
            current.id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, "Store locked")
        )
        self.assertFalse(
            statuses.resolve(current, [Rule("Archive")], inspect=True).allows(
                AccountAction.RETRY_REMOTE
            )
        )
        inspect.assert_not_called()
        statuses.set_authorization(current, AuthorizationStatus(AuthorizationState.CHECKING))
        statuses.refresh(current)
        inspect.assert_called_once_with(current)
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.AUTHORIZED)

    def test_late_frozen_inspection_cannot_clear_a_new_record_failure(self):
        current = oauth_account()
        frozen = deepcopy(current)
        frozen.tenant_id = "previous-tenant"
        entered, release = threading.Event(), threading.Event()
        results = []

        def inspect(account):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return AuthorizationStatus(AuthorizationState.AUTHORIZED)

        statuses = AccountStatusService(inspect, accounts=[current])
        worker = threading.Thread(target=lambda: results.append(statuses.refresh(frozen)))
        worker.start()
        try:
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            failure = AuthorizationStatus(AuthorizationState.UNAVAILABLE, "Record unavailable")
            statuses.credential_record_failed(current.id, failure)
        finally:
            release.set()
            worker.join(THREAD_TIMEOUT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [failure])
        self.assertEqual(statuses.authorization(current), failure)
        self.assertEqual(statuses.authorization(frozen), failure)

    def test_frozen_retry_failure_supersedes_pending_live_inspection(self):
        entered, release = threading.Event(), threading.Event()

        def inspect(account):
            entered.set()
            self.assertTrue(release.wait(THREAD_TIMEOUT))
            return AuthorizationStatus(AuthorizationState.AUTHORIZED)

        current = oauth_account()
        frozen = deepcopy(current)
        frozen.mailboxes.append(Mailbox("shared@example.org"))
        statuses = AccountStatusService(inspect)
        statuses.set_authorization(current, AuthorizationStatus(AuthorizationState.CHECKING))
        worker = threading.Thread(target=lambda: statuses.refresh(current))
        worker.start()
        try:
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            statuses.require_authorization(frozen, "The grant was revoked")
        finally:
            release.set()
            worker.join(THREAD_TIMEOUT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.REQUIRED)

    def test_live_binding_is_registered_before_frozen_retry_inspection(self):
        current = oauth_account()
        frozen = deepcopy(current)
        frozen.mailboxes.append(Mailbox("shared@example.org"))
        inspect = Mock(return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED))
        statuses = AccountStatusService(inspect, accounts=[current])
        inspect.assert_not_called()
        statuses.refresh(frozen)
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.CHECKING)
        statuses.credentials_unavailable(frozen, "Keyring locked")
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.UNAVAILABLE)
        statuses.refresh(current)
        self.assertEqual(statuses.authorization(current).state, AuthorizationState.AUTHORIZED)


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

    def test_shared_capability_is_inspected_even_when_only_own_mail_is_enabled(self):
        account = oauth_account()
        scopes = [MICROSOFT_MAIL_READ_SCOPE, MICROSOFT_MAIL_READ_SHARED_SCOPE]
        update_credential_data(self.store, account.id, msal_cache=microsoft_cache(account, scopes))
        status = self.oauth.authorization_status(account)
        self.assertEqual(status.capabilities, {AuthorizationCapability.SHARED_MAIL})
        data = json.loads(microsoft_cache(account, [MICROSOFT_MAIL_READ_SCOPE]))
        unrelated = json.loads(microsoft_cache(account, scopes))["RefreshToken"]["refresh"]
        unrelated["client_id"] = "another-client"
        data["RefreshToken"]["other"] = unrelated
        update_credential_data(self.store, account.id, msal_cache=json.dumps(data))
        status = self.oauth.authorization_status(account)
        self.assertEqual(status.state, AuthorizationState.AUTHORIZED)
        self.assertEqual(status.capabilities, frozenset())

    def test_tenant_alias_binding_must_match_authority_and_canonical_realm(self):
        account = oauth_account()
        account.tenant_id = "contoso.onmicrosoft.com"
        canonical = deepcopy(account)
        canonical.tenant_id = "12345678-1234-1234-1234-123456789abc"
        for binding, expected in (
            (
                {
                    "authority": "https://login.microsoftonline.com/contoso.onmicrosoft.com",
                    "realm": canonical.tenant_id,
                },
                AuthorizationState.AUTHORIZED,
            ),
            (
                {
                    "authority": "https://login.microsoftonline.com/other.onmicrosoft.com",
                    "realm": canonical.tenant_id,
                },
                AuthorizationState.REQUIRED,
            ),
            (
                {
                    "authority": "https://login.microsoftonline.com/contoso.onmicrosoft.com",
                    "realm": "another-realm",
                },
                AuthorizationState.REQUIRED,
            ),
            (None, AuthorizationState.REQUIRED),
        ):
            with self.subTest(binding=binding):
                update_credential_data(
                    self.store,
                    account.id,
                    msal_cache=microsoft_cache(canonical, [MICROSOFT_MAIL_READ_SCOPE]),
                    microsoft_tenant=binding,
                )
                with patch("msal.PublicClientApplication") as application:
                    self.assertEqual(self.oauth.authorization_status(account).state, expected)
                application.assert_not_called()
        account.tenant_id = canonical.tenant_id
        self.assertEqual(
            self.oauth.authorization_status(account).state, AuthorizationState.AUTHORIZED
        )
        account.tenant_id = "87654321-1234-1234-1234-123456789abc"
        update_credential_data(
            self.store,
            account.id,
            microsoft_tenant={
                "authority": f"https://login.microsoftonline.com/{account.tenant_id}",
                "realm": canonical.tenant_id,
            },
        )
        self.assertEqual(
            self.oauth.authorization_status(account).state, AuthorizationState.REQUIRED
        )
