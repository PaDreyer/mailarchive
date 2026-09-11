import base64
import unittest

from mailarchive.imap_client import RemoteMessage
from mailarchive.mail_sources import GmailMessageSource, MicrosoftGraphMessageSource
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


if __name__ == "__main__":
    unittest.main()
