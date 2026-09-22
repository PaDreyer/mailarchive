"""Provider response validation and cursor safety contracts."""

import unittest
from unittest.mock import Mock

from mailarchive.imap_client import MailboxError, RemoteMessageError
from mailarchive.mail_sources import (
    GmailMessageSource,
    MicrosoftGraphMessageSource,
    ProviderHttpError,
)
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from mailarchive.synchronization import SyncSession
from tests.helpers import mail_target
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_mail_sources import FakeOAuth
from tests.test_synchronization import ScriptedHttp


class ProviderContractTests(unittest.TestCase):
    def gmail(self, steps):
        account = Account(
            "Gmail",
            username="me@example.org",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
            mailboxes=[Mailbox("me@example.org", ["INBOX"])],
        )
        http = ScriptedHttp(steps)
        return GmailMessageSource(FakeOAuth(), http), mail_target(account), http

    def graph(self, steps):
        account = Account(
            "Graph",
            username="me@example.org",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.org", ["INBOX"])],
        )
        http = ScriptedHttp(steps)
        return MicrosoftGraphMessageSource(FakeOAuth(), http), mail_target(account), http

    def imap(self, connection):
        account = Account(
            "IMAP",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", ["INBOX"])],
        )
        return FakeImapMailbox(connection), mail_target(account)

    def test_imap_missing_uidvalidity_does_not_search(self):
        connection = FakeImapConnection(validity_data=[])
        mailbox, target = self.imap(connection)
        with self.assertRaises(MailboxError):
            mailbox.fetch_messages(target, "password")
        self.assertFalse(any(call[0] == "uid" for call in connection.calls))

    def test_imap_invalid_search_uid_does_not_download(self):
        connection = FakeImapConnection(uids=b"invalid")
        mailbox, target = self.imap(connection)
        with self.assertRaises(MailboxError):
            mailbox.fetch_messages(target, "password")
        self.assertFalse(any(call[:2] == ("uid", "fetch") for call in connection.calls))

    def test_imap_uidvalidity_change_during_download_is_visible(self):
        connection = FakeImapConnection(validity_responses=[[b"9001"], [b"9002"]])
        mailbox, target = self.imap(connection)
        with self.assertRaises(MailboxError):
            mailbox.fetch_messages(target, "password")
        self.assertTrue(connection.logged_out)

    def test_gmail_invalid_history_id_is_rejected(self):
        source, target, http = self.gmail(
            [("json", "/history?startHistoryId=100", {"historyId": "not-a-number"})]
        )
        sync = SyncSession(lambda _: "100", lambda _: set())
        _, messages = source.fetch_messages(target, lambda *_: True, sync=sync)
        with self.assertRaises(MailboxError):
            list(messages)
        self.assertIsNone(sync.next_cursor)
        self.assertEqual(http.steps, [])

    def test_gmail_repeating_page_token_is_rejected(self):
        source, target, http = self.gmail(
            [
                ("json", "/messages?", {"messages": [], "nextPageToken": "repeat"}),
                ("json", "pageToken=repeat", {"messages": [], "nextPageToken": "repeat"}),
            ]
        )
        _, messages = source.fetch_messages(target, lambda *_: True)
        with self.assertRaises(MailboxError):
            list(messages)
        self.assertEqual(http.steps, [])

    def test_gmail_expired_cursor_reconciles_with_full_listing(self):
        source, target, http = self.gmail(
            [
                ("json", "/history?startHistoryId=100", ProviderHttpError(404, "expired")),
                ("json", "/profile?fields=historyId", {"historyId": "200"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        reset = Mock()
        sync = SyncSession(lambda _: "100", lambda _: set(), report_reset=reset)
        _, messages = source.fetch_messages(target, lambda *_: True, sync=sync)
        self.assertEqual(list(messages), [])
        reset.assert_called_once_with()
        self.assertEqual(sync.next_cursor, "200")
        self.assertEqual(http.steps, [])

    def test_graph_rejects_untrusted_saved_cursor_before_http_request(self):
        source, target, http = self.graph([])
        sync = SyncSession(lambda _: "https://evil.example/messages", lambda _: set())
        _, messages = source.fetch_messages(target, lambda *_: True, sync=sync)
        with self.assertRaises(MailboxError):
            list(messages)
        self.assertEqual(http.calls, [])

    def test_graph_rejects_untrusted_continuation_link(self):
        source, target, http = self.graph(
            [("json", "/messages?", {"value": [], "@odata.nextLink": "https://evil.example/page"})]
        )
        _, messages = source.fetch_messages(target, lambda *_: True)
        with self.assertRaises(MailboxError):
            list(messages)
        self.assertEqual(len(http.calls), 1)

    def test_graph_requires_completion_delta_link(self):
        source, target, http = self.graph([("json", "/messages/delta?", {"value": []})])
        sync = SyncSession(lambda _: None, lambda _: set())
        _, messages = source.fetch_messages(target, lambda *_: True, sync=sync)
        with self.assertRaises(MailboxError):
            list(messages)
        self.assertIsNone(sync.next_cursor)
        self.assertEqual(http.steps, [])

    def test_graph_missing_reception_metadata_is_an_error(self):
        source, target, http = self.graph(
            [
                ("json", "/messages?", {"value": [{"id": "first"}]}),
                ("json", "/messages/first?$select=receivedDateTime", {}),
            ]
        )
        _, messages = source.fetch_messages(target, lambda *_: True)
        fetched = list(messages)
        self.assertEqual(len(fetched), 1)
        self.assertIsInstance(fetched[0].error, RemoteMessageError)
        self.assertIn("receivedDateTime", str(fetched[0].error))
        self.assertEqual(http.steps, [])


if __name__ == "__main__":
    unittest.main()
