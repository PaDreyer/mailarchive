"""Graph scope aliases through real MSAL, draft saving, and profile restart."""

import json
import time
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import msal
import requests

from mailarchive.application.account_credentials import load_credential_data, update_credential_data
from mailarchive.application.account_status import (
    AccountAction,
    AccountState,
    AuthorizationCapability,
    AuthorizationOutcome,
    AuthorizationState,
)
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Mailbox, MailProvider, Rule, RuleTarget
from mailarchive.infrastructure.oauth import (
    MICROSOFT_IMAP_ACCESS_SCOPE,
    MICROSOFT_MAIL_READ_SCOPE,
    MICROSOFT_MAIL_READ_SHARED_SCOPE,
    OAuthManager,
)
from mailarchive.infrastructure.providers.http import ProviderHttpError
from mailarchive.presentation.account_form import AccountFormValues, build_account_submission
from tests import test_oauth_account_flow as flow
from tests.oauth_fixture import (
    MicrosoftInteractiveTransport,
    MicrosoftRequestsTransport,
    microsoft_cache,
)


class MicrosoftScopeAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.profile = flow.OAuthAccountFlowTests()
        self.profile.setUp()
        self.addCleanup(self.profile.doCleanups)
        self.app = self.profile.app

    def submission(self, *, shared=False, enabled=True):
        submission = self.profile.new_submission(enabled=enabled)
        submission.account.username = f"owner-{submission.account.id}@example.org"
        submission.account.mailboxes[0].address = submission.account.username
        submission.account.client_id = "00000000-0000-0000-0000-000000000001"
        submission.account.tenant_id = "12345678-1234-1234-1234-123456789abc"
        if shared:
            submission.account.mailboxes.append(
                Mailbox(f"shared-{submission.account.id}@example.org", ["INBOX"])
            )
        return submission

    def authorize_draft(self, submission, scopes):
        transport = MicrosoftInteractiveTransport(submission.account, scopes)
        receiver = MagicMock()
        receiver.__enter__.return_value = receiver
        receiver.get_port.return_value = 49321
        receiver.get_auth_response.side_effect = transport.receive
        self.app._authorize = lambda account, credentials, *, cancelled: OAuthManager(
            credentials, cancelled=cancelled
        ).authorize_microsoft(account)
        editor = self.app.account_editor()
        before = self.app.settings
        with (
            patch.object(requests.Session, "request", autospec=True, side_effect=transport.request),
            patch("mailarchive.infrastructure.oauth.BrowserAuthorization", return_value=receiver),
        ):
            self.assertTrue(editor.authorize(submission))
            self.profile.wait_tasks()
        self.assertEqual(self.app.settings, before)
        self.assertIsNone(self.profile.credentials.get(submission.account.id))
        self.assertEqual(len(transport.requested_scopes), 1, editor.result_for(submission).detail)
        self.assertIn(MICROSOFT_MAIL_READ_SCOPE, transport.requested_scopes[0].split())
        self.assertEqual(sum(method == "POST" for method, _, _ in transport.requests), 1)
        return editor

    def saved_graph_account(self, scopes="Mail.Read", *, shared=False):
        submission = self.submission(shared=shared)
        editor = self.authorize_draft(submission, scopes)
        editor.save(submission)
        self.profile.wait_tasks()
        return submission.account

    def refresh_response(self, account, scopes):
        return {
            "access_token": "renewed-access",
            "refresh_token": "rotated-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scopes,
            "client_info": MicrosoftInteractiveTransport._encode(
                {"uid": "synthetic-user", "utid": account.tenant_id}
            ),
        }

    @contextmanager
    def graph_requests(self, account, *, token_response=None, reject_original=False):
        service = self.app._context.execution.service
        source = service.source_registry.get(account)
        transport = MicrosoftRequestsTransport(token_response or {})
        exchanges, graph_tokens = [], []

        def request(session, method, url, **kwargs):
            if method == "POST":
                self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")
                exchanges.append(dict(kwargs["data"]))
                if token_response is None:
                    raise requests.Timeout("Synthetic token endpoint outage")
            return transport.request(session, method, url, **kwargs)

        def graph_get(url, access_token, *args, **kwargs):
            graph_tokens.append(access_token)
            if reject_original and access_token == "synthetic-access":
                raise ProviderHttpError(401, "Original token rejected")
            if "/mailFolders/INBOX?" in url:
                return {"id": "folder"}
            return {"value": [], "@odata.deltaLink": url.split("?", 1)[0] + "?cursor=synthetic"}

        with (
            patch.object(requests.Session, "request", request),
            patch.object(source.http, "get_json", side_effect=graph_get),
        ):
            yield exchanges, graph_tokens

    def run_graph_range(self, account):
        rule = Rule("Archive", targets=[RuleTarget(str(self.profile.root / "archive"))])
        self.app.save_rules([rule])
        service = self.app._context.execution.service
        operation = service.prepare_range_operation(
            self.app.settings, {account.mailboxes[0].id}, rule_id=rule.id
        )
        result = service.run_range_operation(operation)
        self.assertEqual(result[0].failed, 0, result[0].errors)
        self.assertEqual(service.operations.manual_operation(operation)["status"], "completed")

    def edit_access_tokens(self, account, edit):
        data = load_credential_data(self.profile.credentials, account.id)
        cache = msal.SerializableTokenCache()
        cache.deserialize(data["msal_cache"])
        original = next(cache.search(cache.CredentialType.ACCESS_TOKEN))
        edit(cache, original)
        update_credential_data(self.profile.credentials, account.id, msal_cache=cache.serialize())

    def test_native_msal_accepts_short_full_and_mixed_graph_scopes_until_mail_processing(self):
        for scopes, shared in (
            (MICROSOFT_MAIL_READ_SCOPE, False),
            ("Mail.Read", False),
            ("mail.read", False),
            (f"{MICROSOFT_MAIL_READ_SCOPE} {MICROSOFT_MAIL_READ_SHARED_SCOPE}", True),
            ("Mail.Read Mail.Read.Shared", True),
            (f"Mail.Read {MICROSOFT_MAIL_READ_SHARED_SCOPE}", True),
            (f"{MICROSOFT_MAIL_READ_SCOPE} Mail.Read.Shared", True),
        ):
            with self.subTest(scopes=scopes):
                submission = self.submission(shared=shared)
                editor = self.authorize_draft(submission, scopes)
                status, result = editor.authorization_snapshot(submission)
                self.assertEqual(status.authorization.state, AuthorizationState.AUTHORIZED)
                self.assertEqual(result.outcome, AuthorizationOutcome.COMPLETED)
                editor.save(submission)
                self.profile.wait_tasks()
                authorization = self.app.account_status(submission.account.id).authorization
                self.assertEqual(authorization.state, AuthorizationState.AUTHORIZED)
                self.assertEqual(
                    AuthorizationCapability.SHARED_MAIL in authorization.capabilities, shared
                )
                native_cache = json.loads(
                    load_credential_data(self.profile.credentials, submission.account.id)[
                        "msal_cache"
                    ]
                )
                self.assertEqual(
                    {entry["target"] for entry in native_cache["RefreshToken"].values()},
                    {" ".join(sorted(scopes.split()))},
                )
                with self.graph_requests(submission.account) as (exchanges, graph_tokens):
                    self.run_graph_range(submission.account)
                self.assertEqual(exchanges, [])
                self.assertEqual(graph_tokens, ["synthetic-access"])
                self.assertTrue(
                    self.app.account_status(submission.account.id).allows(AccountAction.CHECK_MAIL)
                )

    def test_graph_401_refreshes_once_across_scope_styles_and_reuses_replacement(self):
        for initial, replacement, shared in (
            ("Mail.Read", MICROSOFT_MAIL_READ_SCOPE, False),
            (MICROSOFT_MAIL_READ_SCOPE, "Mail.Read", False),
            (
                f"Mail.Read {MICROSOFT_MAIL_READ_SHARED_SCOPE}",
                f"{MICROSOFT_MAIL_READ_SCOPE} Mail.Read.Shared",
                True,
            ),
        ):
            with self.subTest(initial=initial, replacement=replacement):
                account = self.saved_graph_account(initial, shared=shared)
                response = self.refresh_response(account, replacement)
                with self.graph_requests(
                    account, token_response=response, reject_original=True
                ) as (exchanges, graph_tokens):
                    self.run_graph_range(account)
                    self.run_graph_range(account)
                self.assertEqual(len(exchanges), 1)
                self.assertEqual(
                    graph_tokens, ["synthetic-access", "renewed-access", "renewed-access"]
                )
                cache = json.loads(
                    load_credential_data(self.profile.credentials, account.id)["msal_cache"]
                )
                self.assertEqual(
                    {entry["secret"] for entry in cache["AccessToken"].values()}, {"renewed-access"}
                )
                self.assertEqual(
                    {entry["secret"] for entry in cache["RefreshToken"].values()},
                    {"rotated-refresh"},
                )

    def test_expired_and_near_expiry_graph_tokens_refresh_and_reuse_the_new_token(self):
        for remaining in (-5, 120):
            with self.subTest(remaining=remaining):
                account = self.saved_graph_account()

                def expire(cache, original, remaining=remaining):
                    cache.modify(
                        cache.CredentialType.ACCESS_TOKEN,
                        original,
                        {"expires_on": str(int(time.time()) + remaining)},
                    )

                self.edit_access_tokens(account, expire)
                with self.graph_requests(
                    account, token_response=self.refresh_response(account, "Mail.Read")
                ) as (exchanges, graph_tokens):
                    self.run_graph_range(account)
                    self.run_graph_range(account)
                self.assertEqual(len(exchanges), 1)
                self.assertEqual(graph_tokens, ["renewed-access", "renewed-access"])

    def test_cached_graph_scopes_are_bound_to_client_user_environment_realm_and_resource(self):
        for field, value in (
            ("client_id", "another-client"),
            ("home_account_id", "another-user.tenant"),
            ("environment", "another.example.org"),
            ("realm", "another-tenant"),
            ("target", "https://outlook.office.com/Mail.Read"),
        ):
            with self.subTest(field=field):
                account = self.saved_graph_account()

                def add_unrelated(cache, original, field=field, value=value):
                    unrelated = {
                        **original,
                        "target": MICROSOFT_MAIL_READ_SCOPE,
                        "expires_on": str(int(time.time()) + 7200),
                        "secret": "unrelated-access",
                        field: value,
                    }
                    cache.modify(cache.CredentialType.ACCESS_TOKEN, unrelated, unrelated)

                self.edit_access_tokens(account, add_unrelated)
                with self.graph_requests(account) as (exchanges, graph_tokens):
                    self.run_graph_range(account)
                self.assertEqual(exchanges, [])
                self.assertEqual(graph_tokens, ["synthetic-access"])

    def test_cached_graph_scopes_prefer_a_fresh_token_over_a_near_expiry_alias(self):
        account = self.saved_graph_account()

        def add_near_expiry_first(cache, original):
            cache.remove_at(original)
            near_expiry = {
                **original,
                "target": MICROSOFT_MAIL_READ_SCOPE,
                "expires_on": str(int(time.time()) + 120),
                "secret": "near-expiry-access",
            }
            cache.modify(cache.CredentialType.ACCESS_TOKEN, near_expiry, near_expiry)
            cache.modify(cache.CredentialType.ACCESS_TOKEN, original, original)

        self.edit_access_tokens(account, add_near_expiry_first)
        with self.graph_requests(account) as (exchanges, graph_tokens):
            self.run_graph_range(account)
        self.assertEqual(exchanges, [])
        self.assertEqual(graph_tokens, ["synthetic-access"])

    def test_forced_graph_refresh_preserves_unrelated_access_tokens(self):
        account = self.saved_graph_account()
        unrelated = []

        def add_unrelated(cache, original):
            for field, value in (
                ("client_id", "another-client"),
                ("home_account_id", "another-user.tenant"),
                ("environment", "another.example.org"),
                ("realm", "another-tenant"),
                ("target", "https://outlook.office.com/Mail.Read"),
            ):
                entry = {**original, "secret": "unrelated-" + field, field: value}
                unrelated.append(entry)
                cache.modify(cache.CredentialType.ACCESS_TOKEN, entry, entry)

        self.edit_access_tokens(account, add_unrelated)
        with self.graph_requests(
            account,
            token_response=self.refresh_response(account, MICROSOFT_MAIL_READ_SCOPE),
            reject_original=True,
        ) as (exchanges, _graph_tokens):
            self.run_graph_range(account)
        self.assertEqual(len(exchanges), 1)
        cache = json.loads(load_credential_data(self.profile.credentials, account.id)["msal_cache"])
        entries = list(cache["AccessToken"].values())
        for entry in unrelated:
            self.assertIn(entry, entries)
        self.assertNotIn("synthetic-access", {entry["secret"] for entry in entries})

    def test_scope_selection_requests_only_required_permissions_when_refresh_is_forced(self):
        account = self.saved_graph_account("Mail.Read User.Read")
        with self.graph_requests(
            account,
            token_response=self.refresh_response(account, "Mail.Read"),
            reject_original=True,
        ) as (exchanges, graph_tokens):
            self.run_graph_range(account)
        self.assertEqual(len(exchanges), 1)
        self.assertNotIn("User.Read", exchanges[0]["scope"].split())
        self.assertEqual(graph_tokens, ["synthetic-access", "renewed-access"])

    def test_native_msal_rejects_other_resources_and_missing_graph_consent(self):
        for scopes, shared in (
            ("Mail.ReadBasic", False),
            ("https://outlook.office.com/Mail.Read", False),
            ("https://example.org/Mail.Read", False),
            ("https://graph.microsoft.com.example.org/Mail.Read", False),
            ("https://graph.microsoft.com/other/Mail.Read", False),
            ("openid profile", False),
            ("Mail.Read", True),
            ("Mail.Read.Shared", True),
            ("Mail.Read https://outlook.office.com/Mail.Read.Shared", True),
        ):
            with self.subTest(scopes=scopes, shared=shared):
                submission = self.submission(shared=shared)
                editor = self.authorize_draft(submission, scopes)
                status, result = editor.authorization_snapshot(submission)
                self.assertEqual(status.authorization.state, AuthorizationState.REQUIRED)
                self.assertEqual(result.outcome, AuthorizationOutcome.FAILED)
                self.assertFalse(status.allows(AccountAction.RETRY_REMOTE))
                editor.save(submission)
                self.profile.wait_tasks()
                self.assertIsNone(self.profile.credentials.get(submission.account.id))
                self.assertEqual(
                    self.app.account_status(submission.account.id).state,
                    AccountState.AUTHORIZATION_REQUIRED,
                )

    def test_graph_aliases_do_not_grant_imap_access(self):
        account = self.profile.new_submission(MailProvider.GENERIC_IMAP).account
        for scope, expected in (
            ("Mail.Read", AuthorizationState.REQUIRED),
            (MICROSOFT_MAIL_READ_SCOPE, AuthorizationState.REQUIRED),
            ("IMAP.AccessAsUser.All", AuthorizationState.REQUIRED),
            ("https://graph.microsoft.com/IMAP.AccessAsUser.All", AuthorizationState.REQUIRED),
            (MICROSOFT_IMAP_ACCESS_SCOPE, AuthorizationState.AUTHORIZED),
        ):
            with self.subTest(scope=scope):
                update_credential_data(
                    self.profile.credentials,
                    account.id,
                    msal_cache=microsoft_cache(account, [scope]),
                )
                with patch.object(requests.Session, "request") as network:
                    status = OAuthManager(self.profile.credentials).authorization_status(account)
                network.assert_not_called()
                self.assertEqual(status.state, expected)

    def test_native_short_scope_grant_survives_restart_and_shared_mailbox_changes(self):
        submission = self.submission(shared=True)
        editor = self.authorize_draft(submission, "Mail.Read Mail.Read.Shared")
        self.assertEqual(editor.result_for(submission).outcome, AuthorizationOutcome.COMPLETED)
        submission.account.mailboxes[1].enabled = False
        editor.save(submission)
        self.profile.wait_tasks()
        self.app.set_automatic_monitoring_paused(True)
        self.assertTrue(self.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            self.app = self.profile.app = create_application(
                self.profile.store, self.profile.credentials
            )
        self.addCleanup(self.app.close)
        with patch.object(requests.Session, "request") as network:
            self.app.start()
            self.profile.wait_tasks()
        network.assert_not_called()
        status = self.app.account_status(submission.account.id)
        self.assertEqual(status.authorization.state, AuthorizationState.AUTHORIZED)
        self.assertIn(AuthorizationCapability.SHARED_MAIL, status.authorization.capabilities)
        account = self.app.settings.accounts[0]
        account.mailboxes[1].enabled = True
        updated = build_account_submission(
            AccountFormValues(
                label=account.label,
                provider=account.provider,
                auth_mode=account.auth_mode,
                username=account.username,
                client_id=account.client_id,
                tenant_id=account.tenant_id,
                mailboxes=account.mailboxes,
            ),
            existing=submission.account,
        )
        self.assertFalse(updated.replace_credentials)
        self.assertEqual(
            self.app.preview_account_status(updated).authorization.state,
            AuthorizationState.AUTHORIZED,
        )
        self.app.save_account(updated, replacing_id=account.id)
        self.profile.wait_tasks()
        self.assertEqual(
            self.app.account_status(account.id).authorization.state, AuthorizationState.AUTHORIZED
        )

    def test_native_short_scope_sign_in_keeps_manual_pause(self):
        submission = self.submission(enabled=False)
        editor = self.authorize_draft(submission, "Mail.Read")
        self.assertEqual(editor.result_for(submission).outcome, AuthorizationOutcome.COMPLETED)
        editor.save(submission)
        self.profile.wait_tasks()
        status = self.app.account_status(submission.account.id)
        self.assertEqual(status.authorization.state, AuthorizationState.AUTHORIZED)
        self.assertEqual(status.state, AccountState.PAUSED)
        self.assertFalse(status.allows(AccountAction.CHECK_MAIL))
