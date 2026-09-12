import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from mailarchive.account_form import AccountFormValues, build_account_submission
from mailarchive.credential_data import update_credential_data
from mailarchive.credentials import MemoryCredentialStore
from mailarchive.dialogs import MailboxDialog
from mailarchive.mail_identity import MailTarget, api_scope, imap_scope, mailbox_namespace
from mailarchive.mail_sources import MessageSourceRegistry, ProviderHttpError
from mailarchive.models import Account, AuthMode, Mailbox, MailProvider, Rule, Settings
from mailarchive.oauth import MICROSOFT_MAIL_READ_SHARED_SCOPE, OAuthManager
from mailarchive.service import ArchiveService
from mailarchive.storage import ArchiveState
from tests.helpers import sample_mail
from tests.test_app import FakeTree, FakeVariable, make_account_dialog
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_mail_sources import FakeOAuth
from tests.test_oauth import FakeServiceAccountCredentials
from tests.test_synchronization import ScriptedHttp, graph_delta


class MailboxArchitectureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = ArchiveState(self.root / "state.db")
        self.credentials = MemoryCredentialStore()
        self.events = []
        self.oauth = FakeOAuth()

    def account(self, mailboxes, *, provider=MailProvider.MICROSOFT_GRAPH, application=False):
        return Account(
            "Connection",
            username="owner@example.org",
            provider=provider,
            auth_mode=AuthMode.OAUTH_APPLICATION if application else AuthMode.OAUTH_USER,
            client_id="client",
            tenant_id="tenant",
            mailboxes=mailboxes,
        )

    def run_http(self, account, steps, *, rules=None):
        http = ScriptedHttp(steps)
        registry = MessageSourceRegistry(self.credentials, http=http)
        registry.sources[account.provider].oauth = self.oauth
        settings = Settings(
            str(self.root / "archive"), accounts=[account], rules=rules or [Rule("All")]
        )
        result = ArchiveService(
            self.credentials, self.state, self.events.append, source_registry=registry
        ).run_once(settings)[0]
        self.assertEqual(http.steps, [], [event.message for event in self.events])
        return result

    @staticmethod
    def graph_download(root, folder, message, *, subject="Invoice"):
        return [
            ("json", f"{root}/mailFolders/{folder}?$select=id", {"id": folder}),
            (
                "json",
                f"{root}/messages/{message}?$select=parentFolderId",
                {"parentFolderId": folder},
            ),
            (
                "bytes",
                f"{root}/mailFolders/{folder}/messages/{message}/$value",
                sample_mail(subject=subject),
            ),
        ]

    def test_connection_roundtrip_has_independent_mailbox_preferences(self):
        account = self.account(
            [
                Mailbox("owner@example.org", ["inbox"]),
                Mailbox("team@example.org", ["inbox", "sentitems"], True),
            ]
        )
        account.validate()
        encoded = account.to_dict()
        self.assertNotIn("folder", encoded)
        self.assertNotIn("archive_existing_messages", encoded)
        self.assertEqual(Account.from_dict(encoded), account)
        self.assertFalse(account.mailboxes[0].archive_existing_messages)
        self.assertTrue(account.mailboxes[1].archive_existing_messages)

    def test_application_connection_needs_mailboxes_but_no_sign_in_username(self):
        account = self.account([Mailbox("team@example.org")], application=True)
        account.username = ""
        account.validate()
        submission = build_account_submission(
            AccountFormValues(
                "App",
                MailProvider.MICROSOFT_GRAPH,
                AuthMode.OAUTH_APPLICATION,
                "",
                client_id="client",
                tenant_id="tenant",
                secret="secret",
                mailboxes=account.mailboxes,
            )
        )
        self.assertEqual(submission.account.username, "")
        self.assertEqual(submission.account.mailboxes[0].address, "team@example.org")

    def test_mailboxes_cannot_be_empty_or_repeat_addresses(self):
        with self.assertRaisesRegex(ValueError, "at least one mailbox"):
            Account.from_dict(
                self.account([Mailbox("owner@example.org")]).to_dict() | {"mailboxes": []}
            )
        with self.assertRaisesRegex(ValueError, "only once"):
            self.account([Mailbox("team@example.org"), Mailbox("TEAM@example.org")]).validate()
        with self.assertRaisesRegex(ValueError, "at least one mailbox"):
            build_account_submission(
                AccountFormValues(
                    "Account",
                    MailProvider.GENERIC_IMAP,
                    AuthMode.PASSWORD,
                    "user",
                    host="imap.example.org",
                    secret="secret",
                    mailboxes=[],
                )
            )

    def test_restricted_sign_in_methods_reject_another_mailbox(self):
        for provider, auth in (
            (MailProvider.GENERIC_IMAP, AuthMode.PASSWORD),
            (MailProvider.GMAIL_API, AuthMode.OAUTH_USER),
        ):
            with (
                self.subTest(provider=provider),
                self.assertRaisesRegex(ValueError, "own mailbox only"),
            ):
                Account(
                    "Connection",
                    "imap.example.org",
                    "owner@example.org",
                    provider=provider,
                    auth_mode=auth,
                    client_id="client",
                    mailboxes=[Mailbox("team@example.org")],
                ).validate()

    def test_mailbox_changes_keep_existing_connection_credentials(self):
        for provider in (MailProvider.MICROSOFT_GRAPH, MailProvider.GMAIL_API):
            account = self.account(
                [Mailbox("owner@example.org")], provider=provider, application=True
            )
            updated = build_account_submission(
                AccountFormValues(
                    account.label,
                    provider,
                    account.auth_mode,
                    account.username,
                    client_id=account.client_id,
                    tenant_id=account.tenant_id,
                    mailboxes=[Mailbox("team@example.org"), Mailbox("sales@example.org")],
                ),
                existing=account,
            )
            self.assertFalse(updated.replace_credentials)
            self.assertEqual(updated.credential_updates, {})
            self.assertEqual(updated.account.id, account.id)

    def test_graph_shared_targets_use_users_endpoint_and_same_connection(self):
        account = self.account(
            [
                Mailbox("owner@example.org", ["inbox"], True),
                Mailbox("team@example.org", ["inbox"], True),
            ]
        )
        result = self.run_http(
            account,
            [
                graph_delta(
                    "/me/mailFolders/inbox/messages/delta?", ["same-id"], next_cursor="own-delta"
                ),
                *self.graph_download("/me", "inbox", "same-id", subject="Personal"),
                graph_delta(
                    "/users/team%40example.org/mailFolders/inbox/messages/delta?",
                    ["same-id"],
                    next_cursor="team-delta",
                ),
                *self.graph_download(
                    "/users/team%40example.org", "inbox", "same-id", subject="Team"
                ),
            ],
        )
        self.assertEqual((result.archived, result.failed), (2, 0))
        self.assertTrue(all(item.id == account.id for item in self.oauth.microsoft_accounts))
        second = self.run_http(
            account,
            [
                graph_delta("/v1.0/own-delta", next_cursor="own-d2"),
                graph_delta("/v1.0/team-delta", next_cursor="team-d2"),
            ],
        )
        self.assertEqual((second.checked, second.skipped, second.failed), (0, 0, 0))
        with closing(sqlite3.connect(self.state.database_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(DISTINCT source_namespace) FROM processed_message"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM synchronization_checkpoint").fetchone()[0], 2
            )

    def test_graph_folder_move_preserves_processing_identity(self):
        account = self.account([Mailbox("owner@example.org", ["inbox", "sentitems"], True)])
        first = self.run_http(
            account,
            [
                graph_delta("/mailFolders/inbox/messages/delta?", ["id"], next_cursor="inbox1"),
                *self.graph_download("/me", "inbox", "id"),
                graph_delta("/mailFolders/sentitems/messages/delta?", next_cursor="sent1"),
            ],
        )
        self.assertEqual(first.archived, 1)
        tombstone = {
            "value": [{"id": "id", "@removed": {"reason": "changed"}}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/inbox2",
        }
        moved = self.run_http(
            account,
            [
                ("json", "/v1.0/inbox1", tombstone),
                graph_delta("/v1.0/sent1", ["id"], next_cursor="sent2"),
            ],
        )
        self.assertEqual((moved.archived, moved.already_processed, moved.failed), (0, 1, 0))
        scopes = [
            api_scope(MailTarget(account, account.mailboxes[0], folder))
            for folder in account.mailboxes[0].folders
        ]
        self.assertEqual(scopes[0].processing_namespace, scopes[1].processing_namespace)
        self.assertNotEqual(
            scopes[0].synchronization_namespace, scopes[1].synchronization_namespace
        )

    def test_graph_baseline_backfill_finds_mail_moved_to_another_watched_folder(self):
        mailbox = Mailbox("owner@example.org", ["inbox", "sentitems"])
        account = self.account([mailbox])
        baseline = self.run_http(
            account,
            [
                graph_delta("/mailFolders/inbox/messages/delta?", ["old"], next_cursor="inbox1"),
                graph_delta("/mailFolders/sentitems/messages/delta?", next_cursor="sent1"),
            ],
        )
        self.assertEqual(baseline.skipped_existing, 1)
        mailbox.archive_existing_messages = True
        result = self.run_http(
            account,
            [
                graph_delta("/v1.0/inbox1", next_cursor="inbox2"),
                ("json", "/me/mailFolders/inbox?$select=id", {"id": "inbox"}),
                (
                    "json",
                    "/me/messages/old?$select=parentFolderId",
                    {"parentFolderId": "sentitems"},
                ),
                ("json", "/me/mailFolders/sentitems?$select=id", {"id": "sentitems"}),
                ("bytes", "/me/messages/old/$value", sample_mail()),
                graph_delta("/v1.0/sent1", next_cursor="sent2"),
            ],
        )
        self.assertEqual((result.archived, result.failed), (1, 0))

    def test_one_failed_mailbox_does_not_stop_the_other(self):
        account = self.account(
            [
                Mailbox("broken@example.org", ["inbox"], True),
                Mailbox("team@example.org", ["inbox"], True),
            ]
        )
        result = self.run_http(
            account,
            [
                (
                    "json",
                    "/users/broken%40example.org/mailFolders/inbox/messages/delta?",
                    ProviderHttpError(403, "access denied"),
                ),
                graph_delta(
                    "/users/team%40example.org/mailFolders/inbox/messages/delta?",
                    ["new"],
                    next_cursor="team1",
                ),
                *self.graph_download("/users/team%40example.org", "inbox", "new"),
            ],
        )
        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertFalse(
            self.state.has_completed_initial_scan(
                account.id, mailbox_namespace(account, account.mailboxes[0])
            )
        )
        self.assertTrue(
            self.state.has_completed_initial_scan(
                account.id, mailbox_namespace(account, account.mailboxes[1])
            )
        )

    def test_partial_folder_baseline_remains_initial_until_all_folders_succeed(self):
        mailbox = Mailbox("owner@example.org", ["inbox", "sentitems"])
        account = self.account([mailbox])
        first = self.run_http(
            account,
            [
                graph_delta("/mailFolders/inbox/messages/delta?", ["old1"], next_cursor="inbox1"),
                (
                    "json",
                    "/mailFolders/sentitems/messages/delta?",
                    ProviderHttpError(503, "offline"),
                ),
            ],
        )
        self.assertEqual((first.skipped_existing, first.failed), (1, 1))
        self.assertFalse(
            self.state.has_completed_initial_scan(account.id, mailbox_namespace(account, mailbox))
        )
        second = self.run_http(
            account,
            [
                graph_delta("/v1.0/inbox1", next_cursor="inbox2"),
                graph_delta(
                    "/mailFolders/sentitems/messages/delta?", ["old2"], next_cursor="sent1"
                ),
            ],
        )
        self.assertEqual((second.skipped_existing, second.archived, second.failed), (1, 0, 0))
        self.assertTrue(
            self.state.has_completed_initial_scan(account.id, mailbox_namespace(account, mailbox))
        )

    def test_graph_discovers_nested_folders_and_pagination_for_whole_mailbox(self):
        account = self.account([Mailbox("team@example.org")])
        steps = [
            (
                "json",
                "/users/team%40example.org/mailFolders?",
                {
                    "value": [{"id": "inbox", "childFolderCount": 1}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/folders-page2",
                },
            ),
            (
                "json",
                "/mailFolders/inbox/childFolders?",
                {"value": [{"id": "nested", "childFolderCount": 0}]},
            ),
            (
                "json",
                "/v1.0/folders-page2",
                {
                    "value": [
                        {"id": "hidden", "childFolderCount": 0},
                        {"id": "search", "@odata.type": "#microsoft.graph.mailSearchFolder"},
                    ]
                },
            ),
            graph_delta("/mailFolders/inbox/messages/delta?", ["1"], next_cursor="inbox1"),
            graph_delta("/mailFolders/nested/messages/delta?", ["2"], next_cursor="nested1"),
            graph_delta("/mailFolders/hidden/messages/delta?", ["3"], next_cursor="hidden1"),
        ]
        result = self.run_http(account, steps)
        self.assertEqual((result.skipped_existing, result.failed), (3, 0))

    def test_graph_rejects_untrusted_folder_continuation(self):
        account = self.account([Mailbox("owner@example.org")])
        result = self.run_http(
            account,
            [
                (
                    "json",
                    "/me/mailFolders?",
                    {"value": [], "@odata.nextLink": "https://attacker.example/folders"},
                )
            ],
        )
        self.assertEqual(result.failed, 1)
        self.assertTrue(
            any("invalid folder continuation" in event.message for event in self.events)
        )

    def test_gmail_selected_labels_are_a_union_with_one_mailbox_cursor(self):
        account = self.account(
            [Mailbox("owner@example.org", ["INBOX", "STARRED"], True)],
            provider=MailProvider.GMAIL_API,
        )
        encoded = base64.urlsafe_b64encode(sample_mail()).decode()
        result = self.run_http(
            account,
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "id"}]}),
                (
                    "json",
                    "/messages/id?format=raw",
                    {"raw": encoded, "labelIds": ["INBOX", "STARRED"]},
                ),
                ("json", "/messages?labelIds=STARRED", {"messages": [{"id": "id"}]}),
            ],
        )
        self.assertEqual((result.archived, result.checked, result.failed), (1, 1, 0))
        with closing(sqlite3.connect(self.state.database_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM synchronization_checkpoint").fetchone()[0], 1
            )

    def test_gmail_domain_delegation_impersonates_each_mailbox_under_one_connection(self):
        account = self.account(
            [Mailbox("one@example.org"), Mailbox("two@example.org")],
            provider=MailProvider.GMAIL_API,
            application=True,
        )
        steps = []
        for cursor in ("100", "200"):
            steps += [
                ("json", "/profile?fields=historyId", {"historyId": cursor}),
                ("json", "/messages?maxResults=500&includeSpamTrash=true", {"messages": []}),
            ]
        result = self.run_http(account, steps)
        self.assertEqual(result.failed, 0)
        self.assertEqual(
            self.oauth.google_subjects,
            [(account.id, "one@example.org"), (account.id, "two@example.org")],
        )
        with closing(sqlite3.connect(self.state.database_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM synchronization_checkpoint").fetchone()[0], 2
            )

    def test_gmail_filter_change_reconciles_without_reusing_previous_history_cursor(self):
        mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = self.account([mailbox], provider=MailProvider.GMAIL_API)
        self.run_http(
            account,
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "labelIds=INBOX", {"messages": []}),
            ],
        )
        mailbox.folders.append("STARRED")
        result = self.run_http(
            account,
            [
                ("json", "/profile?fields=historyId", {"historyId": "200"}),
                ("json", "labelIds=INBOX", {"messages": []}),
                ("json", "labelIds=STARRED", {"messages": [{"id": "new"}]}),
                (
                    "json",
                    "/messages/new?format=raw",
                    {
                        "raw": base64.urlsafe_b64encode(sample_mail()).decode(),
                        "labelIds": ["STARRED"],
                    },
                ),
            ],
        )
        self.assertEqual((result.archived, result.skipped_existing, result.failed), (1, 0, 0))

    def test_same_mailbox_processing_is_shared_across_different_connections(self):
        mailbox = Mailbox("team@example.org", ["inbox"], True)
        first = self.account([mailbox])
        self.assertEqual(
            self.run_http(
                first,
                [
                    graph_delta("/messages/delta?", ["id"], next_cursor="one"),
                    *self.graph_download("/users/team%40example.org", "inbox", "id"),
                ],
            ).archived,
            1,
        )
        second = self.account([mailbox], application=True)
        result = self.run_http(second, [graph_delta("/messages/delta?", ["id"], next_cursor="two")])
        self.assertEqual((result.archived, result.already_processed), (0, 1))

    def test_imap_discovers_selectable_folders_and_uses_one_stored_password(self):
        connection = FakeImapConnection()
        connection.list = MagicMock(
            return_value=(
                "OK",
                [
                    b'(\\HasNoChildren) "/" "INBOX"',
                    b'(\\Noselect) "/" "Parent"',
                    b'(\\HasNoChildren) "/" "Sent Items"',
                    (b'(\\HasNoChildren) "/" {8}', b"Invoices"),
                ],
            )
        )
        account = Account(
            "IMAP",
            "imap.example.org",
            "owner@example.org",
            mailboxes=[Mailbox("owner@example.org")],
        )
        self.credentials.set(account.id, "secret")
        registry = MessageSourceRegistry(self.credentials, imap_mailbox=FakeImapMailbox(connection))
        result = ArchiveService(self.credentials, self.state, source_registry=registry).run_once(
            Settings(str(self.root / "archive"), accounts=[account], rules=[Rule("All")])
        )[0]
        self.assertEqual((result.skipped_existing, result.failed), (3, 0))
        self.assertIn(("select", '"Sent Items"', True), connection.calls)
        self.assertEqual(
            [item[2] for item in connection.calls if item[0] == "login"], ["secret"] * 4
        )

    def test_imap_shared_mailbox_uses_target_for_xoauth2_and_login_for_token(self):
        account = Account(
            "IMAP",
            "outlook.office365.com",
            "owner@example.org",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("team@example.org", ["INBOX"])],
        )
        account.validate()
        connection = FakeImapConnection()
        registry = MessageSourceRegistry(self.credentials, imap_mailbox=FakeImapMailbox(connection))
        registry.sources[account.provider].oauth = self.oauth
        result = ArchiveService(self.credentials, self.state, source_registry=registry).run_once(
            Settings(str(self.root / "archive"), accounts=[account], rules=[Rule("All")])
        )[0]
        self.assertEqual(result.failed, 0)
        authentication = next(item for item in connection.calls if item[0] == "authenticate")
        self.assertIn(b"user=team@example.org", authentication[2][0])
        self.assertEqual(self.oauth.microsoft_accounts, [account])
        target = MailTarget(account, account.mailboxes[0], "INBOX")
        another_login = replace(account, username="other@example.org")
        self.assertEqual(
            imap_scope(target, "42"),
            imap_scope(MailTarget(another_login, account.mailboxes[0], "INBOX"), "42"),
        )

    def test_graph_shared_read_scope_is_requested_for_additional_addresses(self):
        account = self.account([Mailbox("owner@example.org"), Mailbox("team@example.org")])
        manager = OAuthManager(self.credentials)
        self.assertIn(
            MICROSOFT_MAIL_READ_SHARED_SCOPE, manager._microsoft_delegated_scopes(account)
        )
        account.mailboxes[1].enabled = False
        self.assertNotIn(
            MICROSOFT_MAIL_READ_SHARED_SCOPE, manager._microsoft_delegated_scopes(account)
        )

    def test_old_settings_and_api_history_migrate_without_duplicate_archive(self):
        for provider, legacy in (
            (MailProvider.MICROSOFT_GRAPH, "microsoft-graph:INBOX"),
            (MailProvider.GMAIL_API, "gmail-api:INBOX"),
        ):
            with self.subTest(provider=provider):
                account = Account.from_dict(
                    {
                        "id": provider.value,
                        "label": "Old",
                        "provider": provider.value,
                        "auth_mode": "oauth_user",
                        "username": "owner@example.org",
                        "folder": "INBOX",
                        "archive_existing_messages": True,
                    }
                )
                with closing(sqlite3.connect(self.state.database_path)) as db, db:
                    db.execute(
                        "INSERT INTO processed_message VALUES (?, ?, 'id', '2026', 'Old', 'All', '/archive', '[]')",
                        (account.id, legacy),
                    )
                self.state.complete_initial_scan(account.id, legacy, set())
                if provider == MailProvider.MICROSOFT_GRAPH:
                    steps = [graph_delta("/messages/delta?", ["id"], next_cursor="new")]
                else:
                    steps = [
                        ("json", "/profile?fields=historyId", {"historyId": "100"}),
                        ("json", "labelIds=INBOX", {"messages": [{"id": "id"}]}),
                    ]
                result = self.run_http(account, steps)
                self.assertEqual(
                    (result.archived, result.already_processed, result.failed), (0, 1, 0)
                )
                self.assertTrue(
                    self.state.has_completed_initial_scan(
                        account.id, mailbox_namespace(account, account.mailboxes[0])
                    )
                )

    def test_history_upgrade_is_once_and_not_assigned_to_a_different_mailbox(self):
        account = Account.from_dict(
            {
                "id": "old",
                "label": "Old",
                "provider": "microsoft_graph",
                "auth_mode": "oauth_user",
                "username": "owner@example.org",
                "folder": "inbox",
            }
        )
        self.state.complete_initial_scan(account.id, "microsoft-graph:inbox", {"old"})
        self.state.record_unmatched(account.id, "microsoft-graph:inbox", "unmatched", "before")
        original = account.mailboxes[0]
        account.mailboxes.insert(0, Mailbox("team@example.org"))
        self.state.upgrade_mailbox_history(account)
        namespace = mailbox_namespace(account, original)
        self.assertEqual(
            self.state.processed_message_ids(account.id, namespace, include_skipped=True), {"old"}
        )
        self.assertEqual(
            self.state.processed_message_ids(
                account.id, mailbox_namespace(account, account.mailboxes[0]), include_skipped=True
            ),
            set(),
        )
        self.state.record_unmatched(account.id, namespace, "unmatched", "after")
        self.state.upgrade_mailbox_history(Account.from_dict(account.to_dict()))
        self.assertEqual(
            self.state.unmatched_message_ids(account.id, namespace, "after"), {"unmatched"}
        )
        account.mailboxes = [Mailbox("different@example.org")]
        self.state.upgrade_mailbox_history(account)
        self.assertFalse(
            self.state.has_completed_initial_scan(
                account.id, mailbox_namespace(account, account.mailboxes[0])
            )
        )

    def test_imap_v2_history_is_adopted_into_mailbox_folder_epoch_identity(self):
        account = Account.from_dict(
            {
                "id": "old-imap",
                "label": "IMAP",
                "host": "imap.example.org",
                "username": "owner@example.org",
                "folder": "INBOX",
            }
        )
        legacy = "imap-v2:" + json.dumps(
            [account.host, account.port, account.username, "INBOX", "9001"], separators=(",", ":")
        )
        self.state.complete_initial_scan(account.id, legacy, {"1"})
        self.state.record_unmatched(account.id, legacy, "2", "rules")
        self.state.upgrade_mailbox_history(account)
        target = imap_scope(
            MailTarget(account, account.mailboxes[0], "INBOX"), "9001"
        ).processing_namespace
        self.assertEqual(
            self.state.processed_message_ids(account.id, target, include_skipped=True), {"1"}
        )
        self.assertEqual(self.state.unmatched_message_ids(account.id, target, "rules"), {"2"})

    def test_mailbox_dialog_validates_and_saves_independent_settings(self):
        dialog = object.__new__(MailboxDialog)
        dialog.address = FakeVariable(" team@example.org ")
        dialog.folders = MagicMock()
        dialog.folders.get.return_value = "inbox\n invoices \n"
        dialog.existing = FakeVariable(True)
        dialog.enabled = FakeVariable(False)
        dialog.destroy = MagicMock()
        dialog._save()
        self.assertEqual(
            dialog.result, Mailbox("team@example.org", ["inbox", "invoices"], True, False)
        )
        dialog.destroy.assert_called_once()

    def test_account_dialog_add_edit_remove_mailboxes(self):
        dialog = make_account_dialog()
        dialog.mailboxes_tree = FakeTree()
        dialog.wait_window = MagicMock()
        dialog.grab_set = MagicMock()
        with patch("mailarchive.dialogs.MailboxDialog") as editor:
            editor.return_value.result = Mailbox("team@example.org")
            dialog._add_mailbox()
            self.assertEqual(len(dialog.mailboxes), 2)
            self.assertEqual(dialog.mailboxes_tree.rows[1]["values"][1], "All folders")
            dialog.mailboxes_tree.selected = ("1",)
            editor.return_value.result = Mailbox("sales@example.org", ["inbox"])
            dialog._edit_mailbox()
            self.assertEqual(dialog.mailboxes[1].address, "sales@example.org")
            dialog._remove_mailbox()
            self.assertEqual(len(dialog.mailboxes), 1)

    def test_mailbox_disabled_skips_only_that_target(self):
        account = self.account(
            [Mailbox("broken@example.org", enabled=False), Mailbox("team@example.org", ["inbox"])]
        )
        result = self.run_http(
            account,
            [
                graph_delta(
                    "/users/team%40example.org/mailFolders/inbox/messages/delta?",
                    next_cursor="team1",
                )
            ],
        )
        self.assertEqual(result.failed, 0)
        self.assertEqual(self.oauth.microsoft_accounts, [account])

    def test_google_application_tokens_use_explicit_subject_instead_of_login_identity(self):
        account = self.account(
            [Mailbox("one@example.org"), Mailbox("two@example.org")],
            provider=MailProvider.GMAIL_API,
            application=True,
        )
        account.username = ""
        account.validate()
        update_credential_data(
            self.credentials, account.id, google_service_account={"type": "service_account"}
        )
        issued = []

        def factory(info, scopes):
            credentials = FakeServiceAccountCredentials()
            issued.append(credentials)
            return credentials

        manager = OAuthManager(
            self.credentials,
            google_service_account_factory=factory,
            google_request_factory=lambda: object(),
        )
        for mailbox in account.mailboxes:
            self.assertEqual(
                manager.google_access_token(account, mailbox_address=mailbox.address),
                "service-account-token",
            )
        self.assertEqual(
            [credentials.subject for credentials in issued], ["one@example.org", "two@example.org"]
        )

    def test_graph_mailbox_recheck_outside_selected_folders_is_suppressed_once(self):
        mailbox = Mailbox("owner@example.org", ["inbox", "sentitems"])
        account = self.account([mailbox])
        self.run_http(
            account,
            [
                graph_delta("/mailFolders/inbox/messages/delta?", ["old"], next_cursor="inbox1"),
                graph_delta("/mailFolders/sentitems/messages/delta?", next_cursor="sent1"),
            ],
        )
        mailbox.archive_existing_messages = True
        result = self.run_http(
            account,
            [
                graph_delta("/v1.0/inbox1", next_cursor="inbox2"),
                ("json", "/mailFolders/inbox?$select=id", {"id": "inbox"}),
                ("json", "/messages/old?$select=parentFolderId", {"parentFolderId": "trash"}),
                ("json", "/mailFolders/sentitems?$select=id", {"id": "sentitems"}),
                graph_delta("/v1.0/sent1", next_cursor="sent2"),
            ],
        )
        self.assertEqual((result.archived, result.failed), (0, 0))
        next_check = self.run_http(
            account,
            [
                graph_delta("/v1.0/inbox2", next_cursor="inbox3"),
                graph_delta("/v1.0/sent2", next_cursor="sent3"),
            ],
        )
        self.assertEqual((next_check.checked, next_check.failed), (0, 0))

    def test_missing_folder_hierarchy_metadata_does_not_complete_baseline(self):
        account = self.account([Mailbox("owner@example.org")])
        result = self.run_http(
            account, [("json", "/me/mailFolders?", {"value": [{"id": "inbox"}]})]
        )
        self.assertEqual(result.failed, 1)
        self.assertFalse(
            self.state.has_completed_initial_scan(
                account.id, mailbox_namespace(account, account.mailboxes[0])
            )
        )

    def test_history_adoption_does_not_duplicate_recent_archive_entries(self):
        account = Account.from_dict(
            {
                "id": "old",
                "label": "Old",
                "provider": "microsoft_graph",
                "auth_mode": "oauth_user",
                "username": "owner@example.org",
                "folder": "inbox",
            }
        )
        with closing(sqlite3.connect(self.state.database_path)) as db, db:
            db.execute(
                "INSERT INTO processed_message VALUES (?, 'microsoft-graph:inbox', 'id', '2026', 'Archived once', 'All', '/archive', '[]')",
                (account.id,),
            )
        self.state.upgrade_mailbox_history(account)
        self.assertEqual(len(self.state.recent()), 1)
        self.assertEqual(self.state.recent()[0]["subject"], "Archived once")

    def test_adoption_markers_survive_copy_and_merge_without_restoring_old_rule_history(self):
        account = Account.from_dict(
            {
                "id": "old",
                "label": "Old",
                "provider": "microsoft_graph",
                "auth_mode": "oauth_user",
                "username": "owner@example.org",
                "folder": "inbox",
            }
        )
        self.state.record_unmatched(account.id, "microsoft-graph:inbox", "id", "old-rules")
        self.state.upgrade_mailbox_history(account)
        namespace = mailbox_namespace(account, account.mailboxes[0])
        self.state.record_unmatched(account.id, namespace, "id", "current-rules")
        copied = self.state.migrated_to(self.root / "copied.db")
        destination = self.root / "merged.db"
        ArchiveState(destination)
        merged = copied.migrated_to(destination)
        for state in (copied, merged):
            state.upgrade_mailbox_history(account)
            self.assertEqual(
                state.unmatched_message_ids(account.id, namespace, "current-rules"), {"id"}
            )
            self.assertEqual(state.unmatched_message_ids(account.id, namespace, "old-rules"), set())
