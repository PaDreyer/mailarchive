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
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from tests.helpers import imap_namespace, mail_target


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
            mailboxes=[Mailbox("me@gmail.com", folders=["INBOX"])],
        )

        namespace, messages = source.fetch_messages(
            mail_target(account),
            lambda source_namespace, message_id: message_id != "known",
        )
        fetched = list(messages)

        self.assertEqual(namespace.processing_namespace, "gmail_api-mailbox:me@gmail.com")
        self.assertEqual(fetched, [RemoteMessage(id="new", raw=raw)])
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
        self.assertEqual(fetched, [RemoteMessage(id="new", raw=raw)])
        self.assertIn("/users/archive%40example.com/", http.json_urls[0])
        self.assertEqual(len(http.byte_urls), 1)

    def test_graph_follows_odata_next_link_and_uses_mime_headers(self) -> None:
        http = SequencedHttp(
            [
                {
                    "value": [{"id": "known"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/second-page",
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
            [RemoteMessage(id="new/id", raw=b"Subject: Graph\r\n\r\nBody")],
        )
        self.assertIn("/me/mailFolders/Custom%2FFolder/messages?", http.json_calls[0][0])
        self.assertEqual(http.json_calls[1][0], "https://graph.microsoft.com/v1.0/second-page")
        self.assertEqual(http.byte_calls[0][2]["Accept"], "message/rfc822")
        self.assertEqual(http.byte_calls[0][2]["Prefer"], 'IdType="ImmutableId"')

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


if __name__ == "__main__":
    unittest.main()
