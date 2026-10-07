"""Real IMAP response shapes and bounded large-mailbox discovery."""

import imaplib
import io
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from mailarchive.application.account_credentials import store_account_credentials
from mailarchive.application.cancellation import Cancellation, ProcessingStopped
from mailarchive.application.source_port import MailboxError, RemoteMessageNamespaceChanged
from mailarchive.application.synchronization import RangePagination, SyncSession
from mailarchive.domain.configuration import Account, AuthMode, Mailbox, Rule, RuleTarget, Settings
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import IMAP_UID_SEARCH_WINDOW, ImapMailbox
from tests.helpers import mail_target
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service


def parsed_response(wire, kind):
    parser = imaplib.IMAP4.__new__(imaplib.IMAP4)
    parser._mode_ascii()
    parser.tagre = re.compile(rb"(?P<tag>T[0-9]+) (?P<type>[A-Z]+) (?P<data>.*)")
    parser.debug = 0
    parser._cmd_log = {}
    parser._cmd_log_len = 10
    parser._cmd_log_idx = 0
    parser.untagged_responses = {}
    parser.file = io.BytesIO(wire)
    parser.read = parser.file.read
    while parser.file.tell() < len(wire):
        parser._get_response()
    return parser.untagged_responses[kind]


class LargeMailbox(FakeImapConnection):
    """Search replies pass through stock imaplib's real line-size enforcement."""

    def __init__(self, count=160_001, first_uid=1):
        super().__init__()
        self.count = count
        self.first_uid = first_uid
        self.windows = []
        self.sequence_windows = []
        self.after_search = lambda: None

    def response(self, name):
        if name == "UIDNEXT":
            return name, [str(self.first_uid + self.count).encode()]
        return super().response(name)

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(self.count).encode()]

    def fetch(self, sequence, attributes):
        self.calls.append(("fetch", sequence, attributes))
        position = self.count if sequence == "*" else 1
        uid = self.first_uid + self.count - 1 if sequence == "*" else self.first_uid
        return "OK", [f"{position} (UID {uid})".encode()]

    def uid(self, command, *arguments):
        if command != "search":
            return super().uid(command, *arguments)
        self.calls.append(("uid", command, *arguments))
        criterion = arguments[-1]
        window = re.match(r"UID ([0-9]+):([0-9]+)(?: |$)", criterion)
        if window:
            lower, upper = map(int, window.groups())
            self.windows.append((lower, upper))
            sequence = re.search(r"(?<!UID )\b([0-9]+):([0-9]+)(?: |$)", criterion[window.end() :])
            sequence_start, sequence_end = (1, self.count)
            if sequence:
                sequence_start, sequence_end = map(int, sequence.groups())
                self.sequence_windows.append((sequence_start, sequence_end))
            ids = range(
                max(lower, self.first_uid + sequence_start - 1),
                min(upper, self.first_uid + min(sequence_end, self.count) - 1) + 1,
            )
        else:
            ids = [int(uid) for uid in criterion.removeprefix("UID ").split(",")]
        wire = b"* SEARCH " + b" ".join(str(uid).encode() for uid in ids) + b"\r\n"
        result = parsed_response(wire, "SEARCH")
        self.after_search()
        return "OK", result


