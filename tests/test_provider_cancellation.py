"""Provider cancellation stops pagination, retries and streaming at safe boundaries."""

import threading
import unittest
from unittest.mock import Mock, patch

from mailarchive.application.cancellation import Cancellation, ProcessingStopped
from mailarchive.application.synchronization import SyncSession
from mailarchive.domain.configuration import Account, Mailbox, MailProvider
from mailarchive.domain.source_identity import MailTarget
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.http import (
    HttpClient,
    ProviderHttpError,
    _OAuthHttpSession,
)
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests.helpers import mail_target, sample_mail
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_restart_providers import OAuth


class ProviderCancellationTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.cancellation = Cancellation(self.stop.is_set)

    def target(self, provider):
        mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = Account(
            "Mail", "imap.example.org", mailbox.address, mailboxes=[mailbox], provider=provider
        )
        return MailTarget(account, mailbox, "INBOX", ("INBOX",))

    def test_http_stop_during_chunk_read_closes_response_without_reading_again(self):
        response = Mock()
        response.headers = {}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        def read(_size):
            self.stop.set()
            return b"partial"

        response.read.side_effect = read
        with patch("mailarchive.infrastructure.providers.http.urlopen", return_value=response):
            with self.assertRaises(ProcessingStopped):
                list(
                    HttpClient().iter_bytes(
                        "https://graph.microsoft.com/raw", "token", cancellation=self.cancellation
                    )
                )
        response.read.assert_called_once()
        response.__exit__.assert_called_once()

    def test_http_stop_after_401_prevents_token_refresh_and_request_retry(self):
        http = Mock()
        refresh = Mock()

        def expired(*args, **kwargs):
            self.stop.set()
            raise ProviderHttpError(401, "expired")

        http.get_json.side_effect = expired
        session = _OAuthHttpSession(http, "token", refresh, cancellation=self.cancellation)
        with self.assertRaises(ProcessingStopped):
            session.get_json("https://graph.microsoft.com/messages")
        refresh.assert_not_called()
        http.get_json.assert_called_once()

    def test_stream_stop_after_401_prevents_token_refresh(self):
        http = Mock()
        refresh = Mock()

        def expired(*args, **kwargs):
            self.stop.set()
            raise ProviderHttpError(401, "expired")
            yield b""

        http.iter_bytes.side_effect = expired
        session = _OAuthHttpSession(http, "token", refresh, cancellation=self.cancellation)
        with self.assertRaises(ProcessingStopped):
            list(session.message_chunks("https://graph.microsoft.com/raw")())
        refresh.assert_not_called()

    def test_graph_stop_on_empty_page_prevents_next_page_and_cursor_completion(self):
        http = Mock(spec=["get_json", "get_bytes"])
        target = self.target(MailProvider.MICROSOFT_GRAPH)
        sync = SyncSession(cursor_for=lambda _: None, recheck_ids_for=lambda _: set())

        def page(*args, **kwargs):
            self.stop.set()
            return {
                "value": [],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/INBOX/messages/delta?$skiptoken=next",
            }

        http.get_json.side_effect = page
        source = MicrosoftGraphMessageSource(OAuth(), http)
        _, messages = source.fetch_messages(
            target, lambda *_: True, sync=sync, cancellation=self.cancellation
        )
        with self.assertRaises(ProcessingStopped):
            list(messages)
        http.get_json.assert_called_once()
        self.assertIsNone(sync.next_cursor)

    def test_graph_stop_during_folder_discovery_prevents_child_folder_requests(self):
        http = Mock(spec=["get_json", "get_bytes"])
        target = self.target(MailProvider.MICROSOFT_GRAPH)
        target.mailbox.folders.clear()

        def folders(*args, **kwargs):
            self.stop.set()
            return {"value": [{"id": "parent", "childFolderCount": 1}]}

        http.get_json.side_effect = folders
        with self.assertRaises(ProcessingStopped):
            MicrosoftGraphMessageSource(OAuth(), http).targets(
                target.account, target.mailbox, cancellation=self.cancellation
            )
        http.get_json.assert_called_once()

    def test_gmail_stop_during_baseline_prevents_listing_and_cursor_completion(self):
        http = Mock(spec=["get_json", "get_bytes"])
        sync = SyncSession(cursor_for=lambda _: None, recheck_ids_for=lambda _: set())

        def profile(*args, **kwargs):
            self.stop.set()
            return {"historyId": "123"}

        http.get_json.side_effect = profile
        source = GmailMessageSource(OAuth(), http)
        _, messages = source.fetch_messages(
            self.target(MailProvider.GMAIL_API),
            lambda *_: True,
            sync=sync,
            cancellation=self.cancellation,
        )
        with self.assertRaises(ProcessingStopped):
            list(messages)
        http.get_json.assert_called_once()
        self.assertIsNone(sync.next_cursor)

    def test_imap_stop_during_download_prevents_next_body_request_and_closes_session(self):
        connection = FakeImapConnection(raw_by_uid={b"77": sample_mail()})
        mailbox = FakeImapMailbox(connection)
        sync = SyncSession(cursor_for=lambda _: None, recheck_ids_for=lambda _: set())
        _, messages = mailbox.fetch_messages(
            mail_target(Account("Mail", "imap.example.org", "owner@example.org")),
            "secret",
            sync=sync,
            cancellation=self.cancellation,
        )
        remote = next(messages)
        chunks = remote.iter_raw()
        next(chunks)
        self.stop.set()
        with self.assertRaises(ProcessingStopped):
            next(chunks)
        messages.close()
        body_calls = [
            call
            for call in connection.calls
            if call[0:2] == ("uid", "fetch") and "BODY.PEEK" in str(call[-1])
        ]
        self.assertEqual(len(body_calls), 1)
        self.assertTrue(connection.closed and connection.logged_out)
        self.assertIsNone(sync.next_cursor)

    def test_imap_stop_after_search_prevents_metadata_requests(self):
        connection = FakeImapConnection()
        uid = connection.uid

        def stop_after_search(command, *args):
            result = uid(command, *args)
            if command == "search":
                self.stop.set()
            return result

        connection.uid = stop_after_search
        with self.assertRaises(ProcessingStopped):
            FakeImapMailbox(connection).fetch_messages(
                mail_target(Account("Mail", "imap.example.org", "owner@example.org")),
                "secret",
                cancellation=self.cancellation,
            )
        self.assertFalse(any(call[0:2] == ("uid", "fetch") for call in connection.calls))
        self.assertTrue(connection.closed and connection.logged_out)

    def test_imap_stop_after_connect_prevents_starttls_and_closes_socket(self):
        client = Mock()

        def connect(*args, **kwargs):
            self.stop.set()
            return client

        account = Account("Mail", "imap.example.org", "owner@example.org", use_ssl=False)
        with (
            patch(
                "mailarchive.infrastructure.providers.imap_client.imaplib.IMAP4",
                side_effect=connect,
            ),
            self.assertRaises(ProcessingStopped),
        ):
            ImapMailbox()._connect(account, cancellation=self.cancellation)
        client.starttls.assert_not_called()
        client.shutdown.assert_called_once()
