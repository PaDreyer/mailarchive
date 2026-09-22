from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mailarchive.imap_client import ImapMailbox, MailboxError
from mailarchive.mail_identity import MailTarget
from mailarchive.mail_sources import (
    GmailMessageSource,
    MicrosoftGraphMessageSource,
    ProviderHttpError,
)
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider, Rule, RuleTarget, Settings
from mailarchive.service import ArchiveService
from mailarchive.synchronization import RangePagination
from mailarchive.workspace import WorkspaceStore


class OAuth:
    def google_access_token(self, *_args, **_kwargs):
        return "token"

    def microsoft_access_token(self, *_args, **_kwargs):
        return "token"


class GmailHttp:
    def __init__(self):
        self.urls = []

    def get_json(self, url, _token, _headers=None):
        self.urls.append(url)
        if "format=raw" in url:
            return {
                "raw": base64.urlsafe_b64encode(b"Subject: hi\r\n\r\nBody").decode(),
                "internalDate": "1767225600000",
            }
        return {"messages": [{"id": "mail-1"}]}


class GraphHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, _token, headers=None):
        self.requests.append((url, headers))
        if "$select=receivedDateTime" in url:
            return {"receivedDateTime": "2026-01-01T00:00:00Z"}
        return {"value": [{"id": "immutable-id"}]}

    def get_bytes(self, url, _token, headers=None):
        self.requests.append((url, headers))
        return b"Subject: hi\r\n\r\nBody"


class CheckpointRecorder:
    def __init__(self) -> None:
        self.namespace = None
        self.token = None
        self.complete = False

    def save(self, namespace, token, complete, _force):
        self.namespace = namespace
        self.token = token
        self.complete = complete
        return True

    def session(self) -> RangePagination:
        return RangePagination(self.namespace, self.token, self.save)


class PagedGmailHttp(GmailHttp):
    def __init__(self) -> None:
        super().__init__()
        self.fail_second_page = True

    def get_json(self, url, _token, _headers=None):
        self.urls.append(url)
        if "/messages?" in url:
            page_token = parse_qs(urlsplit(url).query).get("pageToken")
            if not page_token:
                return {"messages": [{"id": "mail-1"}], "nextPageToken": "page-2"}
            if self.fail_second_page:
                self.fail_second_page = False
                raise ProviderHttpError(500, "page failed")
            return {"messages": [{"id": "mail-2"}]}
        if "format=raw" in url:
            return {
                "raw": base64.urlsafe_b64encode(b"Subject: hi\r\n\r\nBody").decode(),
                "internalDate": "1767225600000",
            }
        raise AssertionError(url)


class PagedGraphHttp(GraphHttp):
    NEXT_PAGE = (
        "https://graph.microsoft.com/v1.0/me/mailFolders/folder%20id/messages?$skiptoken=page-2"
    )

    def __init__(self) -> None:
        super().__init__()
        self.fail_second_page = True

    def get_json(self, url, _token, headers=None):
        self.requests.append((url, headers))
        if "$select=receivedDateTime" in url:
            return {"receivedDateTime": "2026-01-01T00:00:00Z"}
        if url == self.NEXT_PAGE:
            if self.fail_second_page:
                self.fail_second_page = False
                raise ProviderHttpError(500, "page failed")
            return {"value": [{"id": "mail-2"}]}
        return {"value": [{"id": "mail-1"}], "@odata.nextLink": self.NEXT_PAGE}


class ImapConnection:
    def __init__(self):
        self.search = None
        self.validity_reads = 0

    def login(self, *_args):
        return "OK", [b""]

    def select(self, *_args, **_kwargs):
        return "OK", [b"1"]

    def response(self, name):
        assert name == "UIDVALIDITY"
        self.validity_reads += 1
        return name, [b"42"] if self.validity_reads == 1 else [None]

    def uid(self, command, *args):
        if command == "search":
            self.search = args[-1]
            return "OK", [b"7"]
        assert command == "fetch"
        raw = b"Subject: hi\r\n\r\nBody"
        if args[-1] == "(RFC822.SIZE INTERNALDATE)":
            return "OK", [
                b"1 (UID 7 RFC822.SIZE "
                + str(len(raw)).encode()
                + b' INTERNALDATE "01-Jan-2026 01:30:00 +0100")'
            ]
        return "OK", [(b"1 (BODY[])", raw)]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


