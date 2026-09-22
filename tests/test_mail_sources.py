import base64
import io
import unittest
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from mailarchive.credential_data import update_credential_data
from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import (
    MailboxError,
    RemoteMessage,
    RemoteMessageError,
    RemoteMessageUnavailable,
)
from mailarchive.intake_limits import IntakeCapacityError
from mailarchive.mail_sources import (
    GmailMessageSource,
    HttpClient,
    ImapMessageSource,
    MicrosoftGraphMessageSource,
    ProviderHttpError,
    ScanWideProviderError,
    _GmailMailboxScan,
    _GraphFolderScan,
    _SameOriginRedirectHandler,
)
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from mailarchive.synchronization import SyncSession
from tests.helpers import imap_namespace, mail_target

RECEIVED = datetime(2026, 9, 21, tzinfo=timezone.utc)


class FakeOAuth:
    def __init__(self):
        self.microsoft_accounts = []
        self.microsoft_force_refresh = []
        self.google_subjects = []
        self.google_force_refresh = []

    def google_access_token(self, account, *, mailbox_address=None, force_refresh=False):
        self.google_subjects.append((account.id, mailbox_address))
        self.google_force_refresh.append(force_refresh)
        return "google-refreshed-token" if force_refresh else "google-token"

    def microsoft_access_token(self, account, *, force_refresh=False):
        self.microsoft_accounts.append(account)
        self.microsoft_force_refresh.append(force_refresh)
        return "microsoft-refreshed-token" if force_refresh else "microsoft-token"


class FakeGmailHttp:
    def __init__(self, raw):
        self.raw = raw
        self.urls = []

    def get_json(self, url, access_token, headers=None):
        self.urls.append(url)
        if "?format=raw" in url:
            return {
                "raw": base64.urlsafe_b64encode(self.raw).decode("ascii").rstrip("="),
                "internalDate": "1789948800000",
            }
        return {"messages": [{"id": "known"}, {"id": "new"}]}


class FakeGraphHttp:
    def __init__(self, raw):
        self.raw = raw
        self.json_urls = []
        self.byte_urls = []

    def get_json(self, url, access_token, headers=None):
        self.json_urls.append(url)
        if "$select=receivedDateTime" in url:
            return {"receivedDateTime": "2026-09-21T00:00:00Z"}
        return {"value": [{"id": "known"}, {"id": "new"}]}

    def get_bytes(self, url, access_token, headers=None):
        self.byte_urls.append(url)
        return self.raw


class SequencedHttp:
    def __init__(self, pages, raw_by_id=None):
        self.pages = iter(pages)
        self.raw_by_id = raw_by_id or {}
        self.json_calls = []
        self.byte_calls = []

    def get_json(self, url, access_token, headers=None):
        self.json_calls.append((url, access_token, headers))
        if "?format=raw" in url:
            message_id = url.split("/messages/", 1)[1].split("?", 1)[0]
            return self.raw_by_id[message_id] | {"internalDate": "1789948800000"}
        if "$select=receivedDateTime" in url:
            return {"receivedDateTime": "2026-09-21T00:00:00Z"}
        return next(self.pages)

    def get_bytes(self, url, access_token, headers=None):
        self.byte_calls.append((url, access_token, headers))
        message_id = url.split("/messages/", 1)[1].split("/", 1)[0]
        return self.raw_by_id[message_id]


class FakeImapMailbox:
    def __init__(self):
        self.arguments = None
        self.direct_arguments = None

    def fetch_messages(
        self,
        account,
        password,
        should_fetch,
        *,
        access_token=None,
        refresh_access_token=None,
        sync=None,
    ):
        self.arguments = (account.account, password, should_fetch, access_token)
        self.refresh_access_token = refresh_access_token
        from mailarchive.mail_identity import imap_scope

        return imap_scope(account, "42"), iter([RemoteMessage(id="7", raw=b"mail")])

    def fetch_message(
        self,
        target,
        remote_id,
        processing_namespace,
        password,
        *,
        access_token=None,
        refresh_access_token=None,
    ):
        self.direct_arguments = (
            target,
            remote_id,
            processing_namespace,
            password,
            access_token,
            refresh_access_token,
        )
        return RemoteMessage(id=remote_id, raw=b"direct mail")