class SparseMailbox(FakeImapConnection):
    """Live sequence positions with sparse immutable UIDs and native SEARCH parsing."""

    def __init__(self, uids=None, *, date_matches=True):
        super().__init__()
        self.message_uids = (
            list(range(1, IMAP_UID_SEARCH_WINDOW + 1)) + [4_000_000_000]
            if uids is None
            else list(uids)
        )
        self.searches = []
        self.date_matches = date_matches
        self.before_search = lambda: None
        self.after_search = lambda: b""
        self.reject_outside_sequence = False

    def select(self, *args, **kwargs):
        return "OK", [str(len(self.message_uids)).encode()]

    def fetch(self, sequence, attributes):
        self.calls.append(("fetch", sequence, attributes))
        if not self.message_uids:
            return "OK", [None]
        assert sequence == "*" and attributes == "(UID)"
        return "OK", [f"{len(self.message_uids)} (UID {self.message_uids[-1]})".encode()]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        assert command == "search", "Baseline and empty range must not download MIME or headers"
        criterion = args[-1]
        self.searches.append(criterion)
        self.before_search()
        uid_range = re.match(r"UID (\d+):(\d+)(?: |$)", criterion)
        if uid_range is None:
            wanted = {int(value) for value in criterion.removeprefix("UID ").split(",")}
            candidates = [uid for uid in self.message_uids if uid in wanted]
        else:
            low, high = map(int, uid_range.groups())
            sequence = re.match(r"(\d+):(\d+)(?: |$)", criterion[uid_range.end() :])
            candidates = self.message_uids
            if sequence:
                start, end = map(int, sequence.groups())
                if self.reject_outside_sequence and end > len(candidates):
                    return "BAD", [b"Invalid message sequence endpoint"]
                candidates = candidates[start - 1 : end]
            candidates = [uid for uid in candidates if low <= uid <= high]
            if not self.date_matches and "SINCE" in criterion:
                candidates = []
        # Arguments refer to pre-EXPUNGE positions even when updates precede SEARCH.
        updates = self.after_search()
        wire = updates + b"* SEARCH " + b" ".join(str(uid).encode() for uid in candidates) + b"\r\n"
        return "OK", parsed_response(wire, "SEARCH")


