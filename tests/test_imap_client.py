import re
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from mailarchive.imap_client import (
    ImapMailbox,
    MailboxError,
    RemoteMessageError,
    RemoteMessageUnavailable,
    _imap_search_date,
    _parse_internaldate,
)
from mailarchive.models import Account, AuthMode, Mailbox
from mailarchive.synchronization import RangePagination, SyncSession
from tests.helpers import imap_namespace, mail_target, sample_mail


class FakeImapConnection:
    def __init__(
        self,
        *,
        select_status="OK",
        search_status="OK",
        uids=b"77",
        fetch_status="OK",
        fetch_response=None,
        raw_by_uid=None,
        validity_data=None,
        validity_responses=None,
        login_error=None,
        uid_error=None,
        uid_error_command=None,
        close_error=None,
        logout_error=None,
        capabilities=(b"IMAP4REV1", b"AUTH=XOAUTH2"),
        authenticate_error=None,
        authenticate_challenges=1,
    ) -> None:
        self.calls = []
        self.closed = False
        self.logged_out = False
        self.select_status = select_status
        self.search_status = search_status
        self.uids = uids
        self.fetch_status = fetch_status
        self.fetch_response = fetch_response
        self.raw_by_uid = raw_by_uid
        self.validity_data = [b"9001"] if validity_data is None else validity_data
        self.validity_responses = list(validity_responses) if validity_responses is not None else []
        self.login_error = login_error
        self.uid_error = uid_error
        self.uid_error_command = uid_error_command
        self.close_error = close_error
        self.logout_error = logout_error
        self.capabilities = capabilities
        self.authenticate_error = authenticate_error
        self.authenticate_challenges = authenticate_challenges

    def login(self, username, password):
        self.calls.append(("login", username, password))
        if self.login_error is not None:
            raise self.login_error

    def authenticate(self, mechanism, auth_object):
        responses = [auth_object(b"challenge") for _ in range(self.authenticate_challenges)]
        self.calls.append(("authenticate", mechanism, responses))
        if self.authenticate_error is not None:
            raise self.authenticate_error
        return "OK", [b"authenticated"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return self.select_status, [b"1"]

    def response(self, name):
        return "UIDVALIDITY", (
            self.validity_responses.pop(0) if self.validity_responses else self.validity_data
        )

    def uid(self, command, *arguments):
        self.calls.append(("uid", command, *arguments))
        if self.uid_error is not None and (
            self.uid_error_command is None or self.uid_error_command == command
        ):
            raise self.uid_error
        if command == "search":
            return self.search_status, [self.uids]
        request = arguments[-1]
        response = self.fetch_response
        raw = self.raw_by_uid[arguments[0]] if self.raw_by_uid is not None else sample_mail()
        if response is None and request == "(RFC822.SIZE INTERNALDATE)":
            response = [
                b"1 (UID "
                + arguments[0]
                + b" RFC822.SIZE "
                + str(len(raw)).encode()
                + b' INTERNALDATE "21-Sep-2026 00:00:00 +0000")'
            ]
        elif response is None and str(request).startswith("(BODY.PEEK[]<"):
            match = re.search(r"<([0-9]+)\.([0-9]+)>", str(request))
            start, count = map(int, match.groups())
            chunk = raw[start : start + count]
            response = [
                (
                    b"1 (UID "
                    + arguments[0]
                    + b" BODY[]<"
                    + str(start).encode()
                    + b"> {"
                    + str(len(chunk)).encode()
                    + b"}",
                    chunk,
                )
            ]
        return self.fetch_status, response

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def logout(self):
        self.logged_out = True
        if self.logout_error is not None:
            raise self.logout_error


class FakeImapMailbox(ImapMailbox):
    def __init__(self, connection):
        self.connection = connection

    def _connect(self, account):
        return self.connection


class ImapMailboxTests(unittest.TestCase):
    def test_message_chunk_uses_only_the_requested_uid_and_offset(self) -> None:
        mailbox = FakeImapMailbox(None)
        mixed_uids = FakeImapConnection(
            fetch_response=[
                (b"1 (UID 999 BODY[]<0>", b"WRONG"),
                (b"2 (UID 77 BODY[]<0>", b"RIGHT"),
            ]
        )
        with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
            mailbox._message_chunk(mixed_uids, b"77", "9001", 0, 5)

        wrong_offset = FakeImapConnection(fetch_response=[(b"1 (UID 77 BODY[]<5>", b"WRONG")])
        with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
            mailbox._message_chunk(wrong_offset, b"77", "9001", 0, 5)

        wrong_eof = FakeImapConnection(fetch_response=[(b"1 (UID 999 BODY[]<5>", b"X")])
        with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
            mailbox._message_chunk(wrong_eof, b"77", "9001", 5, 1, eof_probe=True)

        missing_eof_body = FakeImapConnection(fetch_response=[b"1 (UID 77 BODY[] NIL)"])
        with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
            mailbox._message_chunk(missing_eof_body, b"77", "9001", 5, 1, eof_probe=True)

        ambiguous = FakeImapConnection(
            fetch_response=[
                (b"1 (UID 77 BODY[]<0> {5}", b"FIRST"),
                (b"2 (UID 77 BODY[]<0> {5}", b"OTHER"),
            ]
        )
        with self.assertRaisesRegex(MailboxError, "ambiguous MIME chunk"):
            mailbox._message_chunk(ambiguous, b"77", "9001", 0, 5)

        duplicate_marker = FakeImapConnection(
            fetch_response=[(b"1 (UID 77 UID 77 BODY[]<0>", b"WRONG")]
        )
        with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
            mailbox._message_chunk(duplicate_marker, b"77", "9001", 0, 5)

        disappeared = FakeImapConnection(fetch_response=[b"1 (UID 77 BODY[] NIL)"], uids=b"")
        with self.assertRaises(RemoteMessageUnavailable):
            mailbox._message_chunk(disappeared, b"77", "9001", 0, 5)

        for extra in (
            b"1 (UID 999 BODY[]<0> NIL)",
            (b"malformed",),
            (b"1 (UID 77 BODY[]<0>", b"RIGHT", b"extra"),
        ):
            with self.subTest(extra=extra):
                malformed = FakeImapConnection(
                    fetch_response=[(b"1 (UID 77 BODY[]<0>", b"RIGHT"), extra]
                )
                with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
                    mailbox._message_chunk(malformed, b"77", "9001", 0, 5)

        for header in (
            b"1 (UID 77 BODY[]<0>",
            b"1 (UID 77 BODY[]<0> {999}",
            b"1 (UID 77 BODY[]<0> {5} {5}",
        ):
            with self.subTest(header=header):
                malformed_literal = FakeImapConnection(fetch_response=[(header, b"RIGHT")])
                with self.assertRaisesRegex(MailboxError, "unexpected MIME chunk response"):
                    mailbox._message_chunk(malformed_literal, b"77", "9001", 0, 5)

    def test_imap_dates_use_fixed_english_months_without_process_locale(self) -> None:
        march = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)

        self.assertEqual(_imap_search_date(march), "01-Mar-2026")
        self.assertEqual(
            _parse_internaldate(b"01-Mar-2026 12:34:56 +0230").isoformat(),
            "2026-03-01T12:34:56+02:30",
        )
        with self.assertRaises(ValueError):
            _parse_internaldate(b"01-Mar-2026 12:34:56 +1260")

    def test_malformed_imap_message_does_not_block_later_uids(self) -> None:
        raw = sample_mail()
        connection = FakeImapConnection(uids=b"77 78", raw_by_uid={b"77": raw, b"78": raw})
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        original_metadata = mailbox._message_metadata

        def metadata(client, uid, uid_validity):
            if uid == b"77":
                raise RemoteMessageError("Message 77 has no INTERNALDATE.")
            return original_metadata(client, uid, uid_validity)

        sync = SyncSession(lambda _namespace: None, lambda _namespace: set())
        with patch.object(mailbox, "_message_metadata", side_effect=metadata):
            _scope, messages = mailbox.fetch_messages(
                mail_target(account), "secret", lambda _scope, _uid: True, sync=sync
            )
            fetched = list(messages)

        self.assertEqual([message.id for message in fetched], ["77", "78"])
        self.assertIsInstance(fetched[0].error, RemoteMessageError)
        self.assertIsNone(fetched[1].error)
        self.assertEqual(sync.next_cursor, "78")

    def test_targeted_fetch_uses_exact_uid_and_releases_session(self) -> None:
        raw = b"Subject: targeted\r\n\r\nbody"
        connection = FakeImapConnection(raw_by_uid={b"77": raw})
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        remote = FakeImapMailbox(connection).fetch_message(
            mail_target(account), "77", imap_namespace(account, "9001"), "secret"
        )

        self.assertIsNotNone(remote)
        self.assertIn(("uid", "search", None, "UID 77"), connection.calls)
        self.assertEqual(b"".join(remote.iter_raw()), raw)
        self.assertFalse(connection.closed)
        remote.release_resources()
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_targeted_fetch_rejects_changed_uidvalidity_and_missing_uid(self) -> None:
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        changed = FakeImapConnection()
        with self.assertRaisesRegex(MailboxError, "UIDVALIDITY changed"):
            FakeImapMailbox(changed).fetch_message(
                mail_target(account), "77", imap_namespace(account, "old"), "secret"
            )
        self.assertTrue(changed.logged_out)

        missing = FakeImapConnection(uids=b"")
        self.assertIsNone(
            FakeImapMailbox(missing).fetch_message(
                mail_target(account), "77", imap_namespace(account, "9001"), "secret"
            )
        )
        self.assertTrue(missing.logged_out)

    def test_selects_readonly_and_fetches_without_seen_flag(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        validity, messages = mailbox.fetch_messages(mail_target(account), "secret")
        fetched = list(messages)

        self.assertEqual(validity.processing_namespace, imap_namespace(account, "9001"))
        self.assertEqual(fetched[0].id, "77")
        self.assertIn(("select", '"INBOX"', True), connection.calls)
        self.assertIn(("uid", "fetch", b"77", "(RFC822.SIZE INTERNALDATE)"), connection.calls)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_streams_message_with_bounded_partial_fetches(self) -> None:
        raw = b"0123456789"
        connection = FakeImapConnection(raw_by_uid={b"77": raw})
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        with patch("mailarchive.imap_client.MESSAGE_CHUNK_BYTES", 4):
            _, messages = FakeImapMailbox(connection).fetch_messages(mail_target(account), "secret")
            remote = next(messages)
            self.assertEqual(remote.raw_size, len(raw))
            self.assertEqual(b"".join(remote.iter_raw()), raw)
            with self.assertRaises(StopIteration):
                next(messages)

        partials = [
            call[-1]
            for call in connection.calls
            if call[:2] == ("uid", "fetch") and "BODY.PEEK" in str(call[-1])
        ]
        self.assertEqual(
            partials,
            [
                "(BODY.PEEK[]<0.4>)",
                "(BODY.PEEK[]<4.4>)",
                "(BODY.PEEK[]<8.2>)",
                "(BODY.PEEK[]<10.1>)",
            ],
        )

    def test_rejects_body_longer_than_declared_message_size(self) -> None:
        raw = b"0123456789"
        connection = FakeImapConnection(raw_by_uid={b"77": raw})
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        with patch.object(
            mailbox,
            "_message_metadata",
            return_value=(datetime(2026, 9, 21, tzinfo=timezone.utc), 8),
        ):
            _, messages = mailbox.fetch_messages(mail_target(account), "secret")
            remote = next(messages)

        with self.assertRaisesRegex(MailboxError, "exceeded its declared size"):
            b"".join(remote.iter_raw())

        messages.close()
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_eof_probe_reports_message_that_disappeared_after_download(self) -> None:
        connection = FakeImapConnection(raw_by_uid={b"77": b"complete"})
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        with patch.object(mailbox, "_recheck_uids", return_value=set()):
            _, messages = mailbox.fetch_messages(mail_target(account), "secret")
            remote = next(messages)

            with self.assertRaises(RemoteMessageUnavailable):
                b"".join(remote.iter_raw())

        messages.close()
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_message_size_is_not_limited_to_uid_width(self) -> None:
        connection = FakeImapConnection(
            fetch_response=[
                b'1 (UID 77 RFC822.SIZE 4294967296 INTERNALDATE "21-Sep-2026 00:00:00 +0000")'
            ]
        )
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        _, messages = FakeImapMailbox(connection).fetch_messages(mail_target(account), "secret")

        self.assertEqual(next(messages).raw_size, 4294967296)
        messages.close()

    def test_manual_range_resumes_after_its_saved_uid(self) -> None:
        raw = sample_mail()
        connection = FakeImapConnection(uids=b"77 78", raw_by_uid={b"77": raw, b"78": raw})
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        saved = {"token": "77", "complete": False}

        def save(_namespace, token, complete, _force):
            saved.update(token=token, complete=complete)
            return True

        pagination = RangePagination(imap_namespace(account, "9001"), "77", save)
        _, messages = FakeImapMailbox(connection).fetch_messages(
            mail_target(account), "secret", range_sync=pagination
        )

        self.assertEqual([message.id for message in messages], ["78"])
        self.assertIn(("uid", "search", None, "UID 78:*"), connection.calls)
        self.assertTrue(saved["complete"])
        self.assertIsNone(saved["token"])

    def test_skips_body_fetch_for_a_known_uid(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        _, messages = mailbox.fetch_messages(
            mail_target(account),
            "secret",
            lambda uid_validity, uid: False,
        )

        self.assertEqual(list(messages), [])
        self.assertIn(("uid", "search", None, "ALL"), connection.calls)
        self.assertNotIn(("uid", "fetch", b"77", "(RFC822.SIZE INTERNALDATE)"), connection.calls)

    def test_failed_login_is_mapped_and_logs_out(self) -> None:
        connection = FakeImapConnection(login_error=OSError("connection reset"))
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        with self.assertRaisesRegex(MailboxError, "connection reset"):
            mailbox.fetch_messages(mail_target(account), "secret")

        self.assertTrue(connection.logged_out)
        self.assertFalse(connection.closed)

    def test_xoauth2_sends_raw_bearer_payload_once_without_password_login(self) -> None:
        connection = FakeImapConnection(authenticate_challenges=2)
        account = Account(
            "Outlook",
            "outlook.office365.com",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        validity, messages = FakeImapMailbox(connection).fetch_messages(
            mail_target(account),
            None,
            access_token="access-token",
        )

        self.assertEqual(validity.processing_namespace, imap_namespace(account, "9001"))
        self.assertEqual(list(messages)[0].id, "77")
        authenticate_calls = [call for call in connection.calls if call[0] == "authenticate"]
        self.assertEqual(
            authenticate_calls,
            [
                (
                    "authenticate",
                    "XOAUTH2",
                    [
                        b"user=me@example.org\x01auth=Bearer access-token\x01\x01",
                        b"",
                    ],
                )
            ],
        )
        self.assertFalse(any(call[0] == "login" for call in connection.calls))

    def test_xoauth2_requires_advertised_server_capability(self) -> None:
        connection = FakeImapConnection(capabilities=(b"IMAP4REV1",))
        account = Account(
            "Outlook",
            "outlook.office365.com",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        with self.assertRaisesRegex(MailboxError, "does not support OAuth"):
            FakeImapMailbox(connection).fetch_messages(
                mail_target(account),
                None,
                access_token="access-token",
            )

        self.assertFalse(any(call[0] == "authenticate" for call in connection.calls))
        self.assertTrue(connection.logged_out)

    def test_xoauth2_failure_does_not_expose_access_token(self) -> None:
        connection = FakeImapConnection(
            authenticate_error=OSError("rejected access-token"),
        )
        account = Account(
            "Outlook",
            "outlook.office365.com",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        with self.assertRaises(MailboxError) as raised:
            FakeImapMailbox(connection).fetch_messages(
                mail_target(account),
                None,
                access_token="access-token",
            )

        self.assertIn("IMAP OAuth authentication failed", str(raised.exception))
        self.assertIn("Reauthorize", str(raised.exception))
        self.assertNotIn("access-token", str(raised.exception))
        self.assertTrue(connection.logged_out)

    def test_rejects_ambiguous_or_missing_authentication_credentials(self) -> None:
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        mailbox = FakeImapMailbox(FakeImapConnection())

        with self.assertRaisesRegex(MailboxError, "cannot be used together"):
            mailbox.fetch_messages(mail_target(account), "password", access_token="access-token")
        with self.assertRaisesRegex(MailboxError, "credentials are missing"):
            mailbox.fetch_messages(mail_target(account), None)

    def test_rejects_oauth_before_connecting_when_endpoint_is_not_trusted(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Outlook",
            "imap.attacker.example",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        with self.assertRaisesRegex(MailboxError, "outlook.office365.com"):
            mailbox.fetch_messages(mail_target(account), None, access_token="access-token")

        self.assertEqual(connection.calls, [])

    def test_select_and_search_failures_are_clear_and_log_out(self) -> None:
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        scenarios = (
            (FakeImapConnection(select_status="NO"), "Could not open mailbox folder 'INBOX'"),
            (FakeImapConnection(search_status="NO"), "Could not load the message list"),
        )
        for connection, message in scenarios:
            with self.subTest(message=message):
                with self.assertRaisesRegex(MailboxError, message):
                    FakeImapMailbox(connection).fetch_messages(mail_target(account), "secret")
                self.assertTrue(connection.logged_out)

    def test_empty_search_with_valid_uid_validity_is_supported(self) -> None:
        connection = FakeImapConnection(uids=b"")
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        validity, messages = mailbox.fetch_messages(mail_target(account), "secret")

        self.assertEqual(validity.processing_namespace, imap_namespace(account, "9001"))
        self.assertEqual(list(messages), [])
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_command_level_fetch_failure_stops_scan_without_advancing_cursor(self) -> None:
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        connection = FakeImapConnection(uids=b"77 78", fetch_status="NO")
        sync = SyncSession(lambda _namespace: None, lambda _namespace: set())
        _, messages = FakeImapMailbox(connection).fetch_messages(
            mail_target(account), "secret", lambda _scope, _uid: True, sync=sync
        )

        with self.assertRaisesRegex(MailboxError, "Could not load message 77"):
            list(messages)

        self.assertIsNone(sync.next_cursor)
        metadata_fetches = [
            call
            for call in connection.calls
            if call[:2] == ("uid", "fetch") and call[-1] == "(RFC822.SIZE INTERNALDATE)"
        ]
        self.assertEqual(len(metadata_fetches), 1)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_missing_or_malformed_fetch_metadata_isolated_per_message(self) -> None:
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        scenarios = (
            (
                FakeImapConnection(fetch_response=[]),
                RemoteMessageUnavailable,
                "no longer available",
            ),
            (
                FakeImapConnection(fetch_response=[b"1 (UID 77 metadata without a body)"]),
                RemoteMessageError,
                "Message 77 has no valid size",
            ),
        )
        for connection, error_type, message in scenarios:
            with self.subTest(message=message):
                _, messages = FakeImapMailbox(connection).fetch_messages(
                    mail_target(account), "secret"
                )
                fetched = list(messages)
                self.assertEqual(len(fetched), 1)
                self.assertIsInstance(fetched[0].error, error_type)
                self.assertRegex(str(fetched[0].error), message)
                self.assertTrue(connection.closed)
                self.assertTrue(connection.logged_out)

    def test_metadata_rejects_multiple_fetch_records(self) -> None:
        connection = FakeImapConnection(
            fetch_response=[
                b'9 (UID 999 RFC822.SIZE 1 INTERNALDATE "01-Jan-2001 00:00:00 +0000")',
                b'1 (UID 77 RFC822.SIZE 123 INTERNALDATE "21-Sep-2026 00:00:00 +0000")',
            ]
        )

        with self.assertRaisesRegex(RemoteMessageError, "ambiguous metadata"):
            FakeImapMailbox(connection)._message_metadata(connection, b"77", "9001")

        duplicate = FakeImapConnection(
            fetch_response=[
                b'1 (UID 77 RFC822.SIZE 5 INTERNALDATE "21-Sep-2026 00:00:00 +0000")',
                b'1 (UID 77 RFC822.SIZE 999 INTERNALDATE "22-Sep-2026 00:00:00 +0000")',
            ]
        )
        with self.assertRaisesRegex(RemoteMessageError, "ambiguous metadata"):
            FakeImapMailbox(duplicate)._message_metadata(duplicate, b"77", "9001")

    def test_foreign_uid_metadata_does_not_describe_requested_message(self) -> None:
        connection = FakeImapConnection(
            fetch_response=[
                b'9 (UID 999 RFC822.SIZE 123 INTERNALDATE "21-Sep-2026 00:00:00 +0000")'
            ]
        )

        with self.assertRaises(RemoteMessageUnavailable):
            FakeImapMailbox(connection)._message_metadata(connection, b"77", "9001")

    def test_transport_failure_during_fetch_is_mapped_and_cleanup_errors_are_ignored(self) -> None:
        connection = FakeImapConnection(
            uid_error=OSError("socket closed"),
            uid_error_command="fetch",
            close_error=RuntimeError("already closed"),
            logout_error=RuntimeError("already logged out"),
        )
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        _, messages = mailbox.fetch_messages(mail_target(account), "secret")

        with self.assertRaisesRegex(MailboxError, "socket closed"):
            list(messages)

        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_closing_partially_consumed_iterator_releases_connection(self) -> None:
        connection = FakeImapConnection(uids=b"77 78")
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )
        _, messages = mailbox.fetch_messages(mail_target(account), "secret")

        self.assertEqual(next(messages).id, "77")
        messages.close()

        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)
        fetch_calls = [call for call in connection.calls if call[:2] == ("uid", "fetch")]
        self.assertEqual(len(fetch_calls), 1)

    def test_filter_failure_still_releases_connection(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Personal",
            "imap.example.org",
            "me@example.org",
            mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
        )

        def failing_filter(_validity, _uid):
            raise RuntimeError("filter failed")

        _, messages = mailbox.fetch_messages(mail_target(account), "secret", failing_filter)
        with self.assertRaisesRegex(RuntimeError, "filter failed"):
            list(messages)

        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_connect_uses_ssl_or_starttls_according_to_account(self) -> None:
        context = object()
        ssl_client = object()
        tls_client = Mock()
        with (
            patch(
                "mailarchive.imap_client.ssl.create_default_context",
                return_value=context,
            ),
            patch(
                "mailarchive.imap_client.imaplib.IMAP4_SSL",
                return_value=ssl_client,
            ) as imap_ssl,
            patch(
                "mailarchive.imap_client.imaplib.IMAP4",
                return_value=tls_client,
            ) as imap_plain,
        ):
            ssl_account = Account(
                "SSL",
                "secure.example.org",
                "me@example.org",
                port=993,
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )
            plain_account = Account(
                "STARTTLS",
                "plain.example.org",
                "me@example.org",
                port=143,
                use_ssl=False,
                mailboxes=[Mailbox("me@example.org", folders=["INBOX"])],
            )

            self.assertIs(ImapMailbox()._connect(ssl_account), ssl_client)
            self.assertIs(ImapMailbox()._connect(plain_account), tls_client)

        imap_ssl.assert_called_once_with(
            "secure.example.org",
            993,
            ssl_context=context,
            timeout=30,
        )
        imap_plain.assert_called_once_with("plain.example.org", 143, timeout=30)
        tls_client.starttls.assert_called_once_with(ssl_context=context)


if __name__ == "__main__":
    unittest.main()
