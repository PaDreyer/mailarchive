"""Connection ownership, mailbox scopes, and folder-name preservation."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mailarchive.dialogs import MailboxDialog
from mailarchive.mail_identity import MailTarget, api_scope, imap_scope, mailbox_namespace
from mailarchive.mail_sources import GmailMessageSource, MicrosoftGraphMessageSource
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider
from mailarchive.service import _selected_targets
from tests.test_mail_sources import FakeOAuth


class MailboxArchitectureTests(unittest.TestCase):
    def test_account_round_trip_preserves_multiple_source_ids_and_folders(self):
        account = Account(
            "Work",
            "outlook.office365.com",
            "login@example.org",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[
                Mailbox("login@example.org", ["INBOX", "A  B"]),
                Mailbox("shared@example.org", ["Shared  Mail"], enabled=False),
            ],
        )
        restored = Account.from_dict(account.to_dict())
        self.assertEqual(restored, account)
        self.assertEqual(restored.mailboxes[0].folders[1], "A  B")
        self.assertNotEqual(restored.mailboxes[0].id, restored.mailboxes[1].id)

    def test_application_connection_can_address_multiple_mailboxes_without_user_login(self):
        account = Account(
            "Graph",
            username="",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            client_id="client",
            tenant_id="tenant",
            mailboxes=[
                Mailbox("one@example.org", ["inbox"]),
                Mailbox("two@example.org", ["inbox"]),
            ],
        )
        account.validate()
        self.assertEqual(len(account.mailboxes), 2)

    def test_password_imap_and_user_gmail_cannot_read_another_mailbox(self):
        for provider, mode in (
            (MailProvider.GENERIC_IMAP, AuthMode.PASSWORD),
            (MailProvider.GMAIL_API, AuthMode.OAUTH_USER),
        ):
            with self.subTest(provider=provider):
                account = Account(
                    "Restricted",
                    "imap.example.org",
                    "one@example.org",
                    provider=provider,
                    auth_mode=mode,
                    client_id="client",
                    mailboxes=[
                        Mailbox("one@example.org", ["INBOX"]),
                        Mailbox("two@example.org", ["INBOX"]),
                    ],
                )
                with self.assertRaisesRegex(ValueError, "own mailbox"):
                    account.validate()

    def test_graph_folder_move_keeps_mailbox_message_identity(self):
        mailbox = Mailbox("owner@example.org", ["first", "second"])
        account = Account(
            "Graph",
            username=mailbox.address,
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[mailbox],
        )
        first = api_scope(MailTarget(account, mailbox, "first"))
        second = api_scope(MailTarget(account, mailbox, "second"))
        self.assertEqual(first.processing_namespace, second.processing_namespace)
        self.assertNotEqual(first.synchronization_namespace, second.synchronization_namespace)

    def test_graph_distinguishes_different_mailboxes_under_one_connection(self):
        first = Mailbox("first@example.org", ["inbox"])
        second = Mailbox("second@example.org", ["inbox"])
        account = Account(
            "Graph",
            username="first@example.org",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[first, second],
        )
        source = MicrosoftGraphMessageSource(FakeOAuth())
        first_target = source.targets(account, first)[0]
        second_target = source.targets(account, second)[0]
        self.assertNotEqual(mailbox_namespace(account, first), mailbox_namespace(account, second))
        self.assertEqual(source._mailbox_root(first_target), "/me")
        self.assertEqual(source._mailbox_root(second_target), "/users/second%40example.org")

    def test_gmail_labels_form_one_message_scope_and_manual_subset(self):
        mailbox = Mailbox("owner@example.org", ["First  Label", "Second Label"])
        account = Account(
            "Gmail",
            username=mailbox.address,
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client",
            mailboxes=[mailbox],
        )
        targets = GmailMessageSource(FakeOAuth()).targets(account, mailbox)
        self.assertEqual(len(targets), 1)
        selected = _selected_targets(account, mailbox, targets, {"Second Label"})
        self.assertEqual(selected[0].selected_folders, ("Second Label",))
        self.assertEqual(
            api_scope(selected[0]).processing_namespace, api_scope(targets[0]).processing_namespace
        )

    def test_imap_folder_and_epoch_both_affect_identity(self):
        mailbox = Mailbox("owner@example.org", ["INBOX", "Other  Folder"])
        account = Account("IMAP", "imap.example.org", mailbox.address, mailboxes=[mailbox])
        inbox = imap_scope(MailTarget(account, mailbox, "INBOX"), "42")
        other = imap_scope(MailTarget(account, mailbox, "Other  Folder"), "42")
        renewed = imap_scope(MailTarget(account, mailbox, "INBOX"), "43")
        self.assertNotEqual(inbox.processing_namespace, other.processing_namespace)
        self.assertNotEqual(inbox.processing_namespace, renewed.processing_namespace)

    def test_mailbox_dialog_saves_inner_and_edge_folder_spaces_verbatim(self):
        dialog = object.__new__(MailboxDialog)
        dialog.address = SimpleNamespace(get=lambda: "owner@example.org")
        dialog.folders = SimpleNamespace(get=lambda *_: " First  Label \nSecond Label\n")
        dialog.enabled = SimpleNamespace(get=lambda: True)
        dialog.original_mailbox = None
        dialog.result = None
        dialog.destroy = Mock()
        dialog._save()
        self.assertEqual(dialog.result.folders, [" First  Label ", "Second Label"])
        dialog.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
