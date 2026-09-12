import base64
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.mail_sources import (
    MessageSourceRegistry,
    MicrosoftGraphMessageSource,
    ProviderHttpError,
)
from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    Mailbox,
    MailField,
    MailProvider,
    Rule,
    Settings,
)
from mailarchive.service import ArchiveService
from mailarchive.storage import ArchiveState
from mailarchive.synchronization import SyncSession
from tests.helpers import imap_namespace, mail_target, sample_mail
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_mail_sources import FakeOAuth


class ScriptedHttp:
    """Reject unexpected requests, including accidental full listings on later runs."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def _request(self, kind, url, headers):
        self.calls.append((kind, url, headers))
        if not self.steps:
            raise AssertionError(f"Unexpected {kind} request: {url}")
        expected_kind, expected_url, response = self.steps.pop(0)
        if kind != expected_kind or expected_url not in url:
            raise AssertionError(f"Expected {expected_kind} {expected_url}, got {kind} {url}")
        if isinstance(response, Exception):
            raise response
        return response

    def get_json(self, url, access_token, headers=None):
        return self._request("json", url, headers)

    def get_bytes(self, url, access_token, headers=None):
        return self._request("bytes", url, headers)


def gmail_raw(message_id, *, subject="Invoice"):
    return (
        "json",
        f"/messages/{message_id}?format=raw",
        {
            "raw": base64.urlsafe_b64encode(sample_mail(subject=subject)).decode(),
            "labelIds": ["INBOX"],
        },
    )


def gmail_metadata(message_id, labels=None):
    return ("json", f"/messages/{message_id}?format=minimal", {"labelIds": labels or ["INBOX"]})


def gmail_history(cursor, *, history=None, next_cursor="101", next_page=None):
    page = {"historyId": next_cursor}
    if history is not None:
        page["history"] = history
    if next_page is not None:
        page["nextPageToken"] = next_page
    return ("json", f"/history?startHistoryId={cursor}", page)


def graph_delta(cursor, ids=(), *, next_cursor="next", next_page=None):
    page = {"value": [{"id": message_id} for message_id in ids]}
    if next_page:
        page["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0/{next_page}"
    else:
        page["@odata.deltaLink"] = f"https://graph.microsoft.com/v1.0/{next_cursor}"
    return ("json", cursor, page)


def graph_folder():
    return ("json", "/mailFolders/INBOX?$select=id", {"id": "folder-id"})


def graph_message(message_id, folder="folder-id"):
    return (
        "json",
        f"/messages/{message_id}?$select=parentFolderId",
        {"parentFolderId": folder},
    )


def graph_raw(message_id):
    return ("bytes", f"/messages/{message_id}/$value", sample_mail())


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = MemoryCredentialStore()
        self.state = ArchiveState(self.root / "state.sqlite3")
        self.events = []
        self.progress = []

    def configure(self, provider, *, existing=False, rules=None):
        self.account = Account(
            "Mailbox",
            "imap.example.org",
            "me@example.org",
            provider=provider,
            auth_mode=AuthMode.PASSWORD
            if provider == MailProvider.GENERIC_IMAP
            else AuthMode.OAUTH_USER,
            mailboxes=[
                Mailbox("me@example.org", folders=["INBOX"], archive_existing_messages=existing)
            ],
        )
        self.credentials.set(self.account.id, "secret")
        self.settings = Settings(
            str(self.root / "Archive"),
            accounts=[self.account],
            rules=rules if rules is not None else [Rule("All", "")],
        )

    def run_http(self, steps, *, failed=0):
        http = ScriptedHttp(steps)
        registry = MessageSourceRegistry(self.credentials, http=http)
        registry.sources[self.account.provider].oauth = FakeOAuth()
        service = ArchiveService(
            self.credentials,
            ArchiveState(self.state.database_path),
            self.events.append,
            source_registry=registry,
            progress_handler=self.progress.append,
        )
        result = service.run_once(self.settings)[0]
        self.assertEqual(result.failed, failed, [event.message for event in self.events])
        self.assertEqual(http.steps, [])
        return result, http

    def cursor(self):
        with closing(sqlite3.connect(self.state.database_path)) as db:
            row = db.execute(
                "SELECT cursor FROM synchronization_checkpoint WHERE account_id = ?",
                (self.account.id,),
            ).fetchone()
        return row[0] if row else None

    def gmail_baseline(self, ids=("old",)):
        return self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": item} for item in ids]}),
            ]
        )[0]

    def run_imap(self, connection, *, failed=0):
        service = ArchiveService(
            self.credentials,
            ArchiveState(self.state.database_path),
            self.events.append,
            mailbox=FakeImapMailbox(connection),
        )
        result = service.run_once(self.settings)[0]
        self.assertEqual(result.failed, failed, [event.message for event in self.events])
        self.assertTrue(connection.logged_out)
        return result

    def test_gmail_later_runs_only_query_history_and_show_zero_skipped_after_restart(self):
        self.configure(MailProvider.GMAIL_API)
        baseline = self.gmail_baseline(ids=[str(i) for i in range(1000)])
        self.assertEqual((baseline.checked, baseline.skipped_existing), (1000, 1000))
        self.assertEqual(self.cursor(), "100")

        later, http = self.run_http([gmail_history("100")])
        self.assertEqual((later.checked, later.skipped, later.archived), (0, 0, 0))
        self.assertEqual(len(http.calls), 1)
        self.assertIn("0 checked, 0 archived, 0 skipped", self.progress[-1].message)
        self.assertEqual(self.cursor(), "101")

    def test_gmail_new_mail_and_older_mail_moved_into_label_are_archived_once(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        history = [
            {
                "messagesAdded": [{"message": {"id": "new", "labelIds": ["INBOX"]}}],
                "labelsAdded": [
                    {"message": {"id": "moved"}, "labelIds": ["INBOX"]},
                    {"message": {"id": "new"}, "labelIds": ["INBOX"]},
                    {"message": {"id": "unrelated"}, "labelIds": ["STARRED"]},
                ],
            }
        ]
        result, _ = self.run_http(
            [
                gmail_history("100", history=history),
                gmail_metadata("new"),
                gmail_raw("new"),
                gmail_metadata("moved"),
                gmail_raw("moved"),
            ]
        )
        self.assertEqual((result.archived, result.checked, result.skipped), (2, 2, 0))
        self.assertEqual(self.cursor(), "101")

    def test_gmail_history_pagination_and_duplicate_events(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline(ids=())
        change = [{"messagesAdded": [{"message": {"id": "new"}}]}]
        result, http = self.run_http(
            [
                gmail_history("100", history=change, next_page="page2"),
                gmail_metadata("new"),
                gmail_raw("new"),
                ("json", "pageToken=page2", {"history": change, "historyId": "102"}),
            ]
        )
        self.assertEqual(result.archived, 1)
        self.assertEqual(len(http.calls), 4)
        self.assertEqual(self.cursor(), "102")

    def test_gmail_rule_changes_and_existing_mail_opt_in_recheck_only_local_candidates(self):
        self.configure(MailProvider.GMAIL_API, rules=[])
        self.gmail_baseline()
        result, _ = self.run_http(
            [
                gmail_history("100", history=[{"messagesAdded": [{"message": {"id": "new"}}]}]),
                gmail_metadata("new"),
                gmail_raw("new"),
            ]
        )
        self.assertEqual(result.unmatched, 1)
        self.settings.rules = [Rule("All", "")]
        result, _ = self.run_http(
            [gmail_history("101", next_cursor="102"), gmail_metadata("new"), gmail_raw("new")]
        )
        self.assertEqual(result.archived, 1)
        self.account.mailboxes[0].archive_existing_messages = True
        result, _ = self.run_http(
            [gmail_history("102", next_cursor="103"), gmail_metadata("old"), gmail_raw("old")]
        )
        self.assertEqual(result.archived, 1)
        result, _ = self.run_http([gmail_history("103", next_cursor="104")])
        self.assertEqual((result.checked, result.skipped), (0, 0))

    def test_gmail_unmatched_backfill_is_not_rechecked_without_rule_changes(self):
        self.configure(MailProvider.GMAIL_API, rules=[])
        self.gmail_baseline()
        self.account.mailboxes[0].archive_existing_messages = True
        result, _ = self.run_http([gmail_history("100"), gmail_metadata("old"), gmail_raw("old")])
        self.assertEqual(result.unmatched, 1)
        result, _ = self.run_http([gmail_history("101", next_cursor="102")])
        self.assertEqual((result.unmatched, result.checked), (0, 0))

    def test_gmail_archiving_failure_replays_history_and_preserves_completed_messages(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline(ids=())
        changes = [{"messagesAdded": [{"message": {"id": "new"}}]}]
        steps = [gmail_history("100", history=changes), gmail_metadata("new"), gmail_raw("new")]
        with patch("mailarchive.service.ArchiveStorage.archive", side_effect=OSError("disk full")):
            result, _ = self.run_http(steps, failed=1)
        self.assertEqual(result.archived, 0)
        self.assertEqual(self.cursor(), "100")
        result, _ = self.run_http(steps)
        self.assertEqual(result.archived, 1)
        self.assertEqual(self.cursor(), "101")

    def test_gmail_interrupted_history_does_not_advance_even_after_archiving_a_message(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline(ids=())
        change = [{"messagesAdded": [{"message": {"id": "new"}}]}]
        result, _ = self.run_http(
            [
                gmail_history("100", history=change, next_page="p2"),
                gmail_metadata("new"),
                gmail_raw("new"),
                ("json", "pageToken=p2", ProviderHttpError(503, "unavailable")),
            ],
            failed=1,
        )
        self.assertEqual(result.archived, 1)
        self.assertEqual(self.cursor(), "100")
        result, _ = self.run_http([gmail_history("100", history=change), gmail_metadata("new")])
        self.assertEqual((result.archived, result.already_processed), (0, 1))

    def test_gmail_expired_history_reconciles_once_using_existing_baseline(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        result, _ = self.run_http(
            [
                ("json", "/history?startHistoryId=100", ProviderHttpError(404, "expired")),
                ("json", "/profile?fields=historyId", {"historyId": "200"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "old"}, {"id": "new"}]}),
                gmail_metadata("old"),
                gmail_metadata("new"),
                gmail_raw("new"),
            ]
        )
        self.assertEqual(
            (result.archived, result.skipped_existing, result.already_processed), (1, 0, 1)
        )
        self.assertEqual(self.cursor(), "200")
        self.assertTrue(any("token expired" in event.message for event in self.events))

    def test_gmail_full_scan_keeps_pre_scan_cursor_to_catch_mail_arriving_during_scan(self):
        self.configure(MailProvider.GMAIL_API, existing=True)
        result, _ = self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                (
                    "json",
                    "/messages?labelIds=INBOX",
                    {"messages": [{"id": "old"}], "nextPageToken": "p2"},
                ),
                gmail_raw("old"),
                ("json", "pageToken=p2", {"messages": []}),
            ]
        )
        self.assertEqual(result.archived, 1)
        self.assertEqual(self.cursor(), "100")
        result, _ = self.run_http(
            [
                gmail_history("100", history=[{"messagesAdded": [{"message": {"id": "arrived"}}]}]),
                gmail_metadata("arrived"),
                gmail_raw("arrived"),
            ]
        )
        self.assertEqual(result.archived, 1)

    def test_gmail_absent_backfill_is_suppressed_without_forgetting_baseline(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        self.account.mailboxes[0].archive_existing_messages = True
        self.run_http([gmail_history("100"), gmail_metadata("old", ["OTHER"])])
        self.run_http([gmail_history("101", next_cursor="102")])
        self.assertIn(
            "old",
            self.state.processed_message_ids(
                self.account.id, "gmail_api-mailbox:me@example.org", include_skipped=True
            ),
        )
        self.account.mailboxes[0].archive_existing_messages = False
        result, _ = self.run_http(
            [
                gmail_history(
                    "102",
                    next_cursor="103",
                    history=[{"labelsAdded": [{"message": {"id": "old"}, "labelIds": ["INBOX"]}]}],
                ),
                gmail_metadata("old"),
            ]
        )
        self.assertEqual((result.archived, result.already_processed), (0, 1))
        self.account.mailboxes[0].archive_existing_messages = True
        result, _ = self.run_http(
            [gmail_history("103", next_cursor="104"), gmail_metadata("old"), gmail_raw("old")]
        )
        self.assertEqual(result.archived, 1)

    def test_gmail_missing_mime_and_failed_initial_listing_do_not_initialize(self):
        self.configure(MailProvider.GMAIL_API, existing=True)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "new"}]}),
                ("json", "/messages/new?format=raw", {"labelIds": ["INBOX"]}),
            ],
            failed=1,
        )
        self.assertIsNone(self.cursor())
        self.assertFalse(
            self.state.has_completed_initial_scan(
                self.account.id, "gmail_api-mailbox:me@example.org"
            )
        )

    def test_graph_later_runs_resume_saved_delta_link_without_listing_old_mail(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        result, _ = self.run_http([graph_delta("/messages/delta?", ["old"], next_cursor="d1")])
        self.assertEqual(result.skipped_existing, 1)
        result, http = self.run_http([graph_delta("/v1.0/d1", next_cursor="d2")])
        self.assertEqual((result.checked, result.skipped), (0, 0))
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(self.cursor().endswith("/d2"))

    def test_graph_delta_pagination_empty_pages_removals_and_duplicate_ids(self):
        self.configure(MailProvider.MICROSOFT_GRAPH, existing=True)
        result, http = self.run_http(
            [
                graph_delta("/messages/delta?", next_page="p2"),
                (
                    "json",
                    "/v1.0/p2",
                    {
                        "value": [
                            {"id": "new", "@removed": {"reason": "changed"}},
                            {"id": "new"},
                            {"id": "new"},
                            {"id": "gone", "@removed": {}},
                        ],
                        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d1",
                    },
                ),
                graph_folder(),
                graph_message("new"),
                graph_raw("new"),
            ]
        )
        self.assertEqual((result.archived, result.checked), (1, 1))
        self.assertTrue(all(call[2]["Prefer"] == 'IdType="ImmutableId"' for call in http.calls))

    def test_graph_rules_and_backfill_recheck_only_candidates_still_in_selected_folder(self):
        self.configure(MailProvider.MICROSOFT_GRAPH, rules=[])
        self.run_http([graph_delta("/messages/delta?", ["old"], next_cursor="d1")])
        self.account.mailboxes[0].archive_existing_messages = True
        result, _ = self.run_http(
            [
                graph_delta("/v1.0/d1", next_cursor="d2"),
                graph_folder(),
                graph_message("old"),
                graph_raw("old"),
            ]
        )
        self.assertEqual(result.unmatched, 1)
        self.settings.rules = [Rule("All", "")]
        result, _ = self.run_http(
            [
                graph_delta("/v1.0/d2", next_cursor="d3"),
                graph_folder(),
                graph_message("old", "other-folder"),
            ]
        )
        self.assertEqual(result.archived, 0)
        result, _ = self.run_http([graph_delta("/v1.0/d3", next_cursor="d4")])
        self.assertEqual(result.checked, 0)
        result, _ = self.run_http(
            [
                graph_delta("/v1.0/d4", ["old"], next_cursor="d5"),
                graph_folder(),
                graph_message("old"),
                graph_raw("old"),
            ]
        )
        self.assertEqual(result.archived, 1)

    def test_graph_expired_delta_link_resets_without_resetting_processing_history(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", ["old"], next_cursor="d1")])
        result, _ = self.run_http(
            [
                ("json", "/v1.0/d1", ProviderHttpError(410, "expired")),
                graph_delta("/messages/delta?", ["old", "new"], next_cursor="d2"),
                graph_folder(),
                graph_message("new"),
                graph_raw("new"),
            ]
        )
        self.assertEqual((result.archived, result.already_processed), (1, 1))
        self.assertTrue(self.cursor().endswith("/d2"))

    def test_graph_failed_mime_download_reuses_previous_cursor(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="d1")])
        steps = [
            graph_delta("/v1.0/d1", ["new"], next_cursor="d2"),
            graph_folder(),
            graph_message("new"),
        ]
        self.run_http(
            [*steps, ("bytes", "/messages/new/$value", ProviderHttpError(429, "rate limit"))],
            failed=1,
        )
        self.assertTrue(self.cursor().endswith("/d1"))
        result, _ = self.run_http([*steps, graph_raw("new")])
        self.assertEqual(result.archived, 1)

    def test_graph_missing_final_delta_link_does_not_advance_cursor(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="d1")])
        self.run_http([("json", "/v1.0/d1", {"value": []})], failed=1)
        self.assertTrue(self.cursor().endswith("/d1"))

    def test_gmail_expiration_after_a_page_does_not_download_successful_mail_twice(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline(ids=())
        result, _ = self.run_http(
            [
                gmail_history(
                    "100", history=[{"messagesAdded": [{"message": {"id": "new"}}]}], next_page="p2"
                ),
                gmail_metadata("new"),
                gmail_raw("new"),
                ("json", "pageToken=p2", ProviderHttpError(404, "expired")),
                ("json", "/profile?fields=historyId", {"historyId": "200"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "new"}]}),
                gmail_metadata("new"),
            ]
        )
        self.assertEqual((result.archived, result.already_processed), (1, 1))
        self.assertEqual(self.cursor(), "200")

    def test_graph_expiration_after_a_page_reconsiders_mail_previously_outside_folder(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="d1")])
        result, _ = self.run_http(
            [
                graph_delta("/v1.0/d1", ["new"], next_page="p2"),
                graph_folder(),
                graph_message("new", "other-folder"),
                ("json", "/v1.0/p2", ProviderHttpError(410, "expired")),
                graph_delta("/messages/delta?", ["new"], next_cursor="d2"),
                graph_message("new"),
                graph_raw("new"),
            ]
        )
        self.assertEqual(result.archived, 1)
        self.assertTrue(self.cursor().endswith("/d2"))

    def test_imap_uses_uid_progress_and_filters_reversed_star_range_after_restart(self):
        self.configure(MailProvider.GENERIC_IMAP)
        baseline = self.run_imap(FakeImapConnection(uids=b"1 2 3"))
        self.assertEqual(baseline.skipped_existing, 3)
        connection = FakeImapConnection(uids=b"3")
        result = self.run_imap(connection)
        self.assertEqual((result.checked, result.skipped), (0, 0))
        self.assertIn(("uid", "search", None, "UID 4:*"), connection.calls)
        connection = FakeImapConnection(uids=b"4")
        result = self.run_imap(connection)
        self.assertEqual(result.archived, 1)
        self.assertEqual(self.cursor(), "4")

    def test_imap_backfill_and_rule_change_search_only_requested_uids(self):
        self.configure(MailProvider.GENERIC_IMAP, rules=[])
        self.run_imap(FakeImapConnection(uids=b"1"))
        self.account.mailboxes[0].archive_existing_messages = True
        connection = FakeImapConnection(uids=b"1")
        self.assertEqual(self.run_imap(connection).unmatched, 1)
        self.assertIn(("uid", "search", None, "UID 1"), connection.calls)
        self.settings.rules = [Rule("All", "")]
        connection = FakeImapConnection(uids=b"1")
        self.assertEqual(self.run_imap(connection).archived, 1)
        self.assertIn(("uid", "search", None, "UID 1"), connection.calls)
        self.assertEqual(self.run_imap(FakeImapConnection(uids=b"1")).checked, 0)

    def test_imap_failed_archiving_and_uidvalidity_changes_preserve_safety(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"1"))
        with patch("mailarchive.service.ArchiveStorage.archive", side_effect=OSError("disk full")):
            self.run_imap(FakeImapConnection(uids=b"2"), failed=1)
        self.assertEqual(self.cursor(), "1")
        self.assertEqual(self.run_imap(FakeImapConnection(uids=b"2")).archived, 1)
        connection = FakeImapConnection(uids=b"1 2", validity_data=[b"9002"])
        self.assertEqual(self.run_imap(connection).skipped_existing, 2)
        self.assertIn(("uid", "search", None, "ALL"), connection.calls)

    def test_imap_empty_folder_saves_zero_cursor_and_missing_validity_stops_safely(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b""))
        self.assertEqual(self.cursor(), "0")
        self.assertEqual(self.run_imap(FakeImapConnection(uids=b"1")).archived, 1)
        connection = FakeImapConnection(uids=b"", validity_data=[])
        self.run_imap(connection, failed=1)
        self.assertEqual(self.cursor(), "1")
        self.assertEqual(sum(call[0] == "select" for call in connection.calls), 2)
        self.assertFalse(any(call[0] == "uid" for call in connection.calls))

    def test_imap_missing_recheck_uids_are_suppressed_and_large_requests_are_batched(self):
        self.configure(MailProvider.GENERIC_IMAP)
        namespace = imap_namespace(self.account, "9001")
        self.state.complete_initial_scan(
            self.account.id, namespace, {str(i) for i in range(1, 1002)}
        )
        self.account.mailboxes[0].archive_existing_messages = True
        connection = FakeImapConnection(uids=b"")
        result = self.run_imap(connection)
        self.assertEqual(result.checked, 0)
        searches = [call for call in connection.calls if call[:2] == ("uid", "search")]
        self.assertEqual(len(searches), 4)
        self.assertTrue(all(len(call[3].split(",")) <= 500 for call in searches))
        connection = FakeImapConnection(uids=b"")
        self.run_imap(connection)
        self.assertEqual(
            len([call for call in connection.calls if call[:2] == ("uid", "search")]), 1
        )

    def test_changed_remote_account_identity_cannot_reuse_a_saved_api_cursor(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        self.account.username = "other@example.org"
        self.gmail_baseline(ids=())

    def test_rule_destination_and_unrelated_rule_changes_do_not_request_rechecks(self):
        rule = Rule("Missing", "", [Condition(MailField.SUBJECT, value="missing")])
        self.configure(MailProvider.GMAIL_API, existing=True, rules=[rule])
        result, _ = self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "new"}]}),
                gmail_raw("new"),
            ]
        )
        self.assertEqual(result.unmatched, 1)
        rule.destination = "changed"
        rule.name = "Renamed"
        self.settings.rules.append(Rule("Unrelated", "", account_ids=["other-account"]))
        result, _ = self.run_http([gmail_history("100")])
        self.assertEqual(result.checked, 0)


class SyncIteratorTests(unittest.TestCase):
    def test_closing_imap_iterator_does_not_publish_cursor(self):
        connection = FakeImapConnection(uids=b"1 2")
        sync = SyncSession(lambda namespace: None, lambda namespace: set())
        _, messages = FakeImapMailbox(connection).fetch_messages(
            mail_target(
                Account(
                    "Mailbox",
                    "imap.example.org",
                    "me@example.org",
                    mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
                )
            ),
            "secret",
            sync=sync,
        )
        next(messages)
        messages.close()
        self.assertIsNone(sync.next_cursor)
        self.assertTrue(connection.logged_out)

    def test_graph_rejects_untrusted_saved_cursor_before_sending_token(self):
        http = ScriptedHttp([])
        sync = SyncSession(
            lambda namespace: "https://attacker.example/delta", lambda namespace: set()
        )
        _, messages = MicrosoftGraphMessageSource(FakeOAuth(), http).fetch_messages(
            mail_target(
                Account(
                    "Mailbox",
                    username="me@example.org",
                    mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
                )
            ),
            lambda namespace, message_id: True,
            sync=sync,
        )
        with self.assertRaisesRegex(Exception, "invalid synchronization link"):
            list(messages)
        self.assertEqual(http.calls, [])

    def test_graph_rejects_untrusted_continuation_and_final_links(self):
        for key in ("@odata.nextLink", "@odata.deltaLink"):
            with self.subTest(key=key):
                http = ScriptedHttp(
                    [
                        (
                            "json",
                            "/messages/delta?",
                            {"value": [], key: "https://attacker.example/delta"},
                        )
                    ]
                )
                sync = SyncSession(lambda namespace: None, lambda namespace: set())
                _, messages = MicrosoftGraphMessageSource(FakeOAuth(), http).fetch_messages(
                    mail_target(Account("Mailbox", mailboxes=[Mailbox("", folders=["INBOX"])])),
                    lambda namespace, message_id: True,
                    sync=sync,
                )
                with self.assertRaisesRegex(Exception, "invalid synchronization link"):
                    list(messages)
                self.assertIsNone(sync.next_cursor)
                self.assertEqual(len(http.calls), 1)

    def test_imap_cursor_lookup_failure_logs_out_before_iterator_is_created(self):
        connection = FakeImapConnection()

        def fail(namespace):
            raise sqlite3.OperationalError("database locked")

        sync = SyncSession(fail, lambda namespace: set())
        with self.assertRaisesRegex(sqlite3.OperationalError, "database locked"):
            FakeImapMailbox(connection).fetch_messages(
                mail_target(Account("Mailbox", mailboxes=[Mailbox("", folders=["INBOX"])])),
                "secret",
                sync=sync,
            )
        self.assertTrue(connection.logged_out)

    def test_imap_maximum_uid_does_not_generate_an_invalid_search_range(self):
        connection = FakeImapConnection(uids=b"4294967295")
        sync = SyncSession(lambda namespace: "4294967295", lambda namespace: set())
        _, messages = FakeImapMailbox(connection).fetch_messages(
            mail_target(Account("Mailbox", mailboxes=[Mailbox("", folders=["INBOX"])])),
            "secret",
            sync=sync,
        )
        self.assertEqual(list(messages), [])
        self.assertIn(("uid", "search", None, "UID 4294967295:*"), connection.calls)
        self.assertEqual(sync.next_cursor, "4294967295")


class SynchronizationStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = ArchiveState(self.root / "state.sqlite3")

    def test_scan_commit_is_atomic_when_checkpoint_write_fails(self):
        with closing(sqlite3.connect(self.state.database_path)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_checkpoint BEFORE INSERT ON synchronization_checkpoint "
                "BEGIN SELECT RAISE(ABORT, 'checkpoint failed'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "checkpoint failed"):
            self.state.complete_scan(
                "account",
                "namespace",
                cursor="100",
                identity="identity",
                skipped_message_ids={"old"},
                discarded_ids={"missing"},
            )
        self.assertFalse(self.state.has_completed_initial_scan("account", "namespace"))
        self.assertEqual(
            self.state.processed_message_ids("account", "namespace", include_skipped=True), set()
        )
        with closing(sqlite3.connect(self.state.database_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM unavailable_message").fetchone()[0], 0
            )

    def test_cursor_is_bound_to_account_namespace_and_remote_identity(self):
        self.state.complete_scan("account", "namespace", cursor="100", identity="identity")
        self.assertEqual(self.state.sync_cursor("account", "namespace", "identity"), "100")
        self.assertIsNone(self.state.sync_cursor("other-account", "namespace", "identity"))
        self.assertIsNone(self.state.sync_cursor("account", "other-namespace", "identity"))
        self.assertIsNone(self.state.sync_cursor("account", "namespace", "other-identity"))

    def test_copy_preserves_cursor_and_merge_invalidates_both_histories_cursors(self):
        self.state.complete_scan(
            "account", "namespace", cursor="100", identity="identity", skipped_message_ids={"old"}
        )
        copied = self.state.migrated_to(self.root / "copied.sqlite3")
        self.assertEqual(copied.sync_cursor("account", "namespace", "identity"), "100")
        target = ArchiveState(self.root / "target.sqlite3")
        target.complete_scan(
            "account",
            "namespace",
            cursor="200",
            identity="identity",
            skipped_message_ids={"older"},
            discarded_ids={"old"},
        )
        target.complete_scan("other-account", "other-namespace", cursor="300", identity="identity")
        merged = self.state.migrated_to(target.database_path)
        self.assertIsNone(merged.sync_cursor("account", "namespace", "identity"))
        self.assertIsNone(merged.sync_cursor("other-account", "other-namespace", "identity"))
        self.assertEqual(
            merged.processed_message_ids("account", "namespace", include_skipped=True),
            {"old", "older"},
        )
        self.assertEqual(
            merged.recheck_message_ids("account", "namespace", "rules", include_existing=True),
            {"old", "older"},
        )
