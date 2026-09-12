"""Provider protocol contracts exercised through the service and persistent SQLite state."""

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mailarchive.mail_identity import imap_scope, mailbox_namespace
from mailarchive.mail_sources import MessageSourceRegistry, MicrosoftGraphMessageSource
from mailarchive.migrations import DATABASE_SCHEMA_VERSION, MIGRATIONS
from mailarchive.models import AuthMode, Mailbox, MailProvider
from mailarchive.service import ArchiveService
from mailarchive.storage import ArchiveState
from tests import test_synchronization as fixtures
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_mail_sources import FakeOAuth


class ProviderContractTests(unittest.TestCase):
    setUp = fixtures.SynchronizationTests.setUp
    configure = fixtures.SynchronizationTests.configure
    run_http = fixtures.SynchronizationTests.run_http
    run_imap = fixtures.SynchronizationTests.run_imap
    cursor = fixtures.SynchronizationTests.cursor
    gmail_baseline = fixtures.SynchronizationTests.gmail_baseline

    def check(self, mailbox=None):
        return self.state.mailbox_check(
            self.account.id, mailbox_namespace(self.account, mailbox or self.account.mailboxes[0])
        )

    def assert_failed_preserves_success(self, before, cursor):
        after = self.check()
        self.assertEqual(after["status"], "failed")
        self.assertIsNotNone(after["error"])
        self.assertEqual(after["last_successful_at"], before["last_successful_at"])
        self.assertGreater(after["started_at"], before["started_at"])
        self.assertGreaterEqual(after["finished_at"], after["started_at"])
        self.assertEqual(self.cursor(), cursor)

    def test_missing_uidvalidity_retries_once_without_search_or_history_mutation(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        before = self.check()
        for data in (None, [], [None]):
            with self.subTest(data=data):
                connection = FakeImapConnection(validity_responses=[data, data])
                self.run_imap(connection, failed=1)
                self.assertEqual(sum(call[0] == "select" for call in connection.calls), 2)
                self.assertFalse(any(call[0] == "uid" for call in connection.calls))
                self.assert_failed_preserves_success(before, "7")
        with closing(sqlite3.connect(self.state.database_path)) as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM source_checkpoint WHERE source_namespace LIKE '%unknown%'"
                ).fetchone()
            )

    def test_uidvalidity_recovered_on_reopening_uses_original_cursor(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        connection = FakeImapConnection(uids=b"8", validity_responses=[[], [b"9001"]])
        result = self.run_imap(connection)
        self.assertEqual((result.archived, result.skipped), (1, 0))
        self.assertIn(("uid", "search", None, "UID 8:*"), connection.calls)
        self.assertEqual(sum(call[0] == "select" for call in connection.calls), 2)
        self.assertEqual(self.cursor(), "8")
        self.assertEqual(self.check()["status"], "success")

    def test_invalid_uidvalidity_fails_before_message_lookup(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        before = self.check()
        values = (b"0", b"-1", b"+1", b"01", b"4294967296", b"unknown", b"\xff", b"", "9001", 9001)
        for value in values:
            with self.subTest(value=value):
                connection = FakeImapConnection(validity_data=[value])
                self.run_imap(connection, failed=1)
                self.assertEqual(sum(call[0] == "select" for call in connection.calls), 1)
                self.assertFalse(any(call[0] == "uid" for call in connection.calls))
                self.assert_failed_preserves_success(before, "7")
        for data in (b"9001", 9001, {"UIDVALIDITY": "9001"}, [b"9001", b"9002"]):
            with self.subTest(response=data):
                connection = FakeImapConnection(validity_data=data)
                self.run_imap(connection, failed=1)
                self.assertFalse(any(call[0] == "uid" for call in connection.calls))
                self.assert_failed_preserves_success(before, "7")

    def test_uidvalidity_change_during_search_or_fetch_does_not_commit_old_epoch(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        before = self.check()
        for responses in ([[b"9001"], [b"9002"]], [[b"9001"], [None], [b"9002"]]):
            with self.subTest(responses=responses):
                result = self.run_imap(
                    FakeImapConnection(uids=b"8", validity_responses=responses), failed=1
                )
                self.assertEqual(result.archived, 0)
                self.assert_failed_preserves_success(before, "7")
        self.assertFalse(list((self.root / "Archive").rglob("*.eml")))

    def test_invalid_search_uids_do_not_advance_checkpoint(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        before = self.check()
        for uids in (b"8 0", b"8 -1", b"8 01", b"4294967296", b"\xff", None):
            with self.subTest(uids=uids):
                result = self.run_imap(FakeImapConnection(uids=uids), failed=1)
                self.assertEqual(result.archived, 0)
                self.assert_failed_preserves_success(before, "7")

    def test_unsigned_imap_maximum_is_valid_and_does_not_wrap(self):
        self.configure(MailProvider.GENERIC_IMAP)
        connection = FakeImapConnection(uids=b"4294967295", validity_data=[b"4294967295"])
        self.run_imap(connection)
        self.assertEqual(self.cursor(), "4294967295")
        connection = FakeImapConnection(uids=b"4294967295", validity_data=[b"4294967295"])
        self.assertEqual(self.run_imap(connection).checked, 0)
        self.assertIn(("uid", "search", None, "UID 4294967295:*"), connection.calls)

    def test_imap_oauth_addresses_each_mailbox_with_shared_connection_credentials(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.account.auth_mode = AuthMode.OAUTH_USER
        self.account.host = "outlook.office365.com"
        self.account.client_id = "client"
        self.account.mailboxes.append(Mailbox("team@example.org", ["INBOX"]))
        self.account.validate()
        oauth = FakeOAuth()
        for uid in (b"7", b"8"):
            connection = FakeImapConnection(uids=uid)
            registry = MessageSourceRegistry(
                self.credentials, imap_mailbox=FakeImapMailbox(connection)
            )
            registry.sources[self.account.provider].oauth = oauth
            result = ArchiveService(
                self.credentials, self.state, source_registry=registry
            ).run_once(self.settings)[0]
            self.assertEqual(result.failed, 0)
            payloads = [call[2][0] for call in connection.calls if call[0] == "authenticate"]
            self.assertEqual(
                payloads,
                [
                    b"user=me@example.org\x01auth=Bearer microsoft-token\x01\x01",
                    b"user=team@example.org\x01auth=Bearer microsoft-token\x01\x01",
                ],
            )
            self.assertFalse(any(call[0] == "login" for call in connection.calls))
            if uid == b"8":
                self.assertEqual(result.archived, 2)
                self.assertEqual(
                    sum(call == ("uid", "search", None, "UID 8:*") for call in connection.calls), 2
                )
            for mailbox in self.account.mailboxes:
                self.assertEqual(self.check(mailbox)["status"], "success")
            with closing(sqlite3.connect(self.state.database_path)) as db:
                cursors = db.execute(
                    "SELECT cursor FROM synchronization_checkpoint WHERE account_id = ?",
                    (self.account.id,),
                ).fetchall()
            self.assertEqual(cursors, [(uid.decode(),), (uid.decode(),)])
        self.assertEqual({account.id for account in oauth.microsoft_accounts}, {self.account.id})

    def test_malformed_initial_api_pages_cannot_complete_a_baseline(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            for value in (None, {}, [42], [{"id": None}]):
                with self.subTest(provider=provider, value=value):
                    self.configure(provider)
                    if provider == MailProvider.GMAIL_API:
                        steps = [
                            ("json", "/profile?fields=historyId", {"historyId": "100"}),
                            ("json", "/messages?", {"messages": value}),
                        ]
                    else:
                        steps = [
                            (
                                "json",
                                "/messages/delta?",
                                {
                                    "value": value,
                                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/new",
                                },
                            )
                        ]
                    self.run_http(steps, failed=1)
                    self.assertIsNone(self.cursor())
                    self.assertEqual(self.check()["status"], "failed")
                    self.assertIsNone(self.check()["last_successful_at"])
                    self.assertFalse(
                        self.state.has_completed_initial_scan(
                            self.account.id,
                            mailbox_namespace(self.account, self.account.mailboxes[0]),
                        )
                    )

    def test_invalid_gmail_profile_history_id_stops_before_full_listing(self):
        for value in (None, 100, "", "0", "１００"):
            with self.subTest(value=value):
                self.configure(MailProvider.GMAIL_API)
                self.run_http(
                    [("json", "/profile?fields=historyId", {"historyId": value})], failed=1
                )
                self.assertEqual(self.check()["status"], "failed")
                self.assertIsNone(self.cursor())

    def test_invalid_gmail_history_structure_tokens_and_membership_cannot_advance(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        before = self.check()
        pages = [
            {"history": None},
            {"history": [42]},
            {"history": [{"messagesAdded": [{"message": None}]}]},
            {"history": [{"labelsAdded": [{"message": {"id": "new"}, "labelIds": "INBOX"}]}]},
            {"nextPageToken": 42},
        ]
        for page in pages:
            with self.subTest(page=page):
                self.run_http(
                    [("json", "/history?startHistoryId=100", {"historyId": "101", **page})],
                    failed=1,
                )
                self.assert_failed_preserves_success(before, "100")
        self.run_http(
            [
                fixtures.gmail_history(
                    "100", history=[{"messagesAdded": [{"message": {"id": "new"}}]}]
                ),
                ("json", "/messages/new?format=minimal", {"labelIds": "INBOX"}),
            ],
            failed=1,
        )
        self.assert_failed_preserves_success(before, "100")

    def test_invalid_gmail_history_ids_preserve_previous_cursor_and_success_time(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        before = self.check()
        for value in (None, 101, "", "0", "-1", "１０１", "99"):
            with self.subTest(value=value):
                self.run_http(
                    [("json", "/history?startHistoryId=100", {"historyId": value})], failed=1
                )
                self.assert_failed_preserves_success(before, "100")

    def test_invalid_provider_message_ids_cannot_be_silently_skipped(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            for value in (None, "", 42):
                with self.subTest(provider=provider, value=value):
                    self.configure(provider)
                    if provider == MailProvider.GMAIL_API:
                        self.gmail_baseline()
                        steps = [
                            fixtures.gmail_history(
                                "100", history=[{"messagesAdded": [{"message": {"id": value}}]}]
                            )
                        ]
                        cursor = "100"
                    else:
                        self.run_http([fixtures.graph_delta("/messages/delta?", next_cursor="old")])
                        steps = [fixtures.graph_delta("/old", ids=[value])]
                        cursor = "https://graph.microsoft.com/v1.0/old"
                    before = self.check()
                    self.run_http(steps, failed=1)
                    self.assert_failed_preserves_success(before, cursor)

    def test_repeating_provider_pages_fail_without_saving_candidate_cursor(self):
        self.configure(MailProvider.GMAIL_API)
        self.gmail_baseline()
        before = self.check()
        self.run_http(
            [
                fixtures.gmail_history("100", next_page="repeat"),
                fixtures.gmail_history("100", next_page="repeat"),
            ],
            failed=1,
        )
        self.assert_failed_preserves_success(before, "100")
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([fixtures.graph_delta("/messages/delta?", next_cursor="old")])
        before = self.check()
        self.run_http(
            [
                fixtures.graph_delta("/old", next_page="repeat"),
                fixtures.graph_delta("/repeat", next_page="repeat"),
            ],
            failed=1,
        )
        self.assert_failed_preserves_success(before, "https://graph.microsoft.com/v1.0/old")

    def test_graph_requires_exactly_one_valid_continuation_or_delta_link(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([fixtures.graph_delta("/messages/delta?", next_cursor="old")])
        before = self.check()
        for links in (
            {},
            {"@odata.deltaLink": None},
            {"@odata.nextLink": 42},
            {"@odata.deltaLink": "https://other.example/v1.0/stolen"},
            {
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/page",
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/new",
            },
        ):
            with self.subTest(links=links):
                self.run_http([("json", "/old", {"value": [], **links})], failed=1)
                self.assert_failed_preserves_success(before, "https://graph.microsoft.com/v1.0/old")

    def test_native_parameters_and_stored_cursors_match_for_each_api_auth_mode(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            for mode in (AuthMode.OAUTH_USER, AuthMode.OAUTH_APPLICATION):
                with self.subTest(provider=provider, mode=mode):
                    self.configure(provider)
                    self.account.auth_mode = mode
                    self.account.client_id = "client"
                    self.account.tenant_id = "tenant"
                    mailbox_root = (
                        "/users/me%40example.org" if mode == AuthMode.OAUTH_APPLICATION else "/me"
                    )
                    opaque = "https://graph.microsoft.com/v1.0/me/mailFolders/INBOX/messages/delta?$deltatoken=a%2Bb&$select=id"
                    initial = (
                        [
                            ("json", "/profile?fields=historyId", {"historyId": "100"}),
                            ("json", "/messages?", {}),
                        ]
                        if provider == MailProvider.GMAIL_API
                        else [
                            (
                                "json",
                                mailbox_root + "/mailFolders/INBOX/messages/delta?",
                                {
                                    "value": [],
                                    "@odata.deltaLink": opaque.replace("/me/", mailbox_root + "/"),
                                },
                            ),
                        ]
                    )
                    _, http = self.run_http(initial)
                    first = urlsplit(http.calls[-1][1])
                    query = parse_qs(first.query)
                    if provider == MailProvider.GMAIL_API:
                        self.assertEqual(first.path, "/gmail/v1/users/me%40example.org/messages")
                        self.assertEqual(
                            query,
                            {
                                "labelIds": ["INBOX"],
                                "maxResults": ["500"],
                                "includeSpamTrash": ["true"],
                            },
                        )
                        self.assertEqual(self.cursor(), "100")
                        _, http = self.run_http([fixtures.gmail_history("100", next_cursor="105")])
                        self.assertEqual(
                            parse_qs(urlsplit(http.calls[0][1]).query),
                            {"startHistoryId": ["100"], "maxResults": ["500"]},
                        )
                        self.assertEqual(self.cursor(), "105")
                    else:
                        self.assertEqual(query, {"$select": ["id"], "$top": ["999"]})
                        saved = opaque.replace("/me/", mailbox_root + "/")
                        self.assertEqual(self.cursor(), saved)
                        _, http = self.run_http([fixtures.graph_delta(saved, next_cursor="new")])
                        self.assertEqual(http.calls[0][1], saved)
                        self.assertEqual(
                            http.calls[0][2], MicrosoftGraphMessageSource.GRAPH_HEADERS
                        )
                    self.assertEqual(self.check()["status"], "success")
                    self.assertEqual(
                        self.check()["last_successful_at"], self.check()["finished_at"]
                    )
                    self.assertIsNotNone(datetime.fromisoformat(self.check()["finished_at"]).tzinfo)
                    with closing(sqlite3.connect(self.state.database_path)) as db:
                        binding = db.execute(
                            "SELECT identity FROM synchronization_checkpoint WHERE account_id = ?",
                            (self.account.id,),
                        ).fetchone()[0]
                    self.assertIn(mode.value, binding)
                    self.assertIn("me@example.org", binding)

    def test_discovery_failure_records_mailbox_check_without_cursor(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.account.mailboxes[0].folders = []
        self.run_http(
            [("json", "/mailFolders?", {"value": [{"id": "folder", "childFolderCount": True}]})],
            failed=1,
        )
        check = self.check()
        self.assertEqual(check["status"], "failed")
        self.assertIsNone(check["last_successful_at"])
        self.assertIsNotNone(check["finished_at"])
        self.assertIsNone(self.cursor())

    def test_partial_mailbox_failure_keeps_successful_folder_cursor_and_other_mailbox_progress(
        self,
    ):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        owner = self.account.mailboxes[0]
        owner.folders = ["INBOX", "sentitems"]
        other = Mailbox("team@example.org", ["INBOX"])
        disabled = Mailbox("disabled@example.org", ["INBOX"], enabled=False)
        self.account.mailboxes.extend([other, disabled])
        self.run_http(
            [
                fixtures.graph_delta("/me/mailFolders/INBOX/messages/delta?", next_cursor="inbox"),
                ("json", "/me/mailFolders/sentitems/messages/delta?", {"value": []}),
                fixtures.graph_delta(
                    "/users/team%40example.org/mailFolders/INBOX/messages/delta?",
                    next_cursor="team",
                ),
            ],
            failed=1,
        )
        self.assertEqual(self.check(owner)["status"], "failed")
        self.assertIsNone(self.check(owner)["last_successful_at"])
        self.assertEqual(self.check(other)["status"], "success")
        self.assertIsNone(self.check(disabled))
        self.assertFalse(
            self.state.has_completed_initial_scan(
                self.account.id, mailbox_namespace(self.account, owner)
            )
        )
        with closing(sqlite3.connect(self.state.database_path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM synchronization_checkpoint").fetchone()[0], 2
            )

    def test_processing_failure_updates_attempt_but_preserves_successful_check(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        before = self.check()
        with patch("mailarchive.service.ArchiveStorage.archive", side_effect=OSError("disk full")):
            self.run_imap(FakeImapConnection(uids=b"8"), failed=1)
        self.assert_failed_preserves_success(before, "7")
        self.assertIn("disk full", self.check()["error"])
        self.assertEqual(self.run_imap(FakeImapConnection(uids=b"8")).archived, 1)
        self.assertEqual(self.check()["status"], "success")
        self.assertIsNone(self.check()["error"])

    def test_missing_adapter_completion_cursor_cannot_mark_check_successful(self):
        self.configure(MailProvider.GENERIC_IMAP)
        source = unittest.mock.Mock()
        source.targets.return_value = [fixtures.mail_target(self.account)]
        source.fetch_messages.return_value = (
            imap_scope(fixtures.mail_target(self.account), "9001"),
            iter([]),
        )
        registry = MessageSourceRegistry(self.credentials)
        registry.sources[self.account.provider] = source
        result = ArchiveService(self.credentials, self.state, source_registry=registry).run_once(
            self.settings
        )[0]
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.check()["status"], "failed")
        self.assertIsNone(self.check()["last_successful_at"])
        self.assertIsNone(self.cursor())

    def test_checks_survive_restart_copy_and_merge_with_latest_success_preserved(self):
        self.configure(MailProvider.GENERIC_IMAP)
        self.run_imap(FakeImapConnection(uids=b"7"))
        success = self.check()
        namespace = mailbox_namespace(self.account, self.account.mailboxes[0])
        copied = self.state.migrated_to(self.root / "copy.db")
        self.assertEqual(copied.mailbox_check(self.account.id, namespace), success)
        older = self.state.migrated_to(self.root / "older.db")
        self.state.begin_mailbox_check(self.account.id, namespace)
        self.state.finish_mailbox_check(self.account.id, namespace)
        newer_success = self.check()["last_successful_at"]
        copied.begin_mailbox_check(self.account.id, namespace)
        running = ArchiveState(copied.database_path).mailbox_check(self.account.id, namespace)
        self.assertEqual(running["status"], "running")
        self.assertIsNone(running["finished_at"])
        self.assertEqual(running["last_successful_at"], success["last_successful_at"])
        copied.finish_mailbox_check(self.account.id, namespace, error="offline")
        failed = copied.mailbox_check(self.account.id, namespace)
        merged = copied.migrated_to(self.state.database_path)
        self.assertEqual(
            merged.mailbox_check(self.account.id, namespace),
            {**failed, "last_successful_at": newer_success},
        )
        self.assertIsNone(self.cursor())
        # Merging an older attempt cannot replace the newer result.
        older.migrated_to(copied.database_path)
        self.assertEqual(copied.mailbox_check(self.account.id, namespace), failed)

    def test_schema_five_upgrade_preserves_cursor_and_adds_independent_checks(self):
        path = self.root / "v5.db"
        with closing(sqlite3.connect(path)) as db, db:
            for migration in MIGRATIONS[:5]:
                migration(db)
            db.execute("PRAGMA user_version = 5")
            db.execute(
                "INSERT INTO synchronization_checkpoint VALUES (?, ?, ?, ?, ?)",
                ("connection", "mailbox", "binding", "123", datetime.now(timezone.utc).isoformat()),
            )
        state = ArchiveState(path)
        self.assertIsNotNone(state.migration_backup_path)
        self.assertEqual(state.sync_cursor("connection", "mailbox", "binding"), "123")
        self.assertIsNone(state.mailbox_check("connection", "mailbox"))
        state.begin_mailbox_check("connection", "mailbox")
        state.finish_mailbox_check("connection", "mailbox", error="UIDVALIDITY missing")
        self.assertEqual(state.mailbox_check("connection", "mailbox")["status"], "failed")
        self.assertEqual(state.sync_cursor("connection", "mailbox", "binding"), "123")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION
            )
