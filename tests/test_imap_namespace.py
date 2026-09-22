"""IMAP folder and UIDVALIDITY identity boundaries."""

import unittest
from dataclasses import replace

from mailarchive.mail_identity import MailTarget, imap_scope
from mailarchive.models import Account, Mailbox
from mailarchive.service import _message_key


class ImapNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mailbox = Mailbox("me@example.org", folders=["INBOX", "Important  Mail"])
        self.account = Account(
            "Mail", "IMAP.example.org", "me@example.org", mailboxes=[self.mailbox]
        )

    def scope(self, account=None, mailbox=None, folder="INBOX", validity="42"):
        return imap_scope(
            MailTarget(account or self.account, mailbox or self.mailbox, folder), validity
        )

    def test_inbox_and_host_case_are_normalized(self):
        self.assertEqual(
            self.scope(),
            self.scope(account=replace(self.account, host="imap.example.org"), folder="inbox"),
        )

    def test_other_folder_case_and_inner_spaces_are_significant(self):
        self.assertNotEqual(
            self.scope(folder="Important  Mail"), self.scope(folder="Important Mail")
        )
        self.assertNotEqual(self.scope(folder="Invoices"), self.scope(folder="invoices"))

    def test_uidvalidity_changes_identity_without_guessing_from_content(self):
        before = self.scope(validity="42")
        after = self.scope(validity="43")
        self.assertNotEqual(before, after)
        self.assertNotEqual(
            _message_key(self.account, before, "7"), _message_key(self.account, after, "7")
        )

    def test_host_or_mailbox_change_creates_another_identity(self):
        different_host = replace(self.account, host="other.example.org")
        different_mailbox = Mailbox("other@example.org", folders=["INBOX"])
        self.assertNotEqual(self.scope(), self.scope(account=different_host))
        self.assertNotEqual(self.scope(), self.scope(mailbox=different_mailbox))


if __name__ == "__main__":
    unittest.main()