class Imap(ImapMailbox):
    def __init__(self):
        self.connection = ImapConnection()

    def _connect(self, _account):
        return self.connection


class RestartProviderTests(unittest.TestCase):
    def test_gmail_range_resumes_from_saved_page_token(self):
        mailbox = Mailbox("owner@example.org", folders=["Selected"])
        account = Account(
            "Gmail",
            username=mailbox.address,
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
            mailboxes=[mailbox],
        )
        http = PagedGmailHttp()
        source = GmailMessageSource(OAuth(), http)
        checkpoint = CheckpointRecorder()

        _, first = source.search_messages(
            MailTarget(account, mailbox, ""),
            lambda _scope, _id: True,
            None,
            None,
            range_sync=checkpoint.session(),
        )
        with self.assertRaises(ProviderHttpError):
            list(first)
        _, resumed = source.search_messages(
            MailTarget(account, mailbox, ""),
            lambda _scope, _id: True,
            None,
            None,
            range_sync=checkpoint.session(),
        )

        self.assertEqual([message.id for message in resumed], ["mail-2"])
        self.assertTrue(checkpoint.complete)
        list_urls = [url for url in http.urls if "/messages?" in url]
        self.assertEqual(parse_qs(urlsplit(list_urls[-1]).query)["pageToken"], ["page-2"])

    def test_graph_range_resumes_from_saved_next_link(self):
        mailbox = Mailbox("owner@example.org", folders=["folder id"])
        account = Account(
            "Graph",
            username=mailbox.address,
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[mailbox],
        )
        http = PagedGraphHttp()
        source = MicrosoftGraphMessageSource(OAuth(), http)
        checkpoint = CheckpointRecorder()

        _, first = source.search_messages(
            MailTarget(account, mailbox, "folder id"),
            lambda _scope, _id: True,
            None,
            None,
            range_sync=checkpoint.session(),
        )
        with self.assertRaises(ProviderHttpError):
            list(first)
        request_count = len(http.requests)
        _, resumed = source.search_messages(
            MailTarget(account, mailbox, "folder id"),
            lambda _scope, _id: True,
            None,
            None,
            range_sync=checkpoint.session(),
        )

        self.assertEqual([message.id for message in resumed], ["mail-2"])
        self.assertEqual(http.requests[request_count][0], PagedGraphHttp.NEXT_PAGE)
        self.assertTrue(checkpoint.complete)

    def test_graph_rejects_a_saved_next_link_for_another_folder(self):
        mailbox = Mailbox("owner@example.org", folders=["folder-a"])
        account = Account(
            "Graph",
            username=mailbox.address,
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[mailbox],
        )
        http = GraphHttp()
        source = MicrosoftGraphMessageSource(OAuth(), http)
        checkpoint = CheckpointRecorder()
        checkpoint.namespace = MailTarget(account, mailbox, "folder-a").mailbox_namespace
        checkpoint.token = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/folder-b/messages?$skiptoken=foreign"
        )
        _, messages = source.search_messages(
            MailTarget(account, mailbox, "folder-a"),
            lambda _scope, _id: True,
            None,
            None,
            range_sync=checkpoint.session(),
        )

        with self.assertRaisesRegex(MailboxError, "invalid synchronization link"):
            list(messages)
        self.assertEqual(http.requests, [])

    def test_gmail_all_labels_range_resumes_with_an_empty_frozen_folder_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mailbox = Mailbox("owner@example.org", folders=[])
            account = Account(
                "Gmail",
                username=mailbox.address,
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_USER,
                client_id="client",
                mailboxes=[mailbox],
            )
            settings = Settings(
                "",
                accounts=[account],
                rules=[Rule("All", targets=[RuleTarget(str(root / "archive"))])],
            )
            source = GmailMessageSource(OAuth(), PagedGmailHttp())
            registry = type("Registry", (), {"get": lambda self, _account: source})()
            state = WorkspaceStore(root / "workspace.sqlite3")
            service = ArchiveService(None, state, source_registry=registry)

            first = service.run_range(settings, {mailbox.id})[0]
            run_id = str(state.incomplete_manual_runs()[0]["id"])
            resumed = service.resume_range_run(run_id)

            self.assertEqual((first.archived, first.failed), (1, 1))
            self.assertEqual((resumed.archived, resumed.failed), (1, 0))
            self.assertEqual(len(list((root / "archive").glob("*.eml"))), 2)

    def test_gmail_range_respects_selected_labels_through_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mailbox = Mailbox("owner@example.org", folders=["First  Label", "Second Label"])
            account = Account(
                "Gmail",
                username=mailbox.address,
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_USER,
                client_id="client",
                mailboxes=[mailbox],
            )
            settings = Settings(
                "",
                accounts=[account],
                rules=[Rule("All", targets=[RuleTarget(str(root / "archive"))])],
            )
            http = GmailHttp()
            source = GmailMessageSource(OAuth(), http)
            registry = type("Registry", (), {"get": lambda self, _account: source})()
            service = ArchiveService(
                None, WorkspaceStore(root / "workspace.sqlite3"), source_registry=registry
            )
            result = service.run_range(
                settings, {mailbox.id}, folders={mailbox.id: {"Second Label"}}
            )[0]
            self.assertEqual(result.archived, 1)
            urls = [url for url in http.urls if "/messages?" in url]
            self.assertEqual(len(urls), 1)
            self.assertEqual(parse_qs(urlsplit(urls[0]).query)["labelIds"], ["Second Label"])
            self.assertEqual(len(list((root / "archive").glob("*.eml"))), 1)

    def test_gmail_range_uses_epoch_seconds_and_internal_date(self):
        mailbox = Mailbox("owner@example.org", folders=["label with  spaces"])
        account = Account(
            "Gmail",
            username=mailbox.address,
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
            mailboxes=[mailbox],
        )
        http = GmailHttp()
        source = GmailMessageSource(OAuth(), http)
        start = datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        _, messages = source.search_messages(
            MailTarget(account, mailbox, ""), lambda _scope, _id: True, start, end
        )
        message = list(messages)[0]
        self.assertEqual(message.received_at, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(message.received_origin, "gmail_internal_date")
        query = parse_qs(urlsplit(http.urls[0]).query)
        self.assertEqual(query["labelIds"], ["label with  spaces"])
        self.assertIn("after:1767225599", query["q"][0])

    def test_graph_range_filters_received_date_and_uses_immutable_ids(self):
        mailbox = Mailbox("owner@example.org", folders=["folder id"])
        account = Account(
            "Graph",
            username=mailbox.address,
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[mailbox],
        )
        http = GraphHttp()
        source = MicrosoftGraphMessageSource(OAuth(), http)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        _, messages = source.search_messages(
            MailTarget(account, mailbox, "folder id"), lambda _scope, _id: True, start, end
        )
        message = list(messages)[0]
        self.assertEqual(message.id, "immutable-id")
        self.assertEqual(message.received_at, start)
        query = parse_qs(urlsplit(http.requests[0][0]).query)
        self.assertIn("receivedDateTime ge", query["$filter"][0])
        self.assertTrue(
            all(request[1].get("Prefer") == 'IdType="ImmutableId"' for request in http.requests)
        )

    def test_imap_range_overlaps_days_and_reads_internaldate(self):
        mailbox = Mailbox("owner@example.org", folders=["Important  Mail"])
        account = Account(
            "IMAP", host="imap.example.org", username=mailbox.address, mailboxes=[mailbox]
        )
        source = Imap()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        _, messages = source.fetch_messages(
            MailTarget(account, mailbox, "Important  Mail"),
            "password",
            lambda _scope, _id: True,
            received_between=(start, end),
        )
        message = list(messages)[0]
        self.assertEqual(message.received_at, datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(message.received_origin, "imap_internaldate")
        self.assertIn("SINCE 31-Dec-2025", source.connection.search)
        self.assertIn("BEFORE 04-Jan-2026", source.connection.search)


if __name__ == "__main__":
    unittest.main()
