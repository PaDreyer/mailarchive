"""Public credentials and frozen intake remain bound across normal account changes."""

import imaplib
import re
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from mailarchive.application.account_commands import AccountSubmission
from mailarchive.application.account_credentials import (
    CredentialIdentityError,
    load_account_credential_data,
    store_account_credentials,
)
from mailarchive.application.account_status import (
    AccountAction,
    AccountStatusService,
    AuthorizationState,
    AuthorizationStatus,
)
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
from mailarchive.domain.source_identity import MailTarget
from mailarchive.infrastructure.credentials import KeyringCredentialStore, MemoryCredentialStore
from mailarchive.infrastructure.oauth import (
    GOOGLE_GMAIL_READONLY_SCOPE,
    MICROSOFT_MAIL_READ_SCOPE,
    OAuthManager,
)
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.http import ProviderHttpError
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from mailarchive.presentation.account_form import AccountFormValues, build_account_submission
from tests import test_oauth_account_flow as oauth_fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.oauth_fixture import MicrosoftRequestsTransport, microsoft_cache
from tests.test_imap_client import FakeImapConnection
from tests.test_restart_core import FakeSource, Registry, raw_mail
from tests.test_source_recovery_boundaries import MembershipTransitions, MutableImapConnection


class NativeLoginConnection(FakeImapConnection):
    """Stock imaplib emits LOGIN into a strict local protocol receiver."""

    def __init__(self, expected):
        super().__init__()
        self.expected = expected
        self.wire = []

    def list(self, *args):
        self.calls.append(("list",))
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def login(self, username, password):
        client = imaplib.IMAP4.__new__(imaplib.IMAP4)
        client._mode_ascii()
        client.state, client.literal, client.debug = "NONAUTH", None, 0
        client.untagged_responses, client.tagged_commands = {}, {}
        client.is_readonly = False
        client.tagpre, client.tagnum = b"T", 0
        client._cmd_log, client._cmd_log_len, client._cmd_log_idx = {}, 10, 0
        client.send = self.wire.append
        client._command("LOGIN", username, client._quote(password))
        match = re.fullmatch(
            rb'T\d+ LOGIN ("(?:\\.|[^"\\])*"|[^\s"\\(){}%*]+) ("(?:\\.|[^"\\])*")\r\n',
            self.wire[-1],
        )
        if match is None:
            raise imaplib.IMAP4.error("LOGIN requires two strings")
        value = match[1].decode()
        decoded = re.sub(r"\\(.)", r"\1", value[1:-1]) if value.startswith('"') else value
        if decoded != self.expected:
            raise imaplib.IMAP4.error("LOGIN changed the username")
        return super().login(decoded, password)


class NativeSecretStore(KeyringCredentialStore):
    def __init__(self, owner):
        try:
            from keyring.backends import SecretService
            from secretstorage.exceptions import PromptDismissedException
        except ImportError:
            owner.skipTest("SecretService is not installed on this platform")
        self.values, self.unlocks = {}, 0
        self.locked, self.deny_delete = False, False
        backend = SecretService.Keyring()

        def delete(account_id):
            if self.deny_delete:
                raise PromptDismissedException("Credential deletion prompt dismissed")
            self.values.pop(account_id)

        def items(attributes):
            account_id = attributes["username"]
            if account_id not in self.values:
                return []
            return [
                SimpleNamespace(
                    is_locked=lambda: False,
                    get_secret=lambda: self.values[account_id].encode(),
                    delete=lambda: delete(account_id),
                )
            ]

        def unlock():
            self.unlocks += 1

        collection = SimpleNamespace(
            connection=SimpleNamespace(close=lambda: None),
            is_locked=lambda: self.locked,
            unlock=unlock,
            search_items=items,
        )
        stack = ExitStack()
        owner.addCleanup(stack.close)
        stack.enter_context(
            patch.object(SecretService.secretstorage, "dbus_init", return_value=None)
        )
        stack.enter_context(
            patch.object(
                SecretService.secretstorage, "get_default_collection", return_value=collection
            )
        )
        module = SimpleNamespace(
            get_keyring=lambda: SimpleNamespace(priority=1),
            get_password=backend.get_password,
            set_password=lambda service, account_id, value: self.values.__setitem__(
                account_id, value
            ),
            delete_password=backend.delete_password,
        )
        super().__init__(keyring_module=module)


