import imaplib
import unittest
from unittest.mock import Mock

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import ImapMailbox, MailboxError
from mailarchive.mail_sources import ImapMessageSource
from mailarchive.models import Account, AuthMode, Mailbox
from mailarchive.oauth import AuthorizationError
from mailarchive.synchronization import SyncSession
from tests.helpers import imap_namespace, mail_target
from tests.test_imap_client import FakeImapConnection
from tests.test_mail_sources import FakeOAuth

EXPIRED = "command: UID => Session invalidated - AccessTokenExpired"


class ImapOAuthRefreshTests(unittest.TestCase):
    def setUp(self):
        self.account = Account(
            "Outlook",
            "outlook.office365.com",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("shared@example.org", folders=['Archive/"Old"'])],
        )
        self.mailbox = ImapMailbox()
        self.refresh = Mock(return_value="new-token")
        self.sync = SyncSession(lambda _: None, lambda _: set())

    def scan(self, connections, should_fetch=None, **options):
        self.mailbox._connect = Mock(side_effect=connections)
        return self.mailbox.fetch_messages(
            mail_target(self.account),
            None,
            should_fetch,
            access_token="old-token",
            refresh_access_token=self.refresh,
            sync=self.sync,
            **options,
        )

    def assert_reauthenticated(self, connection):
        self.assertEqual(
            connection.calls[:2],
            [
                (
                    "authenticate",
                    "XOAUTH2",
                    [b"user=shared@example.org\x01auth=Bearer new-token\x01\x01"],
                ),
                ("select", '"Archive/\\"Old\\""', True),
            ],
        )

    def test_expiry_mid_scan_retries_only_failed_uid_and_continues(self):
        old, new = FakeImapConnection(uids=b"77 78 79"), FakeImapConnection()
        should_fetch = Mock(return_value=True)
        scope, messages = self.scan([old, new], should_fetch)
        self.assertEqual(next(messages).id, "77")
        old.uid_error = imaplib.IMAP4.abort(EXPIRED)

        self.assertEqual([message.id for message in messages], ["78", "79"])

        self.assertEqual(scope.processing_namespace, imap_namespace(self.account, "9001"))
        self.assertEqual(self.sync.next_cursor, "79")
        self.assertEqual([call.args[1] for call in should_fetch.call_args_list], ["77", "78", "79"])
        self.refresh.assert_called_once_with()
        self.assert_reauthenticated(new)
        self.assertEqual(
            [call for call in new.calls if call[0] == "uid"],
            [
                ("uid", "fetch", b"78", "(RFC822.SIZE INTERNALDATE)"),
                ("uid", "fetch", b"79", "(RFC822.SIZE INTERNALDATE)"),
            ],
        )
        self.assertTrue(old.closed and old.logged_out and new.closed and new.logged_out)

    def test_later_expiry_in_same_scan_can_refresh_again(self):
        connections = [FakeImapConnection(uids=b"77 78 79") for _ in range(3)]
        _, messages = self.scan(connections)
        for connection, uid in zip(connections, ["77", "78", "79"], strict=True):
            self.assertEqual(next(messages).id, uid)
            connection.uid_error = imaplib.IMAP4.abort(EXPIRED)
        self.assertEqual(list(messages), [])
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.sync.next_cursor, "79")
        self.assertTrue(all(connection.logged_out for connection in connections))

    def test_search_expiry_repeats_incremental_search_after_reselect(self):
        self.sync.cursor_for = lambda _: "76"
        for returned_status in (False, True):
            with self.subTest(returned_status=returned_status):
                self.refresh.reset_mock()
                old = FakeImapConnection(
                    search_status="NO" if returned_status else "OK",
                    uids=EXPIRED.encode(),
                    uid_error=None if returned_status else imaplib.IMAP4.error(EXPIRED),
                )
                new = FakeImapConnection()
                _, messages = self.scan([old, new])
                self.assertEqual([message.id for message in messages], ["77"])
                self.refresh.assert_called_once_with()
                self.assert_reauthenticated(new)
                self.assertIn(("uid", "search", None, "UID 77:*"), new.calls)
                self.assertEqual(self.sync.next_cursor, "77")

    def test_fetch_expiry_returned_as_no_response_is_retried(self):
        old = FakeImapConnection(fetch_status="NO", fetch_response=[EXPIRED.encode()])
        new = FakeImapConnection()
        _, messages = self.scan([old, new])
        self.assertEqual([message.id for message in messages], ["77"])
        self.refresh.assert_called_once_with()

    def test_select_expiry_is_retried_before_establishing_scope(self):
        old = FakeImapConnection()
        old.select = Mock(side_effect=imaplib.IMAP4.abort(EXPIRED))
        new = FakeImapConnection()
        _, messages = self.scan([old, new])
        self.assertEqual([message.id for message in messages], ["77"])
        self.assertTrue(old.logged_out and new.logged_out)
        self.refresh.assert_called_once_with()

    def test_expiry_during_initial_authentication_can_renew(self):
        old = FakeImapConnection(authenticate_error=imaplib.IMAP4.error(EXPIRED))
        new = FakeImapConnection()
        _, messages = self.scan([old, new])
        self.assertEqual([message.id for message in messages], ["77"])
        self.assertTrue(old.logged_out and new.logged_out)
        self.refresh.assert_called_once_with()

    def test_folder_discovery_renews_and_repeats_list_without_selecting_a_folder(self):
        for response in (imaplib.IMAP4.abort(EXPIRED), ("NO", [EXPIRED.encode()])):
            with self.subTest(response=response):
                old, new = FakeImapConnection(), FakeImapConnection()
                old.list = Mock(side_effect=[response])
                new.list = Mock(return_value=("OK", [b'() "/" "INBOX"', b'() "/" "Archive"']))
                self.mailbox._connect = Mock(side_effect=[old, new])
                oauth = FakeOAuth()
                source = ImapMessageSource(MemoryCredentialStore(), self.mailbox, oauth)
                self.assertEqual(
                    source.list_folders(mail_target(self.account)), ["INBOX", "Archive"]
                )
                self.assertEqual(oauth.microsoft_force_refresh, [False, True])
                self.assertFalse(any(call[0] == "select" for call in new.calls))
                self.assertTrue(old.logged_out and new.logged_out)
                self.assertEqual(old.list.call_args, new.list.call_args)

    def test_recheck_search_expiry_preserves_requested_ids(self):
        self.sync.recheck_ids_for = lambda _: {"77", "78"}
        old = FakeImapConnection()
        old.uid = Mock(side_effect=[("OK", [b"79"]), imaplib.IMAP4.abort(EXPIRED)])
        new = FakeImapConnection(uids=b"77 79")
        _, messages = self.scan([old, new])
        self.assertEqual([message.id for message in messages], ["77", "79"])
        self.assertEqual(self.sync.next_cursor, "79")
        self.assertEqual(self.sync.discarded_ids, {"78"})
        self.assertIn(("uid", "search", None, "UID 77,78"), new.calls)
        self.refresh.assert_called_once_with()

    def test_reconnect_rejects_changed_missing_or_invalid_uidvalidity_before_fetch(self):
        for validity in ([b"9002"], [None], [b"invalid"]):
            with self.subTest(validity=validity):
                old = FakeImapConnection(
                    uid_error=imaplib.IMAP4.abort(EXPIRED), uid_error_command="fetch"
                )
                new = FakeImapConnection(validity_data=validity)
                _, messages = self.scan([old, new])
                with self.assertRaisesRegex(MailboxError, "UIDVALIDITY"):
                    list(messages)
                self.assertFalse(any(call[0] == "uid" for call in new.calls))
                self.assertIsNone(self.sync.next_cursor)
                self.assertTrue(old.logged_out and new.logged_out)

    def test_immediately_expired_replacement_is_not_retried_forever(self):
        connections = [
            FakeImapConnection(uid_error=imaplib.IMAP4.abort(EXPIRED), uid_error_command="fetch")
            for _ in range(2)
        ]
        _, messages = self.scan(connections)
        with self.assertRaisesRegex(MailboxError, "AccessTokenExpired"):
            list(messages)
        self.refresh.assert_called_once_with()
        self.assertEqual(self.mailbox._connect.call_count, 2)
        self.assertIsNone(self.sync.next_cursor)
        self.assertTrue(all(connection.logged_out for connection in connections))

    def test_failed_refresh_propagates_and_closes_old_session(self):
        self.refresh.side_effect = AuthorizationError("Microsoft authorization has expired.")
        old = FakeImapConnection(uid_error=imaplib.IMAP4.abort(EXPIRED), uid_error_command="fetch")
        _, messages = self.scan([old])
        with self.assertRaisesRegex(AuthorizationError, "authorization has expired"):
            list(messages)
        self.assertIsNone(self.sync.next_cursor)
        self.assertTrue(old.closed and old.logged_out)
        self.mailbox._connect.assert_called_once_with(self.account)

    def test_reauthentication_failure_closes_both_connections_without_leaking_token(self):
        old = FakeImapConnection(uid_error=imaplib.IMAP4.abort(EXPIRED), uid_error_command="fetch")
        new = FakeImapConnection(authenticate_error=imaplib.IMAP4.error("rejected new-token"))
        _, messages = self.scan([old, new])
        with self.assertRaisesRegex(MailboxError, "OAuth authentication failed") as error:
            list(messages)
        self.assertNotIn("new-token", str(error.exception))
        self.assertIsNone(self.sync.next_cursor)
        self.assertTrue(old.logged_out and new.logged_out)

    def test_unrelated_errors_do_not_refresh(self):
        for error in (imaplib.IMAP4.abort("connection closed"), OSError("socket closed")):
            with self.subTest(error=error):
                old = FakeImapConnection(uid_error=error, uid_error_command="fetch")
                _, messages = self.scan([old])
                with self.assertRaisesRegex(MailboxError, "closed"):
                    list(messages)
                self.refresh.assert_not_called()
                self.assertTrue(old.logged_out)

    def test_no_recovery_without_oauth_or_refresh_callback(self):
        for password in (None, "password"):
            with self.subTest(password=password):
                connection = FakeImapConnection(
                    uid_error=imaplib.IMAP4.abort(EXPIRED), uid_error_command="fetch"
                )
                self.mailbox._connect = Mock(return_value=connection)
                _, messages = self.mailbox.fetch_messages(
                    mail_target(self.account),
                    password,
                    access_token="old-token" if password is None else None,
                    refresh_access_token=None if password is None else self.refresh,
                )
                with self.assertRaisesRegex(MailboxError, "AccessTokenExpired"):
                    list(messages)
                self.refresh.assert_not_called()
                self.mailbox._connect.assert_called_once()

    def test_closing_iterator_after_refresh_closes_current_session_without_cursor(self):
        old = FakeImapConnection(
            uids=b"77 78", uid_error=imaplib.IMAP4.abort(EXPIRED), uid_error_command="fetch"
        )
        new = FakeImapConnection()
        _, messages = self.scan([old, new])
        self.assertEqual(next(messages).id, "77")
        messages.close()
        self.assertTrue(old.logged_out and new.logged_out)
        self.assertIsNone(self.sync.next_cursor)
