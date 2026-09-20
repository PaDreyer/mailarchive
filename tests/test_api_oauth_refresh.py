import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from mailarchive.mail_identity import mailbox_namespace
from mailarchive.mail_sources import (
    GmailMessageSource,
    MessageSourceRegistry,
    MicrosoftGraphMessageSource,
    ProviderHttpError,
)
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from mailarchive.oauth import AuthorizationError
from mailarchive.service import ArchiveService
from mailarchive.synchronization import SyncSession
from tests import test_synchronization as fixtures
from tests.helpers import mail_target
from tests.test_mail_sources import FakeOAuth

AUTH_MODES = (AuthMode.OAUTH_USER, AuthMode.OAUTH_APPLICATION)
API_PROVIDERS = (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH)


class TokenRecordingHttp(fixtures.ScriptedHttp):
    def __init__(self, steps):
        super().__init__(steps)
        self.authorized_calls = []

    def get_json(self, url, access_token, headers=None):
        self.authorized_calls.append(("json", url, headers, access_token))
        return super().get_json(url, access_token, headers)

    def get_bytes(self, url, access_token, headers=None):
        self.authorized_calls.append(("bytes", url, headers, access_token))
        return super().get_bytes(url, access_token, headers)


def rejected(step, status=401):
    return (*step[:2], ProviderHttpError(status, "Access token rejected"))


