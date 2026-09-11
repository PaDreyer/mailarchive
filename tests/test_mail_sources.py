import base64
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from mailarchive.credential_data import update_credential_data
from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import MailboxError, RemoteMessage
from mailarchive.mail_sources import (
    GmailMessageSource,
    HttpClient,
    ImapMessageSource,
    MicrosoftGraphMessageSource,
)
from mailarchive.models import Account, AuthMode, MailProvider


class FakeOAuth:
    def google_access_token(self, account):
        return "google-token"

    def microsoft_access_token(self, account):
        return "microsoft-token"


class FakeGmailHttp:
    def __init__(self, raw):
        self.raw = raw
        self.urls = []

    def get_json(self, url, access_token, headers=None):
        self.urls.append(url)
        if "?format=raw" in url:
            return {"raw": base64.urlsafe_b64encode(self.raw).decode("ascii").rstrip("=")}
        return {"messages": [{"id": "known"}, {"id": "new"}]}


class FakeGraphHttp:
    def __init__(self, raw):
        self.raw = raw
        self.json_urls = []
        self.byte_urls = []

    def get_json(self, url, access_token, headers=None):
        self.json_urls.append(url)
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
            return self.raw_by_id[message_id]
        return next(self.pages)

    def get_bytes(self, url, access_token, headers=None):
        self.byte_calls.append((url, access_token, headers))
        message_id = url.split("/messages/", 1)[1].split("/", 1)[0]
        return self.raw_by_id[message_id]


class FakeImapMailbox:
    def __init__(self):
        self.arguments = None

    def fetch_messages(self, account, password, should_fetch):
        self.arguments = (account, password, should_fetch)
        return "42", iter([RemoteMessage(id="7", raw=b"mail")])


class MailSourceTests(unittest.TestCase):
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
        )

        namespace, messages = source.fetch_messages(
            account,
            lambda source_namespace, message_id: message_id != "known",
        )
        fetched = list(messages)

        self.assertEqual(namespace, "gmail-api:INBOX")
        self.assertEqual(fetched, [RemoteMessage(id="new", raw=raw)])
        self.assertEqual(sum("?format=raw" in url for url in http.urls), 1)

    def test_gmail_follows_pagination_and_url_encodes_message_ids(self) -> None:
        raw = b"Subject: Paged\r\n\r\nBody"
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        http = SequencedHttp(
            [
                {"messages": [{"id": ""}, {"id": "known"}], "nextPageToken": "next page"},
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
            folder=" Important ",
        )

        namespace, messages = source.fetch_messages(
            account,
            lambda _namespace, message_id: message_id != "known",
        )

        self.assertEqual(namespace, "gmail-api:Important")
        self.assertEqual(list(messages), [RemoteMessage(id="new/id", raw=raw)])
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
                    account,
                    lambda _namespace, _message_id: True,
                )

                with self.assertRaisesRegex(MailboxError, message):
                    list(messages)

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
            folder="inbox",
        )

        namespace, messages = source.fetch_messages(
            account,
            lambda source_namespace, message_id: message_id != "known",
        )
        fetched = list(messages)

        self.assertEqual(namespace, "microsoft-graph:inbox")
        self.assertEqual(fetched, [RemoteMessage(id="new", raw=raw)])
        self.assertIn("/users/archive%40example.com/", http.json_urls[0])
        self.assertEqual(len(http.byte_urls), 1)

    def test_graph_follows_odata_next_link_and_uses_mime_headers(self) -> None:
        http = SequencedHttp(
            [
                {
                    "value": [{"id": "known"}, {"id": ""}],
                    "@odata.nextLink": "https://graph.example/second-page",
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
            folder=" Custom/Folder ",
        )

        namespace, messages = source.fetch_messages(
            account,
            lambda _namespace, message_id: message_id != "known",
        )

        self.assertEqual(namespace, "microsoft-graph:Custom/Folder")
        self.assertEqual(
            list(messages),
            [RemoteMessage(id="new/id", raw=b"Subject: Graph\r\n\r\nBody")],
        )
        self.assertIn("/me/mailFolders/Custom%2FFolder/messages?", http.json_calls[0][0])
        self.assertEqual(http.json_calls[1][0], "https://graph.example/second-page")
        self.assertEqual(http.byte_calls[0][2]["Accept"], "message/rfc822")
        self.assertEqual(http.byte_calls[0][2]["Prefer"], 'IdType="ImmutableId"')

    def test_imap_source_requires_password_and_translates_namespace(self) -> None:
        store = MemoryCredentialStore()
        account = Account("Personal", "imap.example.org", "me@example.org")
        source = ImapMessageSource(store, FakeImapMailbox())

        with self.assertRaisesRegex(MailboxError, "No password is stored"):
            source.fetch_messages(account, lambda _namespace, _uid: True)

        update_credential_data(store, account.id, password="secret")
        namespace, messages = source.fetch_messages(
            account,
            lambda source_namespace, uid: source_namespace == "imap:42" and uid == "7",
        )

        self.assertEqual(namespace, "imap:42")
        self.assertEqual(list(messages), [RemoteMessage(id="7", raw=b"mail")])
        self.assertTrue(source.mailbox.arguments[2]("42", "7"))

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


if __name__ == "__main__":
    unittest.main()