class AddedMessageHttp(MembershipTransitions):
    def __init__(self, provider):
        super().__init__(provider)
        self.added = False

    def get_json(self, url, *args, **kwargs):
        path = urlsplit(url).path
        if self.added and path.endswith("/history"):
            self.requests.append(url)
            return {
                "historyId": "101",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": item, "labelIds": ["INBOX"]}}
                            for item in ("message", "healthy")
                        ]
                    }
                ],
            }
        result = super().get_json(url, *args, **kwargs)
        if self.added and path.endswith("/messages/delta"):
            result["value"] = [{"id": item} for item in ("message", "healthy")]
            result["@odata.deltaLink"] = f"https://graph.microsoft.com{path}?$deltatoken=added"
        return result


class CredentialAdmissionRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def application(self, credentials, *, profile="profile"):
        store = ConfigStore(self.root / profile)
        if not store.path.exists():
            store.save(Settings(start_at_login=False, automatic_monitoring_paused=True))
        with patch("mailarchive.bootstrap.set_start_at_login"):
            app = create_application(store, credentials)
        self.addCleanup(app.close)
        return app

    def save_password(self, app, username="owner@example.org", folders=None):
        submission = build_account_submission(
            AccountFormValues(
                label="Owner",
                provider=MailProvider.GENERIC_IMAP,
                auth_mode=AuthMode.PASSWORD,
                username=username,
                host="imap.example.org",
                secret="synthetic-password",
                mailboxes=[
                    Mailbox(
                        username,
                        ["INBOX"] if folders is None else folders,
                        archive_existing_messages=True,
                    )
                ],
            )
        )
        app.save_account(submission)
        app.save_rules([Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])])
        return app.settings.accounts[0]

    @staticmethod
    def imap_source(app, credentials, connection):
        adapter = ImapMailbox()
        adapter._connect = lambda *args, **kwargs: connection
        service = app._context.execution.service
        service.source_registry = Registry(ImapMessageSource(credentials, adapter))
        return service

    def test_literal_login_characters_survive_public_save_list_and_range(self):
        for index, username in enumerate(("alice smith", 'alice"smith', "alice\\smith")):
            for folders in (["INBOX"], []):
                with self.subTest(username=username, folders=folders):
                    credentials = MemoryCredentialStore()
                    app = self.application(credentials, profile=f"login-{index}-{bool(folders)}")
                    account = self.save_password(app, username, folders)
                    connection = NativeLoginConnection(username)
                    service = self.imap_source(app, credentials, connection)
                    self.assertEqual(service.run_once(app.settings)[0].failed, 0)
                    self.assertEqual(
                        service.run_range(app.settings, {account.mailboxes[0].id})[0].failed, 0
                    )
                    self.assertEqual(app.settings.accounts[0].username, username)
                    self.assertTrue(connection.wire)

    def test_new_label_unlock_failure_aborts_account_and_reinspection_recovers(self):
        credentials = NativeSecretStore(self)
        app = self.application(credentials)
        submission = build_account_submission(
            AccountFormValues(
                label="Workspace",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_APPLICATION,
                username="",
                mailboxes=[
                    Mailbox(address, ["INBOX"])
                    for address in ("first@example.org", "second@example.org")
                ],
                service_account_file="synthetic.json",
            ),
            service_account_loader=lambda _: {
                "type": "service_account",
                "private_key": "synthetic",
                "client_email": "synthetic@example.iam.gserviceaccount.com",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
        )
        app.save_account(submission)
        app.save_rules([Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])])
        token = SimpleNamespace(token="synthetic", refresh=lambda _: None)
        token.with_subject = lambda _: token
        oauth = OAuthManager(
            credentials,
            google_service_account_factory=lambda *args, **kwargs: token,
            google_request_factory=object,
            on_credentials_unavailable=lambda account, detail: (
                app.account_statuses.credential_record_failed(
                    account.id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, detail)
                )
            ),
        )
        server = MembershipTransitions(MailProvider.GMAIL_API)
        service = app._context.execution.service
        service.source_registry = Registry(GmailMessageSource(oauth, server))
        self.assertEqual(service.run_once(app.settings)[0].failed, 0)

        account = app.settings.accounts[0]
        for mailbox in account.mailboxes:
            mailbox.folders.append("STARRED")
        app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)
        credentials.locked = True
        server.requests.clear()
        result = service.run_once(app.settings)[0]
        self.assertEqual((credentials.unlocks, result.failed), (1, 1))
        self.assertEqual(server.requests, [])
        self.assertFalse(app.account_status(account.id).allows(AccountAction.AUTHORIZE))
        credentials.locked = False
        app.refresh_account_authorization(account.id)
        self.assertTrue(app._background.wait(THREAD_TIMEOUT))
        self.assertEqual(service.run_once(app.settings)[0].failed, 0)

    def test_local_credential_reinspection_requires_complete_noninteractive_data(self):
        cases = (
            (MailProvider.GENERIC_IMAP, AuthMode.PASSWORD, {"password": "synthetic"}),
            (
                MailProvider.MICROSOFT_GRAPH,
                AuthMode.OAUTH_APPLICATION,
                {"client_secret": "synthetic"},
            ),
            (
                MailProvider.GMAIL_API,
                AuthMode.OAUTH_APPLICATION,
                {
                    "google_service_account": {
                        "type": "service_account",
                        "client_email": "synthetic@example.iam.gserviceaccount.com",
                        "private_key": "synthetic",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                },
            ),
        )
        for provider, mode, complete in cases:
            with self.subTest(provider=provider):
                account = Account("Owner", provider=provider, auth_mode=mode)
                credentials = MemoryCredentialStore()
                oauth = OAuthManager(credentials)
                statuses = AccountStatusService(
                    inspect=oauth.authorization_status, accounts=[account]
                )
                self.assertEqual(statuses.refresh(account).state, AuthorizationState.NOT_REQUIRED)
                statuses.credential_record_failed(
                    account.id, AuthorizationStatus(AuthorizationState.UNAVAILABLE, "Store locked")
                )
                self.assertEqual(statuses.refresh(account).state, AuthorizationState.UNAVAILABLE)
                self.assertEqual(
                    statuses.authorization(account).state, AuthorizationState.UNAVAILABLE
                )
                incomplete = deepcopy(complete)
                if provider == MailProvider.GMAIL_API:
                    incomplete["google_service_account"].pop("token_uri")
                else:
                    incomplete[next(iter(incomplete))] = ""
                store_account_credentials(credentials, account, incomplete, replace=True)
                self.assertEqual(statuses.refresh(account).state, AuthorizationState.UNAVAILABLE)
                store_account_credentials(credentials, account, complete, replace=True)
                self.assertEqual(statuses.refresh(account).state, AuthorizationState.NOT_REQUIRED)

    def test_deleted_password_never_reconnects_after_denied_delete_or_restart(self):
        credentials = NativeSecretStore(self)
        app = self.application(credentials)
        account = self.save_password(app)
        connection = MutableImapConnection()
        connection.fail_body = True
        service = self.imap_source(app, credentials, connection)
        self.assertEqual(service.run_once(app.settings)[0].failed, 1)
        intake = dict(service.discovery.pending_automatic_intakes(due_only=False)[0])
        credentials.deny_delete = True
        app.delete_account(account.id)
        connection.calls.clear()
        self.assertEqual(service.run_once(app.settings, force_retry=True), [])
        self.assertEqual(connection.calls, [])
        self.assertEqual(
            dict(service.discovery.pending_automatic_intakes(due_only=False)[0]), intake
        )

        self.assertTrue(app.close())
        reopened = self.application(credentials)
        service = self.imap_source(reopened, credentials, connection)
        self.assertEqual(reopened.settings.accounts, [])
        self.assertEqual(service.run_once(reopened.settings, force_retry=True), [])
        self.assertEqual(connection.calls, [])
        self.assertEqual(
            dict(service.discovery.pending_automatic_intakes(due_only=False)[0]), intake
        )

    def test_deleted_account_keeps_accepted_local_raw_work(self):
        credentials = NativeSecretStore(self)
        app = self.application(credentials)
        account = self.save_password(app)
        service = app._context.execution.service
        source = oauth_fixture.PagedOAuthSource(
            {
                uid: RemoteMessage(
                    uid,
                    raw_mail(),
                    datetime(2026, 1, int(uid), tzinfo=timezone.utc),
                    "imap_internaldate",
                )
                for uid in ("1", "2")
            }
        )
        service.source_registry = Registry(source)
        offline = self.root / "offline"
        offline.write_text("Unavailable destination")
        app.save_rules([Rule("Archive", targets=[RuleTarget(str(offline / "archive"))])])
        operation = service.prepare_range_operation(app.settings, {account.mailboxes[0].id})
        self.assertGreater(service.run_range_operation(operation)[0].failed, 0)
        self.assertEqual(source.enumerated, ["1"])
        credentials.deny_delete = True
        app.delete_account(account.id)
        offline.unlink()
        results = service.run_range_operation(operation)
        self.assertEqual(sum(result.archived for result in results), 1)
        self.assertGreater(sum(result.failed for result in results), 0)
        self.assertEqual(source.enumerated, ["1"])
        self.assertEqual(len(list((offline / "archive").glob("*.eml"))), 1)

    def test_deleted_oauth_account_never_reads_retained_credentials_after_restart(self):
        credentials = NativeSecretStore(self)
        app = self.application(credentials)
        submission = build_account_submission(
            AccountFormValues(
                label="Owner",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_USER,
                username="owner@example.org",
                client_id="client",
                mailboxes=[Mailbox("owner@example.org", ["INBOX"], archive_existing_messages=True)],
            )
        )

        def grant(account, store, *, cancelled):
            store_account_credentials(
                store,
                account,
                {
                    "google_credentials": {
                        "client_id": "client",
                        "client_secret": "synthetic",
                        "token": "synthetic",
                        "expiry": "2030-01-01T00:00:00Z",
                        "refresh_token": "synthetic",
                        "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
                        "account": account.username,
                    }
                },
            )

        app._authorize = grant
        editor = app.account_editor()
        self.assertTrue(editor.authorize(submission))
        self.assertTrue(app._background.wait(THREAD_TIMEOUT))
        editor.save(submission)
        app.save_rules([Rule("Archive", targets=[RuleTarget(str(self.root / "archive"))])])
        server = MembershipTransitions(MailProvider.GMAIL_API)
        server.body_error = ProviderHttpError(503, "Temporary MIME failure")
        service = app._context.execution.service
        service.source_registry = Registry(GmailMessageSource(OAuthManager(credentials), server))
        self.assertEqual(service.run_once(app.settings)[0].failed, 1)
        intake = dict(service.discovery.pending_automatic_intakes(due_only=False)[0])
        credentials.deny_delete = True
        app.delete_account(submission.account.id)
        self.assertTrue(app.close())
        reopened = self.application(credentials)
        service = reopened._context.execution.service
        service.source_registry = Registry(GmailMessageSource(OAuthManager(credentials), server))
        server.requests.clear()
        with patch.object(credentials, "get", side_effect=AssertionError("Removed account read")):
            self.assertEqual(service.run_once(reopened.settings, force_retry=True), [])
        self.assertEqual(server.requests, [])
        self.assertEqual(
            dict(service.discovery.pending_automatic_intakes(due_only=False)[0]), intake
        )

    def test_dynamic_imap_cleanup_preserves_another_frozen_authority(self):
        fixture = oauth_fixture.OAuthAccountFlowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        account = fixture.save_account(MailProvider.GENERIC_IMAP)
        fixture.authorize(account)
        account.mailboxes[0].folders = ["Project  A"]
        account.mailboxes[0].archive_existing_messages = True
        app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)
        app.save_rules([Rule("Archive", targets=[RuleTarget(str(fixture.root / "archive"))])])

        def broken_download():
            raise MailboxError("Temporary body failure")
            yield b""

        source = FakeSource(
            {
                "1": RemoteMessage(
                    "1",
                    received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    received_origin="imap_internaldate",
                    raw_chunks=broken_download,
                )
            }
        )
        service = app._context.execution.service
        service.source_registry = Registry(source)
        self.assertEqual(service.run_once(app.settings)[0].failed, 1)
        pending = dict(service.discovery.pending_automatic_intakes(due_only=False)[0])
        current = fixture.change_tenant(account)
        current.mailboxes[0].folders = []
        app.save_account(AccountSubmission(current, {}, False), replacing_id=current.id)
        source.messages = {}
        target = MailTarget(current, current.mailboxes[0], "Replacement")
        with patch.object(source, "targets", return_value=[target]):
            self.assertEqual(service.run_once(app.settings)[0].failed, 0)
        self.assertEqual(
            dict(service.discovery.pending_automatic_intakes(due_only=False)[0]), pending
        )

    def test_frozen_client_cannot_be_adopted_but_healthy_current_ids_finish(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            with self.subTest(provider=provider):
                fixture = oauth_fixture.OAuthAccountFlowTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                app, credentials = fixture.app, fixture.credentials
                account = fixture.save_account(provider)
                account.mailboxes[0].archive_existing_messages = True
                app.save_account(AccountSubmission(account, {}, False), replacing_id=account.id)

                def grant(candidate, store, *, cancelled, provider=provider):
                    updates = {
                        "msal_cache": microsoft_cache(candidate, [MICROSOFT_MAIL_READ_SCOPE])
                    }
                    if provider == MailProvider.GMAIL_API:
                        updates = {
                            "google_credentials": {
                                "client_id": candidate.client_id,
                                "client_secret": "synthetic-secret",
                                "token": "synthetic-" + candidate.client_id,
                                "expiry": "2030-01-01T00:00:00Z",
                                "refresh_token": "synthetic-refresh",
                                "scopes": [GOOGLE_GMAIL_READONLY_SCOPE],
                                "account": candidate.username,
                            }
                        }
                    store_account_credentials(store, candidate, updates)

                app._authorize = grant

                def authorize(candidate, replace=False, *, app=app, fixture=fixture):
                    editor = app.account_editor(candidate.id)
                    submission = AccountSubmission(candidate, {}, replace)
                    self.assertTrue(editor.authorize(submission))
                    fixture.wait_tasks()
                    editor.save(submission)
                    fixture.wait_tasks()

                authorize(account)
                old = deepcopy(app.settings.accounts[0])
                app.save_rules([Rule("Frozen", targets=[RuleTarget(str(fixture.root / "old"))])])
                server = AddedMessageHttp(provider)
                server.body_error = ProviderHttpError(503, "Temporary MIME failure")
                manager = OAuthManager(
                    credentials,
                    live_account=lambda aid, app=app: next(
                        (a for a in app.settings.accounts if a.id == aid), None
                    ),
                )
                source_type = (
                    GmailMessageSource
                    if provider == MailProvider.GMAIL_API
                    else MicrosoftGraphMessageSource
                )
                service = app._context.execution.service
                service.source_registry = Registry(source_type(manager, server))
                transport = MicrosoftRequestsTransport(
                    {
                        "access_token": "synthetic",
                        "expires_in": 3600,
                        "scope": MICROSOFT_MAIL_READ_SCOPE,
                    }
                )
                with patch(
                    "requests.sessions.Session.request",
                    autospec=True,
                    side_effect=transport.request,
                ):
                    self.assertEqual(service.run_once(app.settings)[0].failed, 1)
                    pending = dict(service.discovery.pending_automatic_intakes(due_only=False)[0])
                    frozen = service.operations.run_settings_snapshot(pending["run_id"])
                    current = deepcopy(old)
                    current.client_id = "new-client"
                    authorize(current, True)
                    app.save_rules(
                        [Rule("Current", targets=[RuleTarget(str(fixture.root / "current"))])]
                    )
                    server.added = True
                    with self.assertRaises(CredentialIdentityError):
                        load_account_credential_data(
                            credentials, old, lambda _, current=current: current
                        )
                    result = service.run_once(app.settings, force_retry=True)[0]
                    self.assertEqual((result.archived, result.failed), (1, 0))
                    self.assertEqual(
                        dict(service.discovery.pending_automatic_intakes(due_only=False)[0]),
                        pending,
                    )
                    self.assertEqual(list((fixture.root / "old").glob("*.eml")), [])
                    self.assertEqual(len(list((fixture.root / "current").glob("*.eml"))), 1)
                    self.assertEqual(
                        service.run_once(app.settings, force_retry=True)[0].archived, 0
                    )
                    authorize(old, True)
                    self.assertEqual(
                        service.run_once(app.settings, force_retry=True)[0].archived, 1
                    )
                    self.assertEqual(
                        service.discovery.pending_automatic_intakes(due_only=False), []
                    )
                    self.assertEqual(
                        service.operations.run_settings_snapshot(pending["run_id"]), frozen
                    )
                    self.assertEqual(len(list((fixture.root / "old").glob("*.eml"))), 1)
                    self.assertEqual(len(list((fixture.root / "current").glob("*.eml"))), 1)