class ApiOAuthRefreshTests(unittest.TestCase):
    def setup_source(self, provider, mode, steps, *, incremental=False):
        self.account = Account(
            "API mailbox",
            username="owner@example.org",
            provider=provider,
            auth_mode=mode,
            client_id="client",
            tenant_id="tenant",
            mailboxes=[Mailbox("archive@example.org", folders=["INBOX"])],
        )
        if provider == MailProvider.GMAIL_API and mode == AuthMode.OAUTH_USER:
            self.account.username = self.account.mailboxes[0].address
        self.oauth = FakeOAuth()
        self.http = TokenRecordingHttp(steps)
        source_type = (
            GmailMessageSource
            if provider == MailProvider.GMAIL_API
            else MicrosoftGraphMessageSource
        )
        self.source = source_type(self.oauth, self.http)
        cursor = None
        if incremental:
            cursor = (
                "100"
                if provider == MailProvider.GMAIL_API
                else "https://graph.microsoft.com/v1.0/saved"
            )
        self.sync = SyncSession(lambda _: cursor, lambda _: {"recheck"}, report_reset=Mock())
        self.should_fetch = Mock(return_value=True)
        self.target = mail_target(self.account)

    @staticmethod
    def scan_steps(provider, incremental):
        if provider == MailProvider.GMAIL_API:

            def page(ids, **options):
                if incremental:
                    return fixtures.gmail_history(
                        "100",
                        history=[{"messagesAdded": [{"message": {"id": uid}} for uid in ids]}],
                        **options,
                    )
                response = {"messages": [{"id": uid} for uid in ids]}
                if "next_page" in options:
                    response["nextPageToken"] = options["next_page"]
                return ("json", "/messages?", response)

            steps = (
                [] if incremental else [("json", "/profile?fields=historyId", {"historyId": "100"})]
            )
            steps.append(page(["first"], next_page="page 2"))
            for uid in ("first", "second"):
                if uid == "second":
                    steps.append(page([uid]))
                if incremental:
                    steps.append(fixtures.gmail_metadata(uid))
                steps.append(fixtures.gmail_raw(uid))
            steps += [fixtures.gmail_metadata("recheck"), fixtures.gmail_raw("recheck")]
            return steps, "101" if incremental else "100"
        steps = [
            fixtures.graph_delta(
                "/saved" if incremental else "/messages/delta?",
                ["first"],
                next_page="page2?$skiptoken=opaque%2Bvalue",
            ),
            fixtures.graph_folder(),
            fixtures.graph_message("first"),
            fixtures.graph_raw("first"),
            fixtures.graph_delta("/page2?$skiptoken=opaque%2Bvalue", ["second"]),
            fixtures.graph_message("second"),
            fixtures.graph_raw("second"),
            fixtures.graph_message("recheck"),
            fixtures.graph_raw("recheck"),
        ]
        return steps, "https://graph.microsoft.com/v1.0/next"

    def read_messages(self):
        _, messages = self.source.fetch_messages(self.target, self.should_fetch, sync=self.sync)
        return list(messages)

    def refresh_flags(self, provider):
        return (
            self.oauth.google_force_refresh
            if provider == MailProvider.GMAIL_API
            else self.oauth.microsoft_force_refresh
        )

    def test_expiry_at_every_api_read_resumes_all_supported_authentication_modes(self):
        for provider in API_PROVIDERS:
            for mode in AUTH_MODES:
                for incremental in (False, True):
                    steps, cursor = self.scan_steps(provider, incremental)
                    for index, step in enumerate(steps):
                        with self.subTest(
                            provider=provider, mode=mode, incremental=incremental, request=index
                        ):
                            script = steps[:index] + [rejected(step)] + steps[index:]
                            self.setup_source(provider, mode, script, incremental=incremental)
                            messages = self.read_messages()
                            self.assertEqual(
                                [message.id for message in messages], ["first", "second", "recheck"]
                            )
                            self.assertEqual(self.should_fetch.call_count, 3)
                            self.assertEqual(self.sync.next_cursor, cursor)
                            self.sync.report_reset.assert_not_called()
                            self.assertEqual(self.http.steps, [])
                            self.assertEqual(self.refresh_flags(provider), [False, True])
                            before, after = self.http.authorized_calls[index : index + 2]
                            self.assertEqual(before[:3], after[:3])
                            prefix = "google" if provider == MailProvider.GMAIL_API else "microsoft"
                            self.assertEqual(before[3], f"{prefix}-token")
                            self.assertTrue(
                                all(
                                    call[3] == f"{prefix}-refreshed-token"
                                    for call in self.http.authorized_calls[index + 1 :]
                                )
                            )
                            if provider == MailProvider.GMAIL_API:
                                self.assertEqual(
                                    self.oauth.google_subjects,
                                    [(self.account.id, self.target.mailbox.address)] * 2,
                                )
                            else:
                                self.assertEqual(self.oauth.microsoft_accounts, [self.account] * 2)

    def test_several_expirations_in_one_scan_can_each_renew(self):
        for provider in API_PROVIDERS:
            with self.subTest(provider=provider):
                steps, cursor = self.scan_steps(provider, True)
                script = []
                for index, step in enumerate(steps):
                    if index in (0, 3, len(steps) - 1):
                        script.append(rejected(step))
                    script.append(step)
                self.setup_source(provider, AuthMode.OAUTH_USER, script, incremental=True)
                self.assertEqual(len(self.read_messages()), 3)
                self.assertEqual(self.sync.next_cursor, cursor)
                self.assertEqual(self.refresh_flags(provider), [False, True, True, True])

    def test_real_http_transport_repeats_json_and_mime_requests_with_renewed_bearer(self):
        for provider in API_PROVIDERS:
            with self.subTest(provider=provider):
                self.setup_source(provider, AuthMode.OAUTH_USER, [])
                if provider == MailProvider.GMAIL_API:
                    source = GmailMessageSource(self.oauth)
                    page = {"messages": [{"id": "first"}]}
                    raw_response = json.dumps(fixtures.gmail_raw("first")[2]).encode()
                else:
                    source = MicrosoftGraphMessageSource(self.oauth)
                    page = {"value": [{"id": "first"}]}
                    raw_response = fixtures.sample_mail()
                rejected_response = HTTPError(
                    "https://provider.example/",
                    401,
                    "Unauthorized",
                    {},
                    io.BytesIO(b'{"error":{"code":"InvalidAuthenticationToken"}}'),
                )
                with patch(
                    "mailarchive.mail_sources.urlopen",
                    side_effect=[
                        io.BytesIO(json.dumps(page).encode()),
                        rejected_response,
                        io.BytesIO(raw_response),
                    ],
                ) as urlopen:
                    _, messages = source.fetch_messages(self.target, self.should_fetch)
                    self.assertEqual([message.id for message in messages], ["first"])
                before, after = [call.args[0] for call in urlopen.call_args_list[1:]]
                self.assertEqual(before.full_url, after.full_url)
                prefix = "google" if provider == MailProvider.GMAIL_API else "microsoft"
                self.assertEqual(before.get_header("Authorization"), f"Bearer {prefix}-token")
                self.assertEqual(
                    after.get_header("Authorization"), f"Bearer {prefix}-refreshed-token"
                )
                self.assertEqual(before.get_header("Accept"), after.get_header("Accept"))

    def test_authentication_retry_is_bounded_and_other_http_errors_are_not_refreshed(self):
        for provider in API_PROVIDERS:
            for status in (401, 403, 429, 500):
                with self.subTest(provider=provider, status=status):
                    steps, _ = self.scan_steps(provider, True)
                    script = [rejected(steps[0], status)] * (2 if status == 401 else 1)
                    self.setup_source(
                        provider, AuthMode.OAUTH_APPLICATION, script, incremental=True
                    )
                    with self.assertRaises(ProviderHttpError) as error:
                        self.read_messages()
                    self.assertEqual(error.exception.status, status)
                    self.assertEqual(self.http.steps, [])
                    self.assertEqual(
                        self.refresh_flags(provider), [False, True] if status == 401 else [False]
                    )
                    self.assertIsNone(self.sync.next_cursor)
                    self.sync.report_reset.assert_not_called()

    def test_revoked_refresh_credentials_stop_without_replaying_the_request(self):
        for provider in API_PROVIDERS:
            for mode in AUTH_MODES:
                with self.subTest(provider=provider, mode=mode):
                    steps, _ = self.scan_steps(provider, True)
                    self.setup_source(provider, mode, [rejected(steps[0])], incremental=True)
                    acquire = Mock(
                        side_effect=["old-token", AuthorizationError("Credentials revoked")]
                    )
                    if provider == MailProvider.GMAIL_API:
                        self.oauth.google_access_token = acquire
                    else:
                        self.oauth.microsoft_access_token = acquire
                    with self.assertRaisesRegex(AuthorizationError, "Credentials revoked"):
                        self.read_messages()
                    self.assertEqual(acquire.call_count, 2)
                    self.assertEqual(len(self.http.authorized_calls), 1)
                    self.assertIsNone(self.sync.next_cursor)

    def test_graph_folder_discovery_recovers_on_every_page_including_children(self):
        steps = [
            (
                "json",
                "/mailFolders?",
                {
                    "value": [{"id": "a", "childFolderCount": 1}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/folders2",
                },
            ),
            ("json", "/a/childFolders?", {"value": [{"id": "b", "childFolderCount": 0}]}),
            ("json", "/folders2", {"value": [{"id": "c", "childFolderCount": 0}]}),
        ]
        for mode in AUTH_MODES:
            for index, step in enumerate(steps):
                with self.subTest(mode=mode, page=index):
                    self.setup_source(
                        MailProvider.MICROSOFT_GRAPH,
                        mode,
                        steps[:index] + [rejected(step)] + steps[index:],
                    )
                    self.assertEqual(self.source.list_folders(self.target), ["a", "b", "c"])
                    self.assertEqual(self.oauth.microsoft_force_refresh, [False, True])
                    before, after = self.http.authorized_calls[index : index + 2]
                    self.assertEqual(before[:3], after[:3])
                    self.assertEqual(after[3], "microsoft-refreshed-token")
                    self.assertEqual(after[2], MicrosoftGraphMessageSource.GRAPH_HEADERS)

    def test_refresh_is_local_to_each_scan_when_a_source_is_shared(self):
        for provider in API_PROVIDERS:
            with self.subTest(provider=provider):
                steps, _ = self.scan_steps(provider, False)
                script = [rejected(steps[0])] + steps + steps
                self.setup_source(provider, AuthMode.OAUTH_APPLICATION, script)
                _, first = self.source.fetch_messages(
                    self.target, self.should_fetch, sync=self.sync
                )
                second_target = mail_target(
                    self.account, mailbox=Mailbox("other@example.org", ["INBOX"])
                )
                second_sync = SyncSession(lambda _: None, lambda _: {"recheck"})
                _, second = self.source.fetch_messages(
                    second_target, self.should_fetch, sync=second_sync
                )
                self.assertEqual(len(list(first)), 3)
                self.assertEqual(len(list(second)), 3)
                prefix = "google" if provider == MailProvider.GMAIL_API else "microsoft"
                self.assertEqual(self.http.authorized_calls[len(steps) + 1][3], f"{prefix}-token")
                self.assertEqual(self.refresh_flags(provider), [False, False, True])


class ApiOAuthPersistenceTests(unittest.TestCase):
    setUp = fixtures.SynchronizationTests.setUp
    configure = fixtures.SynchronizationTests.configure
    cursor = fixtures.SynchronizationTests.cursor

    def setup_service(self, provider, mode, steps):
        self.configure(provider, existing=True)
        self.account.auth_mode = mode
        self.account.client_id = "client"
        self.account.tenant_id = "tenant"
        # Each subtest has an independent physical mailbox and processing history.
        self.account.username = f"{self.account.id}@example.org"
        self.account.mailboxes[0].address = self.account.username
        self.http = TokenRecordingHttp(steps)
        registry = MessageSourceRegistry(self.credentials, http=self.http)
        self.oauth = FakeOAuth()
        registry.sources[provider].oauth = self.oauth
        return ArchiveService(self.credentials, self.state, source_registry=registry)

    def check(self):
        return self.state.mailbox_check(
            self.account.id, mailbox_namespace(self.account, self.account.mailboxes[0])
        )

    @staticmethod
    def baseline(provider):
        if provider == MailProvider.GMAIL_API:
            return [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?", {"messages": []}),
            ]
        return [fixtures.graph_delta("/messages/delta?", next_cursor="saved")]

    def test_recovered_api_scan_commits_checkpoint_and_next_scan_is_incremental(self):
        for provider in API_PROVIDERS:
            for mode in AUTH_MODES:
                with self.subTest(provider=provider, mode=mode):
                    steps, cursor = ApiOAuthRefreshTests.scan_steps(provider, True)
                    steps = steps[:-2]  # No unmatched messages to recheck in this mailbox.
                    last = steps[-1]
                    next_scan = (
                        [fixtures.gmail_history("101", next_cursor="102")]
                        if provider == MailProvider.GMAIL_API
                        else [fixtures.graph_delta("/next", next_cursor="after")]
                    )
                    service = self.setup_service(
                        provider,
                        mode,
                        self.baseline(provider) + steps[:-1] + [rejected(last), last] + next_scan,
                    )
                    self.assertEqual(service.run_once(self.settings)[0].failed, 0)
                    result = service.run_once(self.settings)[0]
                    self.assertEqual((result.archived, result.failed), (2, 0))
                    self.assertEqual(self.cursor(), cursor)
                    self.assertEqual(self.check()["status"], "success")
                    result = service.run_once(self.settings)[0]
                    self.assertEqual((result.checked, result.archived, result.failed), (0, 0, 0))
                    self.assertEqual(self.http.steps, [])

    def test_rejected_replacement_preserves_success_and_saved_messages_for_next_scan(self):
        for provider in API_PROVIDERS:
            for mode in AUTH_MODES:
                with self.subTest(provider=provider, mode=mode):
                    steps, cursor = ApiOAuthRefreshTests.scan_steps(provider, True)
                    steps = steps[:-2]
                    if provider == MailProvider.GMAIL_API:
                        retry = [steps[0], steps[1], *steps[3:]]  # Skip the saved MIME body.
                    else:
                        retry = [steps[0], steps[4], steps[1], *steps[5:]]
                    service = self.setup_service(
                        provider,
                        mode,
                        self.baseline(provider) + steps[:-1] + [rejected(steps[-1])] * 2 + retry,
                    )
                    self.assertEqual(service.run_once(self.settings)[0].failed, 0)
                    saved_cursor, before = self.cursor(), self.check()
                    result = service.run_once(self.settings)[0]
                    self.assertEqual((result.archived, result.failed), (1, 1))
                    self.assertEqual(self.cursor(), saved_cursor)
                    self.assertEqual(self.check()["status"], "failed")
                    self.assertEqual(
                        self.check()["last_successful_at"], before["last_successful_at"]
                    )
                    result = service.run_once(self.settings)[0]
                    self.assertEqual((result.archived, result.failed), (1, 0))
                    self.assertEqual(self.cursor(), cursor)
                    self.assertEqual(self.check()["status"], "success")
                    self.assertEqual(self.http.steps, [])
