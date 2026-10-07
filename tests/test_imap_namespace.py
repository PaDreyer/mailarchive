"""IMAP folder and UIDVALIDITY identity boundaries."""

import unittest
from dataclasses import replace

from mailarchive.application.service import _message_key
from mailarchive.domain.configuration import Account, Mailbox, MailProvider
from mailarchive.domain.source_identity import MailTarget, folder_scope_key, imap_scope


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
        self.assertEqual(folder_scope_key(MailProvider.GENERIC_IMAP, "iNbOx"), "INBOX")
        self.assertEqual(folder_scope_key(MailProvider.GENERIC_IMAP, "Archive"), "Archive")
        self.assertEqual(folder_scope_key(MailProvider.GENERIC_IMAP, "archive"), "archive")

    def test_other_folder_case_and_inner_spaces_are_significant(self):
        self.assertNotEqual(
            self.scope(folder="Important  Mail"), self.scope(folder="Important Mail")
        )
        self.assertNotEqual(self.scope(folder="Invoices"), self.scope(folder="invoices"))

    def test_unicode_input_and_existing_wire_name_have_the_same_identity(self):
        self.assertEqual(self.scope(folder="Entwürfe"), self.scope(folder="Entw&APw-rfe"))

    def test_unicode_case_mapping_does_not_alias_the_special_inbox(self):
        self.assertNotEqual(self.scope(folder="ınbox"), self.scope(folder="INBOX"))
        self.assertEqual(self.scope(folder="ınbox"), self.scope(folder="&ATE-nbox"))
        self.assertEqual(folder_scope_key(MailProvider.GENERIC_IMAP, "ınbox"), "&ATE-nbox")

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
