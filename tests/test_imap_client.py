import unittest

from mailarchive.imap_client import ImapMailbox
from mailarchive.models import Account
from tests.helpers import sample_mail


class FakeImapConnection:
    def __init__(self) -> None:
        self.calls = []
        self.closed = False
        self.logged_out = False

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, name):
        return "UIDVALIDITY", [b"9001"]

    def uid(self, command, *arguments):
        self.calls.append(("uid", command, *arguments))
        if command == "search":
            return "OK", [b"77"]
        return "OK", [(b"77 (BODY[] {100})", sample_mail())]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


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


if __name__ == "__main__":
    unittest.main()
