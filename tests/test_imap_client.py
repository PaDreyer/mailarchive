import unittest
from unittest.mock import Mock, patch

from mailarchive.imap_client import ImapMailbox, MailboxError
from mailarchive.models import Account, AuthMode
from tests.helpers import sample_mail


class FakeImapConnection:
    def __init__(
        self,
        *,
        select_status="OK",
        search_status="OK",
        uids=b"77",
        fetch_status="OK",
        fetch_response=None,
        validity_data=None,
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
        self.validity_data = [b"9001"] if validity_data is None else validity_data
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
        return "UIDVALIDITY", self.validity_data

    def uid(self, command, *arguments):
        self.calls.append(("uid", command, *arguments))
        if self.uid_error is not None and (
            self.uid_error_command is None or self.uid_error_command == command
        ):
            raise self.uid_error
        if command == "search":
            return self.search_status, [self.uids]
        response = self.fetch_response
        if response is None:
            response = [(b"77 (BODY[] {100})", sample_mail())]
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
    def test_selects_readonly_and_fetches_without_seen_flag(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")
        validity, messages = mailbox.fetch_messages(account, "secret")
        fetched = list(messages)

        self.assertEqual(validity, "9001")
        self.assertEqual(fetched[0].id, "77")
        self.assertIn(("select", "INBOX", True), connection.calls)
        self.assertIn(("uid", "fetch", b"77", "(BODY.PEEK[])"), connection.calls)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_skips_body_fetch_for_a_known_uid(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")

        _, messages = mailbox.fetch_messages(
            account,
            "secret",
            lambda uid_validity, uid: False,
        )

        self.assertEqual(list(messages), [])
        self.assertIn(("uid", "search", None, "ALL"), connection.calls)
        self.assertNotIn(("uid", "fetch", b"77", "(BODY.PEEK[])"), connection.calls)

    def test_failed_login_is_mapped_and_logs_out(self) -> None:
        connection = FakeImapConnection(login_error=OSError("connection reset"))
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")

        with self.assertRaisesRegex(MailboxError, "connection reset"):
            mailbox.fetch_messages(account, "secret")

        self.assertTrue(connection.logged_out)
        self.assertFalse(connection.closed)

    def test_xoauth2_sends_raw_bearer_payload_once_without_password_login(self) -> None:
        connection = FakeImapConnection(authenticate_challenges=2)
        account = Account(
            "Outlook",
            "outlook.office365.com",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
        )

        validity, messages = FakeImapMailbox(connection).fetch_messages(
            account,
            None,
            access_token="access-token",
        )

        self.assertEqual(validity, "9001")
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
        )

        with self.assertRaisesRegex(MailboxError, "does not support OAuth"):
            FakeImapMailbox(connection).fetch_messages(
                account,
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
        )

        with self.assertRaises(MailboxError) as raised:
            FakeImapMailbox(connection).fetch_messages(
                account,
                None,
                access_token="access-token",
            )

        self.assertIn("IMAP OAuth authentication failed", str(raised.exception))
        self.assertIn("Reauthorize", str(raised.exception))
        self.assertNotIn("access-token", str(raised.exception))
        self.assertTrue(connection.logged_out)

    def test_rejects_ambiguous_or_missing_authentication_credentials(self) -> None:
        account = Account("Personal", "imap.example.org", "me@example.org")
        mailbox = FakeImapMailbox(FakeImapConnection())

        with self.assertRaisesRegex(MailboxError, "cannot be used together"):
            mailbox.fetch_messages(account, "password", access_token="access-token")
        with self.assertRaisesRegex(MailboxError, "credentials are missing"):
            mailbox.fetch_messages(account, None)

    def test_rejects_oauth_before_connecting_when_endpoint_is_not_trusted(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account(
            "Outlook",
            "imap.attacker.example",
            "me@example.org",
            auth_mode=AuthMode.OAUTH_USER,
        )

        with self.assertRaisesRegex(MailboxError, "outlook.office365.com"):
            mailbox.fetch_messages(account, None, access_token="access-token")

        self.assertEqual(connection.calls, [])

    def test_select_and_search_failures_are_clear_and_log_out(self) -> None:
        account = Account("Personal", "imap.example.org", "me@example.org")
        scenarios = (
            (FakeImapConnection(select_status="NO"), "Could not open mailbox 'INBOX'"),
            (FakeImapConnection(search_status="NO"), "Could not load the message list"),
        )
        for connection, message in scenarios:
            with self.subTest(message=message):
                with self.assertRaisesRegex(MailboxError, message):
                    FakeImapMailbox(connection).fetch_messages(account, "secret")
                self.assertTrue(connection.logged_out)

    def test_missing_uid_validity_and_empty_search_are_supported(self) -> None:
        connection = FakeImapConnection(validity_data=[], uids=b"")
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")

        validity, messages = mailbox.fetch_messages(account, "secret")

        self.assertEqual(validity, "unknown")
        self.assertEqual(list(messages), [])
        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_fetch_failure_and_empty_body_raise_and_cleanup(self) -> None:
        account = Account("Personal", "imap.example.org", "me@example.org")
        scenarios = (
            (FakeImapConnection(fetch_status="NO"), "Could not load message 77"),
            (
                FakeImapConnection(fetch_response=[b"metadata without a body"]),
                "Message 77 was empty",
            ),
        )
        for connection, message in scenarios:
            with self.subTest(message=message):
                _, messages = FakeImapMailbox(connection).fetch_messages(account, "secret")
                with self.assertRaisesRegex(MailboxError, message):
                    list(messages)
                self.assertTrue(connection.closed)
                self.assertTrue(connection.logged_out)

    def test_transport_failure_during_fetch_is_mapped_and_cleanup_errors_are_ignored(self) -> None:
        connection = FakeImapConnection(
            uid_error=OSError("socket closed"),
            uid_error_command="fetch",
            close_error=RuntimeError("already closed"),
            logout_error=RuntimeError("already logged out"),
        )
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")
        _, messages = mailbox.fetch_messages(account, "secret")

        with self.assertRaisesRegex(MailboxError, "socket closed"):
            list(messages)

        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)

    def test_closing_partially_consumed_iterator_releases_connection(self) -> None:
        connection = FakeImapConnection(uids=b"77 78")
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")
        _, messages = mailbox.fetch_messages(account, "secret")

        self.assertEqual(next(messages).id, "77")
        messages.close()

        self.assertTrue(connection.closed)
        self.assertTrue(connection.logged_out)
        fetch_calls = [call for call in connection.calls if call[:2] == ("uid", "fetch")]
        self.assertEqual(len(fetch_calls), 1)

    def test_filter_failure_still_releases_connection(self) -> None:
        connection = FakeImapConnection()
        mailbox = FakeImapMailbox(connection)
        account = Account("Personal", "imap.example.org", "me@example.org")

        def failing_filter(_validity, _uid):
            raise RuntimeError("filter failed")

        _, messages = mailbox.fetch_messages(account, "secret", failing_filter)
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
            ssl_account = Account("SSL", "secure.example.org", "me@example.org", port=993)
            plain_account = Account(
                "STARTTLS",
                "plain.example.org",
                "me@example.org",
                port=143,
                use_ssl=False,
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