class MailSourceTests(unittest.TestCase):
    def test_provider_redirects_are_limited_to_the_same_https_origin(self) -> None:
        handler = _SameOriginRedirectHandler()
        request = Request(
            "https://graph.microsoft.com/v1.0/me/messages",
            headers={"Authorization": "Bearer secret-token"},
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://graph.microsoft.com/v1.0/me/messages?page=2",
        )
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret-token")

        for target in (
            "https://attacker.example/collect",
            "http://graph.microsoft.com/v1.0/me/messages",
            "https://graph.microsoft.com:444/v1.0/me/messages",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(HTTPError, "untrusted origin"):
                handler.redirect_request(request, None, 302, "Found", {}, target)

        with patch("mailarchive.mail_sources.urlopen") as open_url:
            with self.assertRaisesRegex(MailboxError, "invalid secure URL"):
                list(HttpClient().iter_bytes("http://provider.example/message", "token"))
        open_url.assert_not_called()

    def test_targeted_gmail_fetch_uses_stable_id_without_listing_or_label_filter(self) -> None:
        raw = b"Subject: Direct\r\n\r\nBody"
        http = FakeGmailHttp(raw)
        source = GmailMessageSource(FakeOAuth(), http)
        account = Account(
            label="Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("me@gmail.com", folders=["changed-label"])],
        )
        target = mail_target(account)

        message = source.fetch_message(target, "stable/id", target.mailbox_namespace)

        self.assertEqual(message.raw, raw)
        self.assertEqual(len(http.urls), 1)
        self.assertIn("/messages/stable%2Fid?format=raw", http.urls[0])

    def test_targeted_graph_fetch_uses_mailbox_wide_immutable_id(self) -> None:
        raw = b"Subject: Direct\r\n\r\nBody"
        http = FakeGraphHttp(raw)
        source = MicrosoftGraphMessageSource(FakeOAuth(), http)
        account = Account(
            label="Microsoft 365",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("me@example.com", folders=["changed-folder"])],
        )
        target = mail_target(account)

        message = source.fetch_message(target, "stable/id", target.mailbox_namespace)

        self.assertEqual(message.raw, raw)
        self.assertIn("/me/messages/stable%2Fid?", http.json_urls[0])
        self.assertIn("/me/messages/stable%2Fid/$value", http.byte_urls[0])
        self.assertNotIn("/mailFolders/", http.byte_urls[0])

    def test_lazy_body_404_remains_an_intake_error_until_reconciliation(self) -> None:
        def unavailable():
            raise ProviderHttpError(404, "gone during body download")
            yield b""

        for scan_type in (_GmailMailboxScan, _GraphFolderScan):
            for automatic in (False, True):
                with self.subTest(scan=scan_type.__name__, automatic=automatic):
                    sync = (
                        SyncSession(lambda _namespace: "cursor", lambda _namespace: set())
                        if automatic
                        else None
                    )
                    scan = scan_type.__new__(scan_type)
                    scan.sync = sync
                    chunks = scan._raw_chunks("message-id", unavailable)

                    with self.assertRaises(RemoteMessageUnavailable):
                        list(chunks())

                    if sync is not None:
                        self.assertEqual(sync.discarded_ids, set())

    def test_lazy_body_auth_and_throttle_failures_remain_scan_wide(self) -> None:
        for status in (401, 403, 429):
            for scan_type in (_GmailMailboxScan, _GraphFolderScan):
                with self.subTest(status=status, scan=scan_type.__name__):
                    scan = scan_type.__new__(scan_type)

                    def unavailable(status=status):
                        raise ProviderHttpError(status, "scan-wide failure")
                        yield b""

                    with self.assertRaises(ScanWideProviderError):
                        list(scan._raw_chunks("message-id", unavailable)())

    def test_lazy_message_http_failure_is_isolated(self) -> None:
        for scan_type in (_GmailMailboxScan, _GraphFolderScan):
            with self.subTest(scan=scan_type.__name__):
                scan = scan_type.__new__(scan_type)

                def unavailable():
                    raise ProviderHttpError(500, "message-specific failure")
                    yield b""

                with self.assertRaises(RemoteMessageError):
                    list(scan._raw_chunks("message-id", unavailable)())

    def test_gmail_skips_known_ids_before_downloading_mime(self) -> None:
        raw = b"Subject: Test\r\n\r\nBody"
        http = FakeGmailHttp(raw)
        source = GmailMessageSource(FakeOAuth(), http)
        account = Account(
            label="Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("me@gmail.com", folders=["INBOX"])],
        )

        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda source_namespace, message_id: message_id != "known",
        )
        fetched = list(messages)

        self.assertEqual(namespace.processing_namespace, "gmail_api-mailbox:me@gmail.com")
        self.assertEqual(
            fetched,
            [
                RemoteMessage(
                    id="new", raw=raw, received_at=RECEIVED, received_origin="gmail_internal_date"
                )
            ],
        )
        self.assertEqual(sum("?format=raw" in url for url in http.urls), 1)

    def test_gmail_follows_pagination_and_url_encodes_message_ids(self) -> None:
        raw = b"Subject: Paged\r\n\r\nBody"
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        http = SequencedHttp(
            [
                {"messages": [{"id": "known"}], "nextPageToken": "next page"},
                {"messages": [{"id": "new/id"}]},
            ],
            {"new%2Fid": {"raw": encoded}},
        )
        source = GmailMessageSource(FakeOAuth(), http)
        account = Account(
            label="Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("me@gmail.com", folders=[" Important "])],
        )

        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda _namespace, message_id: message_id != "known",
        )

        self.assertEqual(namespace.processing_namespace, "gmail_api-mailbox:me@gmail.com")
        self.assertEqual(
            list(messages),
            [
                RemoteMessage(
                    id="new/id",
                    raw=raw,
                    received_at=RECEIVED,
                    received_origin="gmail_internal_date",
                )
            ],
        )
        self.assertIn("pageToken=next+page", http.json_calls[1][0])
        self.assertIn("/messages/new%2Fid?format=raw", http.json_calls[2][0])
        self.assertTrue(all(call[1] == "google-token" for call in http.json_calls))

    def test_gmail_rejects_missing_or_invalid_mime_data(self) -> None:
        account = Account(
            label="Gmail",
            username="me@gmail.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("me@gmail.com", folders=["INBOX"])],
        )
        scenarios = (
            ({"raw": ""}, "did not contain MIME data"),
            ({"raw": "a"}, "contained invalid MIME data"),
        )
        for response, message in scenarios:
            with self.subTest(response=response):
                http = SequencedHttp(
                    [{"messages": [{"id": "broken"}]}],
                    {"broken": response},
                )
                _, messages = GmailMessageSource(FakeOAuth(), http).fetch_messages(
                    mail_target(account),
                    lambda _namespace, _message_id: True,
                )

                fetched = list(messages)
                self.assertEqual(len(fetched), 1)
                self.assertIsInstance(fetched[0].error, RemoteMessageError)
                self.assertRegex(str(fetched[0].error), message)

    def test_graph_application_access_targets_configured_mailbox(self) -> None:
        raw = b"Subject: Test\r\n\r\nBody"
        http = FakeGraphHttp(raw)
        source = MicrosoftGraphMessageSource(FakeOAuth(), http)
        account = Account(
            label="Microsoft 365",
            username="archive@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="client-id",
            tenant_id="tenant-id",
            mailboxes=[Mailbox("archive@example.com", folders=["inbox"])],
        )

        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda source_namespace, message_id: message_id != "known",
        )
        fetched = list(messages)

        self.assertEqual(
            namespace.processing_namespace, "microsoft_graph-mailbox:archive@example.com"
        )
        self.assertEqual(
            fetched,
            [
                RemoteMessage(
                    id="new",
                    raw=raw,
                    received_at=RECEIVED,
                    received_origin="graph_received_date_time",
                )
            ],
        )
        self.assertIn("/users/archive%40example.com/", http.json_urls[0])
        self.assertEqual(len(http.byte_urls), 1)

    def test_graph_follows_odata_next_link_and_uses_mime_headers(self) -> None:
        http = SequencedHttp(
            [
                {
                    "value": [{"id": "known"}],
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/me/mailFolders/"
                        "%20Custom%2FFolder%20/messages?$skiptoken=second-page"
                    ),
                },
                {"value": [{"id": "new/id"}]},
            ],
            {"new%2Fid": b"Subject: Graph\r\n\r\nBody"},
        )
        source = MicrosoftGraphMessageSource(FakeOAuth(), http)
        account = Account(
            label="Microsoft 365",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("me@example.com", folders=[" Custom/Folder "])],
        )

        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda _namespace, message_id: message_id != "known",
        )

        self.assertEqual(namespace.processing_namespace, "microsoft_graph-mailbox:me@example.com")
        self.assertEqual(
            list(messages),
            [
                RemoteMessage(
                    id="new/id",
                    raw=b"Subject: Graph\r\n\r\nBody",
                    received_at=RECEIVED,
                    received_origin="graph_received_date_time",
                )
            ],
        )
        self.assertIn("/me/mailFolders/%20Custom%2FFolder%20/messages?", http.json_calls[0][0])
        self.assertEqual(
            http.json_calls[1][0],
            "https://graph.microsoft.com/v1.0/me/mailFolders/"
            "%20Custom%2FFolder%20/messages?$skiptoken=second-page",
        )
        self.assertEqual(http.byte_calls[0][2]["Accept"], "message/rfc822")
        self.assertEqual(http.byte_calls[0][2]["Prefer"], 'IdType="ImmutableId"')

    def test_nonstreaming_graph_body_404_keeps_loaded_reception_metadata(self) -> None:
        class MissingBodyHttp(FakeGraphHttp):
            def get_bytes(self, url, access_token, headers=None):
                raise ProviderHttpError(404, "message disappeared")

        source = MicrosoftGraphMessageSource(FakeOAuth(), MissingBodyHttp(b""))
        account = Account(
            label="Microsoft 365",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
        )

        _namespace, messages = source.fetch_messages(
            mail_target(account), lambda _namespace, message_id: message_id == "new"
        )
        message = list(messages)[0]

        self.assertEqual(message.received_at, RECEIVED)
        self.assertEqual(message.received_origin, "graph_received_date_time")
        with self.assertRaises(RemoteMessageUnavailable):
            list(message.iter_raw())

    def test_imap_source_requires_password_and_translates_namespace(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        oauth = FakeOAuth()
        source = ImapMessageSource(store, FakeImapMailbox(), oauth)

        with self.assertRaisesRegex(MailboxError, "No password is stored"):
            source.fetch_messages(mail_target(account), lambda _namespace, _uid: True)

        update_credential_data(store, account.id, password="secret")
        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda source_namespace, uid: (
                source_namespace.processing_namespace == imap_namespace(account, "42")
                and uid == "7"
            ),
        )

        self.assertEqual(namespace.processing_namespace, imap_namespace(account, "42"))
        self.assertEqual(list(messages), [RemoteMessage(id="7", raw=b"mail")])
        self.assertTrue(source.mailbox.arguments[2](namespace, "7"))
        self.assertEqual(source.mailbox.arguments[1], "secret")
        self.assertIsNone(source.mailbox.arguments[3])
        self.assertEqual(oauth.microsoft_accounts, [])

        direct = source.fetch_message(mail_target(account), "7", imap_namespace(account, "42"))
        self.assertEqual(direct.raw, b"direct mail")
        self.assertEqual(
            source.mailbox.direct_arguments[1:4], ("7", imap_namespace(account, "42"), "secret")
        )

    def test_imap_source_uses_microsoft_oauth_without_loading_a_password(self) -> None:
        store = MemoryCredentialStore()
        account = Account(
            label="Outlook IMAP",
            host="outlook.office365.com",
            username="me@example.com",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
        )
        oauth = FakeOAuth()
        mailbox = FakeImapMailbox()
        source = ImapMessageSource(store, mailbox, oauth)
        update_credential_data(store, account.id, password="must-not-be-used")

        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda source_namespace, uid: (
                source_namespace.processing_namespace == imap_namespace(account, "42")
                and uid == "7"
            ),
        )

        self.assertEqual(namespace.processing_namespace, imap_namespace(account, "42"))
        self.assertEqual(list(messages), [RemoteMessage(id="7", raw=b"mail")])
        self.assertEqual(oauth.microsoft_accounts, [account])
        self.assertIsNone(mailbox.arguments[1])
        self.assertEqual(mailbox.arguments[3], "microsoft-token")
        self.assertTrue(mailbox.arguments[2](namespace, "7"))
        self.assertEqual(mailbox.refresh_access_token(), "microsoft-refreshed-token")
        self.assertEqual(oauth.microsoft_accounts, [account, account])
        self.assertEqual(oauth.microsoft_force_refresh, [False, True])

    def test_imap_source_rejects_application_authentication(self) -> None:
        account = Account(
            label="IMAP application",
            host="imap.example.org",
            username="me@example.org",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        oauth = FakeOAuth()
        source = ImapMessageSource(MemoryCredentialStore(), FakeImapMailbox(), oauth)

        with self.assertRaisesRegex(MailboxError, "does not support application"):
            source.fetch_messages(mail_target(account), lambda _namespace, _uid: True)

        self.assertEqual(oauth.microsoft_accounts, [])

    def test_http_client_rejects_invalid_or_non_object_json(self) -> None:
        client = HttpClient()
        for payload, message in (
            (b"not-json", "invalid JSON response"),
            (b"[]", "unexpected response"),
        ):
            with (
                self.subTest(payload=payload),
                patch.object(
                    client,
                    "get_bytes",
                    return_value=payload,
                ),
            ):
                with self.assertRaisesRegex(MailboxError, message):
                    client.get_json("https://provider.example/messages", "token")

    def test_http_client_maps_http_and_network_errors(self) -> None:
        client = HttpClient()
        http_error = HTTPError(
            "https://provider.example/messages",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"rate limit"),
        )
        with patch("mailarchive.mail_sources.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(MailboxError, "HTTP 429: rate limit"):
                client.get_bytes("https://provider.example/messages", "token")

        with patch(
            "mailarchive.mail_sources.urlopen",
            side_effect=URLError("offline"),
        ):
            with self.assertRaisesRegex(MailboxError, "offline"):
                client.get_bytes("https://provider.example/messages", "token")

    def test_http_client_streams_in_bounded_chunks_and_enforces_declared_size(self) -> None:
        class Response(io.BytesIO):
            def __init__(self, payload, declared):
                super().__init__(payload)
                self.headers = {"Content-Length": str(declared)}
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

        response = Response(b"abcdefg", 7)
        with (
            patch("mailarchive.mail_sources.MESSAGE_CHUNK_BYTES", 3),
            patch("mailarchive.mail_sources.urlopen", return_value=response),
        ):
            chunks = list(
                HttpClient().iter_bytes("https://provider.example/message", "token", max_bytes=7)
            )
        self.assertEqual(chunks, [b"abc", b"def", b"g"])
        self.assertTrue(all(size == 3 for size in response.read_sizes))

        truncated = Response(b"short", 999)
        with patch("mailarchive.mail_sources.urlopen", return_value=truncated):
            with self.assertRaisesRegex(MailboxError, "ended before its declared size"):
                list(HttpClient().iter_bytes("https://provider.example/message", "token"))

        overlong = Response(b"abcd", 3)
        with patch("mailarchive.mail_sources.urlopen", return_value=overlong):
            with self.assertRaisesRegex(MailboxError, "exceeded its declared size"):
                list(HttpClient().iter_bytes("https://provider.example/message", "token"))

        for invalid_length in ("+3", " 3", "3 ", "3_0", ""):
            invalid = Response(b"abc", 3)
            invalid.headers["Content-Length"] = invalid_length
            with (
                self.subTest(content_length=invalid_length),
                patch("mailarchive.mail_sources.urlopen", return_value=invalid),
            ):
                with self.assertRaisesRegex(MailboxError, "invalid response size"):
                    list(HttpClient().iter_bytes("https://provider.example/message", "token"))

        ambiguous = Response(b"abc", 3)
        ambiguous.headers = Message()
        ambiguous.headers["Content-Length"] = "3"
        ambiguous.headers["Content-Length"] = "999"
        with patch("mailarchive.mail_sources.urlopen", return_value=ambiguous):
            with self.assertRaisesRegex(MailboxError, "ambiguous response sizes"):
                list(HttpClient().iter_bytes("https://provider.example/message", "token"))

        oversized = Response(b"unused", 8)
        with patch("mailarchive.mail_sources.urlopen", return_value=oversized):
            with self.assertRaisesRegex(MailboxError, "exceeds its size limit"):
                list(
                    HttpClient().iter_bytes(
                        "https://provider.example/message", "token", max_bytes=7
                    )
                )
        self.assertEqual(oversized.read_sizes, [])

        raw_oversized = Response(b"unused", 8)
        with patch("mailarchive.mail_sources.urlopen", return_value=raw_oversized):
            with self.assertRaisesRegex(IntakeCapacityError, "exceeds its size limit"):
                list(
                    HttpClient().iter_bytes(
                        "https://provider.example/message",
                        "token",
                        max_bytes=7,
                        capacity_error=True,
                    )
                )

        gmail_raw = b"Subject: complete\r\n\r\nbody"
        gmail_wire = b'{"raw":"' + base64.urlsafe_b64encode(gmail_raw).rstrip(b"=") + b'"}'
        truncated_gmail = Response(gmail_wire, len(gmail_wire) + 1)
        with patch("mailarchive.mail_sources.urlopen", return_value=truncated_gmail):
            with self.assertRaisesRegex(MailboxError, "ended before its declared size"):
                b"".join(HttpClient().iter_gmail_raw("https://gmail.example/message", "token"))

    def test_gmail_raw_decoder_handles_arbitrary_transport_boundaries(self) -> None:
        raw = b"Subject: streamed\r\n\r\nbody with bytes \x00\xff"
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
        wire = b'{"raw":"' + encoded + b'","ignored":true}'
        boundaries = (1, 2, 5, 3, 7)
        chunks = []
        offset = 0
        for size in boundaries:
            chunks.append(wire[offset : offset + size])
            offset += size
        chunks.extend(wire[index : index + 4] for index in range(offset, len(wire), 4))
        client = HttpClient()
        with patch.object(client, "iter_bytes", return_value=iter(chunks)):
            decoded = b"".join(client.iter_gmail_raw("https://gmail.example/message", "token"))

        self.assertEqual(decoded, raw)


if __name__ == "__main__":
    unittest.main()