class ImapProtocolRegressionTests(unittest.TestCase):
    def setUp(self):
        self.target = mail_target(Account("Test", "imap.example.org", "fake@example.org"))

    def test_body_fetch_accepts_uid_after_literal_and_ignores_syntax_in_body(self):
        raw = b"UID 999 BODY[]<9>"
        wire = b"* 1 FETCH (BODY[]<0> {" + str(len(raw)).encode() + b"}\r\n" + raw
        response = parsed_response(wire + b" UID 77)\r\n", "FETCH")
        self.assertEqual(response[-1], b" UID 77)")
        connection = FakeImapConnection(fetch_response=response)
        self.assertEqual(ImapMailbox()._message_chunk(connection, b"77", "9001", 0, len(raw)), raw)

    def test_body_fetch_rejects_ambiguous_uid_or_offset_in_trailer(self):
        for trailer in (b" UID 78)", b" UID 77 UID 77)", b" UID 77 BODY[]<1> NIL)"):
            with self.subTest(trailer=trailer):
                response = parsed_response(
                    b"* 1 FETCH (BODY[]<0> {5}\r\nhello" + trailer + b"\r\n", "FETCH"
                )
                connection = FakeImapConnection(fetch_response=response)
                with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk"):
                    ImapMailbox()._message_chunk(connection, b"77", "9001", 0, 5)

    def test_mime_and_metadata_ignore_only_pure_unsolicited_flag_updates(self):
        mailbox = ImapMailbox()
        body = parsed_response(b"* 1 FETCH (BODY[]<0> {5}\r\nhello UID 77)\r\n", "FETCH")
        metadata = parsed_response(
            b'* 1 FETCH (UID 77 RFC822.SIZE 5 INTERNALDATE " 1-Mar-2026 12:34:56 +0230")\r\n',
            "FETCH",
        )
        for flags in (
            b"2 (FLAGS (\\Seen))",
            b"2 (UID 78 FLAGS (\\Seen))",
            b"2 (FLAGS (\\Seen) UID 78)",
            b"1 (UID 77 FLAGS (UID 77))",
        ):
            with self.subTest(flags=flags):
                updates = parsed_response(
                    b"* "
                    + flags.split(b" ", 1)[0]
                    + b" FETCH "
                    + flags.split(b" ", 1)[1]
                    + b"\r\n",
                    "FETCH",
                )
                connection = FakeImapConnection(fetch_response=body + updates)
                self.assertEqual(mailbox._message_chunk(connection, b"77", "9001", 0, 5), b"hello")
                connection = FakeImapConnection(fetch_response=metadata + updates)
                self.assertIsInstance(
                    mailbox._metadata_batch(connection, [b"77"], "9001")[b"77"], tuple
                )
        conflicting = [b"2 (UID 78 FLAGS (\\Seen) RFC822.SIZE 9)"]
        connection = FakeImapConnection(fetch_response=body + conflicting)
        with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk"):
            mailbox._message_chunk(connection, b"77", "9001", 0, 5)
        connection = FakeImapConnection(fetch_response=metadata + conflicting)
        self.assertIsInstance(
            mailbox._metadata_batch(connection, [b"77"], "9001")[b"77"], Exception
        )

    def test_metadata_accepts_standard_space_padded_internaldate(self):
        wire = b'* 1 FETCH (UID 77 RFC822.SIZE 5 INTERNALDATE " 1-Mar-2026 12:34:56 +0230")\r\n'
        connection = FakeImapConnection(fetch_response=parsed_response(wire, "FETCH"))
        received, size, _headers = ImapMailbox()._metadata_batch(connection, [b"77"], "9001")[b"77"]
        self.assertEqual(received, datetime(2026, 3, 1, 10, 4, 56, tzinfo=timezone.utc))
        self.assertEqual(size, 5)

    def test_large_baseline_uses_bounded_searches_and_retains_high_water_cursor(self):
        connection = LargeMailbox()
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        seen = []
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(uid) or False, sync=sync
        )
        self.assertEqual(list(messages), [])
        self.assertEqual(len(seen), connection.count)
        self.assertEqual(seen[0], "1")
        self.assertEqual(seen[-1], "160001")
        self.assertEqual(sync.next_cursor, "160001")
        self.assertTrue(
            all(
                upper - lower < IMAP_UID_SEARCH_WINDOW
                for lower, upper in connection.sequence_windows
            )
        )
        self.assertEqual(
            connection.sequence_windows,
            [(110002, 160001), (60002, 110001), (10002, 60001), (1, 10001)],
        )
        self.assertTrue(connection.logged_out)

    def test_incremental_search_rechecks_old_ids_without_lowering_cursor(self):
        connection = LargeMailbox()
        sync = SyncSession(lambda _: "160000", lambda _: {"7"})
        seen = []
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(uid) or False, sync=sync
        )
        list(messages)
        self.assertEqual(seen, ["7", "160001"])
        self.assertEqual(sync.next_cursor, "160001")
        self.assertEqual(connection.windows, [(160001, 160001)])

    def test_large_high_uid_mailbox_skips_unassigned_prefix(self):
        connection = LargeMailbox(first_uid=2_000_000_000)
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, _uid: False, sync=sync
        )
        list(messages)
        self.assertEqual(len(connection.windows), 4)
        self.assertEqual(len(connection.sequence_windows), 4)
        self.assertEqual(sync.next_cursor, "2000160000")

    def test_small_sparse_mailbox_uses_count_bound_instead_of_empty_uid_windows(self):
        connection = LargeMailbox(count=3, first_uid=2_000_000_000)
        seen = []
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(uid) or False
        )
        list(messages)
        self.assertEqual(seen, ["2000000000", "2000000001", "2000000002"])
        self.assertEqual(connection.windows, [(1, 2000000002)])

    def sparse_service(self, connection):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mailbox = Mailbox("owner@example.org", ["INBOX"], archive_existing_messages=False)
        account = Account("Owner", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        settings = Settings(
            accounts=[account], rules=[Rule("All", targets=[RuleTarget(str(root / "archive"))])]
        )
        credentials = MemoryCredentialStore()
        store_account_credentials(credentials, account, {"password": "fake"})
        source = ImapMessageSource(credentials, FakeImapMailbox(connection))
        state = WorkspaceStore(root / "profile" / "workspace.sqlite3")
        return make_service(state, Registry(source)), settings, state, mailbox

    def test_sparse_baseline_service_bounds_requests_by_count_and_keeps_all_uids(self):
        connection = SparseMailbox()
        service, settings, state, mailbox = self.sparse_service(connection)
        result = service.run_once(settings)[0]
        self.assertEqual(
            (result.checked, result.skipped_existing, result.failed), (50_001, 50_001, 0)
        )
        self.assertEqual(len(connection.searches), 2)
        self.assertEqual(state.scope(mailbox.id, "INBOX")["cursor"], "4000000000")
        with state.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_message").fetchone()[0], 50_001
            )
            self.assertEqual(db.execute("SELECT count(*) FROM intake").fetchone()[0], 0)
        self.assertEqual(state.spool_usage()[0], 0)
        self.assertTrue(connection.logged_out)

    def test_sparse_empty_range_service_avoids_numeric_uid_walk_and_automatic_cursor(self):
        connection = SparseMailbox(date_matches=False)
        service, settings, state, mailbox = self.sparse_service(connection)
        result = service.run_range(
            settings, {mailbox.id}, start=datetime(2026, 10, 1, tzinfo=timezone.utc)
        )[0]
        self.assertEqual((result.checked, result.archived, result.failed), (0, 0, 0))
        self.assertEqual(len(connection.searches), 2)
        self.assertTrue(all("SINCE 30-Sep-2026" in query for query in connection.searches))
        self.assertIsNone(state.scope(mailbox.id, "INBOX"))
        self.assertEqual(state.incomplete_manual_runs(), [])
        self.assertTrue(connection.logged_out)

    def test_reverse_sequence_pages_keep_survivors_with_expunges_and_new_arrivals(self):
        connection = SparseMailbox(range(1, 100_004))
        initial = set(connection.message_uids)
        removed = {1, 3, 50_004}

        def updates():
            if len(connection.searches) != 1:
                return b""
            connection.message_uids[:] = [
                uid for uid in connection.message_uids if uid not in removed
            ]
            connection.message_uids.extend([100_004, 100_005])
            return b"* 1 EXPUNGE\r\n* 2 EXPUNGE\r\n* 50002 EXPUNGE\r\n* 100002 EXISTS\r\n"

        connection.after_search = updates
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        seen = []
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(int(uid)) or False, sync=sync
        )
        list(messages)
        self.assertEqual(set(seen), initial - {1, 3})
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(sync.next_cursor, "100003")
        self.assertEqual(len(connection.searches), 3)
        followup = SyncSession(lambda _: sync.next_cursor, lambda _: set(), baseline=True)
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(int(uid)) or False, sync=followup
        )
        list(messages)
        self.assertEqual(seen[-2:], [100_004, 100_005])
        self.assertEqual(followup.next_cursor, "100005")

    def test_sequence_endpoint_rejection_replans_only_after_confirmed_expunge(self):
        connection = SparseMailbox()
        connection.reject_outside_sequence = True

        def expunge_before_first_search():
            if len(connection.searches) == 1:
                del connection.message_uids[:10_000]

        connection.before_search = expunge_before_first_search
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        seen = []
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(int(uid)) or False, sync=sync
        )
        list(messages)
        self.assertEqual(seen, connection.message_uids)
        self.assertEqual(len(connection.searches), 3)
        self.assertIn("2:40001", connection.searches[1])
        self.assertEqual(sync.next_cursor, "4000000000")
        self.assertEqual(len([call for call in connection.calls if call[0] == "fetch"]), 2)

    def test_sequence_endpoint_rejection_can_reconcile_a_folder_emptied_by_expunge(self):
        connection = SparseMailbox()
        connection.reject_outside_sequence = True
        connection.before_search = connection.message_uids.clear
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, _uid: False, sync=sync
        )
        self.assertEqual(list(messages), [])
        self.assertEqual(len(connection.searches), 1)
        self.assertEqual(sync.next_cursor, "0")

    def test_sequence_search_failure_without_count_shrink_remains_visible(self):
        connection = SparseMailbox()
        connection.uid = Mock(return_value=("BAD", [b"Unrelated search failure"]))
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        with self.assertRaisesRegex(MailboxError, "Could not load the message list"):
            FakeImapMailbox(connection).fetch_messages(self.target, "fake", sync=sync)
        connection.uid.assert_called_once()
        self.assertIsNone(sync.next_cursor)
        self.assertTrue(connection.logged_out)

    def test_continuously_rejected_shrinking_sequence_range_has_bounded_retries(self):
        connection = SparseMailbox()

        def reject(*args):
            del connection.message_uids[0]
            return "BAD", [b"Invalid message sequence endpoint"]

        connection.uid = Mock(side_effect=reject)
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        with self.assertRaisesRegex(MailboxError, "Could not load the message list"):
            FakeImapMailbox(connection).fetch_messages(self.target, "fake", sync=sync)
        self.assertEqual(connection.uid.call_count, 3)
        self.assertIsNone(sync.next_cursor)
        self.assertTrue(connection.logged_out)

    def test_oversized_sequence_search_response_does_not_advance_discovery(self):
        connection = SparseMailbox()
        response = b" ".join(str(uid).encode() for uid in connection.message_uids)
        connection.uid = Mock(return_value=("OK", [response]))
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        with self.assertRaisesRegex(MailboxError, "too many message UIDs"):
            FakeImapMailbox(connection).fetch_messages(self.target, "fake", sync=sync)
        connection.uid.assert_called_once()
        self.assertIsNone(sync.next_cursor)
        self.assertTrue(connection.logged_out)

    def test_sequence_replan_checks_uidvalidity_before_retrying(self):
        connection = SparseMailbox()
        connection.reject_outside_sequence = True

        def reset_namespace():
            del connection.message_uids[:10_000]
            connection.validity_data = [b"9002"]

        connection.before_search = reset_namespace
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        with self.assertRaises(RemoteMessageNamespaceChanged):
            FakeImapMailbox(connection).fetch_messages(self.target, "fake", sync=sync)
        self.assertEqual(len(connection.searches), 1)
        self.assertIsNone(sync.next_cursor)
        self.assertTrue(connection.logged_out)

    def test_arrivals_after_search_high_water_are_left_for_next_check(self):
        connection = LargeMailbox(count=3)
        connection.after_search = lambda: setattr(connection, "count", 4)
        sync = SyncSession(lambda _: None, lambda _: set(), baseline=True)
        seen = []
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(uid) or False, sync=sync
        )
        list(messages)
        self.assertEqual(seen, ["1", "2", "3"])
        self.assertEqual(sync.next_cursor, "3")
        followup = SyncSession(lambda _: sync.next_cursor, lambda _: set())
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, uid: seen.append(uid) or False, sync=followup
        )
        list(messages)
        self.assertEqual(seen, ["1", "2", "3", "4"])
        self.assertEqual(followup.next_cursor, "4")

    def test_range_resume_preserves_uid_progress_and_conservative_dates(self):
        saved = {"token": "150000", "complete": False}
        cancelled = threading.Event()

        def save(_namespace, token, complete, _force):
            saved.update(token=token, complete=complete)
            cancelled.set()
            return True

        pagination = RangePagination(None, "150000", save)
        pagination.resume_namespace = self.target_namespace()
        connection = LargeMailbox()
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target,
            "fake",
            lambda _scope, _uid: False,
            received_between=(
                datetime(2026, 3, 1, tzinfo=timezone.utc),
                datetime(2026, 3, 2, tzinfo=timezone.utc),
            ),
            range_sync=pagination,
            cancellation=Cancellation(cancelled.is_set),
        )
        # Stop partway through an iteration and recreate its durable pagination.
        # A skipped known UID advances safely without fetching its body.
        with self.assertRaises(ProcessingStopped):
            next(messages)
        self.assertEqual(saved["token"], "150001")
        restarted = LargeMailbox()
        pagination = RangePagination(self.target_namespace(), saved["token"], save)
        _, messages = FakeImapMailbox(restarted).fetch_messages(
            self.target, "fake", lambda _scope, _uid: False, range_sync=pagination
        )
        list(messages)
        self.assertEqual(restarted.windows, [(150002, 160001)])
        self.assertEqual(restarted.sequence_windows, [])
        self.assertTrue(saved["complete"])
        self.assertIsNone(saved["token"])
        criterion = next(call[-1] for call in connection.calls if call[:2] == ("uid", "search"))
        self.assertIn("SINCE 28-Feb-2026 BEFORE 04-Mar-2026", criterion)

    def target_namespace(self):
        from mailarchive.domain.source_identity import imap_scope

        return imap_scope(self.target, "9001").processing_namespace

    def test_cancellation_between_windows_closes_connection_without_advancing_cursor(self):
        cancelled = threading.Event()
        connection = LargeMailbox()
        connection.after_search = lambda: cancelled.set() if len(connection.windows) == 2 else None
        sync = SyncSession(lambda _: None, lambda _: set())
        with self.assertRaises(ProcessingStopped):
            FakeImapMailbox(connection).fetch_messages(
                self.target, "fake", sync=sync, cancellation=Cancellation(cancelled.is_set)
            )
        self.assertEqual(len(connection.windows), 2)
        self.assertIsNone(sync.next_cursor)
        self.assertTrue(connection.logged_out)

    def test_oauth_expiry_between_windows_restarts_enumeration_without_skipping_ids(self):
        old, renewed = LargeMailbox(), LargeMailbox(count=160_002)

        def expire():
            if len(old.windows) == 2:
                raise imaplib.IMAP4.abort("AccessTokenExpired")

        old.after_search = expire
        mailbox = ImapMailbox()
        mailbox._connect = Mock(side_effect=[old, renewed])
        target = mail_target(
            Account(
                "Outlook",
                "outlook.office365.com",
                "fake@example.org",
                auth_mode=AuthMode.OAUTH_USER,
            )
        )
        sync = SyncSession(lambda _: None, lambda _: set())
        seen = []
        refresh = Mock(return_value="new-fake-token")
        _, messages = mailbox.fetch_messages(
            target,
            None,
            lambda _scope, uid: seen.append(uid) or False,
            access_token="fake-token",
            refresh_access_token=refresh,
            sync=sync,
        )
        list(messages)
        self.assertEqual(len(seen), 160001)
        self.assertEqual(len(set(seen)), 160001)
        self.assertEqual(sync.next_cursor, "160001")
        self.assertEqual(len(old.windows), 2)
        self.assertEqual(len(renewed.windows), 4)
        refresh.assert_called_once_with()
        self.assertTrue(old.logged_out and renewed.logged_out)
        followup = SyncSession(lambda _: sync.next_cursor, lambda _: set(), baseline=True)
        _, messages = FakeImapMailbox(renewed).fetch_messages(
            target,
            None,
            lambda _scope, uid: seen.append(uid) or False,
            access_token="new-fake-token",
            sync=followup,
        )
        list(messages)
        self.assertEqual(seen[-1], "160002")
        self.assertEqual(followup.next_cursor, "160002")

    def test_highest_sequence_fetch_establishes_finite_bound_without_uidnext(self):
        connection = FakeImapConnection(uids=b"77 78")
        connection.response = lambda name: (name, [None] if name == "UIDNEXT" else [b"9001"])
        connection.fetch = Mock(return_value=("OK", [b"2 (UID 78)"]))
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, _uid: False
        )
        list(messages)
        connection.fetch.assert_called_once_with("*", "(UID)")
        self.assertIn(("uid", "search", None, "UID 1:78"), connection.calls)

    def test_high_water_fetch_accepts_unsolicited_flag_updates(self):
        connection = FakeImapConnection(uids=b"77 78")
        connection.fetch = Mock(
            return_value=("OK", [b"1 (FLAGS (UID 77))", b"2 (UID 78 FLAGS (\\Seen))"])
        )
        sync = SyncSession(lambda _: None, lambda _: set())
        _, messages = FakeImapMailbox(connection).fetch_messages(
            self.target, "fake", lambda _scope, _uid: False, sync=sync
        )
        list(messages)
        self.assertEqual(sync.next_cursor, "78")
