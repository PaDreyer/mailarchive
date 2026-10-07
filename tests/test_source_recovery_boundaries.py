"""Frozen discovery owners, IMAP generations and folder identities survive retries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from mailarchive.application.account_credentials import store_account_credentials
from mailarchive.application.synchronization import RangePagination
from mailarchive.domain.configuration import (
    Account,
    Mailbox,
    MailProvider,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.domain.source_identity import imap_folder_wire_name, legacy_imap_scope
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.http import ProviderHttpError
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests import test_provider_scope_regressions as membership_fixture
from tests.helpers import mail_target, sample_mail
from tests.test_imap_client import FakeImapConnection
from tests.test_mail_sources import FakeOAuth
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service


class MembershipTransitions(membership_fixture.MembershipServer):
    def __init__(self, provider):
        super().__init__(provider)
        self.fail_secondary = False
        self.moved = False

    def get_json(self, url, *args, **kwargs):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        secondary = "/mailFolders/STARRED/messages" in parsed.path or query.get("labelIds") == [
            "STARRED"
        ]
        if self.fail_secondary and secondary and not parsed.path.endswith("/delta"):
            raise ProviderHttpError(503, "Temporary second-folder listing failure")
        if self.moved and "/mailFolders/INBOX/messages/delta" in parsed.path:
            self.requests.append(url)
            return {
                "value": [{"id": "message", "@removed": {"reason": "changed"}}],
                "@odata.deltaLink": f"https://graph.microsoft.com{parsed.path}?$deltatoken=moved",
            }
        if self.moved and parsed.path.endswith("/history"):
            self.requests.append(url)
            return {
                "historyId": "101",
                "history": [
                    {"messagesAdded": [{"message": {"id": "message", "labelIds": ["INBOX"]}}]}
                ],
            }
        return super().get_json(url, *args, **kwargs)


class FrozenDiscoveryOwnerTests(unittest.TestCase):
    def provider(self, provider, folders):
        fixture = membership_fixture.ProviderScopeRegressionTests()
        self.addCleanup(fixture.doCleanups)
        root, account, settings, _server, _source, state, _service = fixture.setup_provider(
            provider, folders=folders
        )
        server = MembershipTransitions(provider)
        source_type = (
            GmailMessageSource
            if provider == MailProvider.GMAIL_API
            else MicrosoftGraphMessageSource
        )
        source = source_type(FakeOAuth(), server)
        return root, account, settings, server, state, make_service(state, Registry(source))

    def intake(self, state, intake_id):
        with state.connection() as db:
            return dict(db.execute("SELECT * FROM intake WHERE id=?", (intake_id,)).fetchone())

    def test_narrow_automatic_scan_preserves_broader_manual_retry_and_history(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            for reconcile in (False, True):
                with self.subTest(provider=provider, reconcile=reconcile):
                    root, account, settings, server, state, service = self.provider(
                        provider, ["STARRED", "INBOX"]
                    )
                    mailbox = account.mailboxes[0]
                    mailbox.archive_existing_messages = False
                    service.run_once(settings)
                    server.fail_secondary = True
                    server.body_error = ProviderHttpError(503, "Temporary MIME failure")
                    operation = service.prepare_range_operation(
                        settings, {mailbox.id}, rule_id=settings.rules[0].id
                    )
                    self.assertGreater(service.run_range_operation(operation)[0].failed, 0)
                    with state.connection() as db:
                        intake_id = db.execute(
                            "SELECT id FROM intake WHERE status='error'"
                        ).fetchone()[0]
                        first_attempt = tuple(
                            db.execute(
                                "SELECT * FROM manual_operation_attempt WHERE operation_id=?",
                                (operation,),
                            ).fetchone()
                        )
                    old_intake = self.intake(state, intake_id)
                    mailbox.folders = ["INBOX"]
                    state.save_settings(settings)
                    server.fail_secondary = False
                    server.moved = True
                    server.members = {"STARRED"}
                    if reconcile:
                        service.run_once(settings, force_retry=True)
                    self.assertEqual(self.intake(state, intake_id), old_intake)
                    result = service.run_range_operation(operation)[0]
                    self.assertEqual((result.archived, result.failed), (1, 0))
                    self.assertEqual(state.manual_operation(operation)["status"], "completed")
                    self.assertEqual(len(list((root / "archive").glob("*.eml"))), 1)
                    self.assertEqual(self.intake(state, intake_id)["status"], "accepted")
                    with state.connection() as db:
                        self.assertEqual(
                            tuple(
                                db.execute(
                                    "SELECT * FROM manual_operation_attempt WHERE operation_id=? AND number=1",
                                    (operation,),
                                ).fetchone()
                            ),
                            first_attempt,
                        )
                        self.assertEqual(
                            db.execute("SELECT count(*) FROM receipt").fetchone()[0], 1
                        )
                    self.assertEqual(state.spool_usage(), (0, 0))

    def test_narrow_scan_preserves_deferred_automatic_owner_and_its_original_rule(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            with self.subTest(provider=provider):
                root, account, settings, server, state, service = self.provider(
                    provider, ["STARRED", "INBOX"]
                )
                server.body_error = ProviderHttpError(503, "Temporary MIME failure")
                self.assertEqual(service.run_once(settings)[0].failed, 1)
                saved_intake = dict(state.pending_automatic_intakes()[0])
                account.mailboxes[0].folders = ["INBOX"]
                settings.rules[0].targets = [RuleTarget(str(root / "current"))]
                state.save_settings(settings)
                server.moved = True
                server.members = {"STARRED"}
                service.run_once(settings)
                self.assertEqual(dict(state.pending_automatic_intakes()[0]), saved_intake)
                result = service.run_once(settings, force_retry=True)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))
                self.assertEqual(state.pending_automatic_intakes(), [])
                self.assertEqual(len(list((root / "archive").glob("*.eml"))), 1)
                self.assertEqual(list((root / "current").glob("*.eml")), [])

    def test_same_automatic_owner_can_reconcile_outside_scope_within_backoff(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            with self.subTest(provider=provider):
                _root, _account, settings, server, state, service = self.provider(
                    provider, ["INBOX"]
                )
                server.body_error = ProviderHttpError(503, "Temporary MIME failure")
                self.assertEqual(service.run_once(settings)[0].failed, 1)
                intake_id = state.pending_automatic_intakes()[0]["id"]
                server.moved = True
                server.members = {"TRASH"}
                service.run_once(settings)
                self.assertEqual(state.pending_automatic_intakes(), [])
                intake = self.intake(state, intake_id)
                self.assertEqual(intake["status"], "filtered")
                self.assertIn("Temporary MIME failure", intake["error"])

    def test_automatic_owner_selection_order_does_not_block_reconciliation(self):
        for provider in (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH):
            with self.subTest(provider=provider):
                _root, account, settings, server, state, service = self.provider(
                    provider, ["INBOX", "STARRED"]
                )
                server.body_error = ProviderHttpError(503, "Temporary MIME failure")
                self.assertEqual(service.run_once(settings)[0].failed, 1)
                account.mailboxes[0].folders = ["STARRED", "INBOX"]
                state.save_settings(settings)
                if provider == MailProvider.GMAIL_API:
                    server.moved = True
                    server.members = {"TRASH"}
                else:

                    def move_after_inbox_listing(server=server):
                        if "/mailFolders/INBOX/messages" in server.requests[-1]:
                            server.members = {"TRASH"}

                    server.after_listing = move_after_inbox_listing
                service.run_once(settings)
                self.assertEqual(state.pending_automatic_intakes(), [])


class MutableImapConnection(FakeImapConnection):
    def __init__(self):
        super().__init__(uids=b"77", raw_by_uid={b"77": sample_mail()})
        self.generations = {"INBOX": b"9001", "Archive": b"9001"}
        self.inbox_uids = b"77"
        self.selected = "INBOX"
        self.fail_body = False
        self.fail_body_uid = None
        self.body_reads = []

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        self.uids = self.inbox_uids if self.selected == "INBOX" else b""
        return super().select(mailbox, readonly)

    def response(self, name):
        return name, [self.generations[self.selected]]

    def uid(self, command, *args):
        if command == "fetch" and str(args[-1]).startswith("(BODY.PEEK[]<"):
            self.body_reads.append((self.selected, args[0]))
            if self.fail_body and (self.fail_body_uid is None or args[0] == self.fail_body_uid):
                self.fail_body = False
                raise ConnectionResetError("Connection closed during MIME read")
        return super().uid(command, *args)


class ImapGenerationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mailbox = Mailbox("owner@example.org", ["INBOX", "Archive"])
        self.account = Account(
            "Owner", "imap.example.org", self.mailbox.address, mailboxes=[self.mailbox]
        )
        self.rule = Rule("All", targets=[RuleTarget(str(self.root / "archive"))])
        self.settings = Settings(accounts=[self.account], rules=[self.rule])
        credentials = MemoryCredentialStore()
        store_account_credentials(credentials, self.account, {"password": "fake"})
        self.server = MutableImapConnection()
        self.adapter = ImapMailbox()
        self.adapter._connect = Mock(return_value=self.server)
        self.source = ImapMessageSource(credentials, self.adapter)
        self.state = WorkspaceStore(self.root / "profile" / "workspace.sqlite3")
        self.service = make_service(self.state, Registry(self.source))
        self.service.run_once(self.settings)
        self.server.fail_body = True
        self.operation = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.assertEqual(self.service.run_range_operation(self.operation)[0].failed, 1)
        self.run = self.state.manual_run_for_source(self.operation, self.mailbox.id)["id"]
        self.checkpoint = self.state.range_target_checkpoint(self.run, "INBOX")
        self.healthy_scope = dict(self.state.scope(self.mailbox.id, "Archive"))

    def test_restarted_saved_range_pauses_generation_before_search_or_download(self):
        old_intake = dict(self.state.unresolved_intakes(self.run)[0])
        self.server.generations["INBOX"] = b"9002"
        self.server.raw_by_uid[b"77"] = sample_mail(subject="New generation")
        self.server.calls.clear()
        self.server.body_reads.clear()
        self.state = WorkspaceStore(self.state.database_path, recover=True)
        self.service = make_service(self.state, Registry(self.source))
        result = self.service.run_range_operation(self.operation)[0]
        self.assertEqual((result.archived, result.failed), (0, 1))
        self.assertIn("UIDVALIDITY", result.errors[0])
        self.assertEqual(self.server.body_reads, [])
        self.assertFalse(
            any(call[0] == "fetch" or call[:2] == ("uid", "search") for call in self.server.calls)
        )
        self.assertEqual(self.state.range_target_checkpoint(self.run, "INBOX"), self.checkpoint)
        self.assertEqual(dict(self.state.unresolved_intakes(self.run)[0]), old_intake)
        paused = self.state.scope(self.mailbox.id, "INBOX")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["processing_namespace"], self.checkpoint["namespace"])
        self.assertEqual(dict(self.state.scope(self.mailbox.id, "Archive")), self.healthy_scope)
        self.assertEqual(self.state.manual_operation(self.operation)["status"], "failed")
        self.assertEqual(list((self.root / "archive").glob("*.eml")), [])
        # A separate explicit operation acknowledges the live generation without
        # reinterpreting the old operation's UID or rewriting its checkpoint.
        fresh = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        self.assertEqual(self.service.run_range_operation(fresh)[0].archived, 1)
        self.assertEqual(self.state.manual_operation(fresh)["status"], "completed")
        self.assertEqual(self.state.range_target_checkpoint(self.run, "INBOX"), self.checkpoint)
        self.assertEqual(dict(self.state.unresolved_intakes(self.run)[0]), old_intake)
        self.assertEqual(len(list((self.root / "archive").glob("*.eml"))), 1)

    def test_invalid_uidvalidity_is_not_a_confirmed_generation_change(self):
        self.server.generations["INBOX"] = b"invalid"
        self.assertEqual(self.service.run_range_operation(self.operation)[0].failed, 1)
        self.assertEqual(self.state.scope(self.mailbox.id, "INBOX")["status"], "active")
        self.assertEqual(self.state.range_target_checkpoint(self.run, "INBOX"), self.checkpoint)

    def test_accepted_local_peer_finishes_before_saved_generation_retry_is_paused(self):
        obstruction = self.root / "offline"
        obstruction.write_text("Destination temporarily unavailable", encoding="utf-8")
        self.rule.targets = [
            RuleTarget(str(self.root / "healthy")),
            RuleTarget(str(obstruction / "archive")),
        ]
        self.state.save_settings(self.settings)
        self.server.inbox_uids = b"77 78 79"
        accepted_raw = sample_mail(subject="Old accepted message")
        self.server.raw_by_uid.update({b"78": accepted_raw, b"79": sample_mail()})
        self.server.fail_body = True
        self.server.fail_body_uid = b"79"
        operation = self.service.prepare_range_operation(
            self.settings, {self.mailbox.id}, rule_id=self.rule.id
        )
        first = self.service.run_range_operation(operation)[0]
        self.assertEqual((first.archived, first.failed), (1, 2))
        plan = self.state.operations.manual_open_plans(operation)[0]
        raw_path = Path(plan["raw_path"])
        with self.state.connection() as db:
            receipt = tuple(db.execute("SELECT * FROM receipt").fetchone())
        obstruction.unlink()
        self.server.generations["INBOX"] = b"9002"
        self.server.raw_by_uid = {
            uid: sample_mail(subject="New namespace message") for uid in (b"77", b"78", b"79")
        }
        self.server.calls.clear()
        self.server.body_reads.clear()
        result = self.service.run_range_operation(operation)[0]
        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.server.body_reads, [])
        self.assertFalse(any(call[0] == "fetch" for call in self.server.calls))
        self.assertEqual(self.state.scope(self.mailbox.id, "INBOX")["status"], "paused")
        self.assertEqual(self.state.manual_operation(operation)["status"], "failed")
        self.assertEqual(self.state.operations.manual_open_plans(operation), [])
        self.assertFalse(raw_path.exists())
        for folder in (self.root / "healthy", obstruction / "archive"):
            files = list(folder.glob("*.eml"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), accepted_raw)
        with self.state.connection() as db:
            self.assertEqual(
                tuple(
                    db.execute(
                        "SELECT * FROM receipt WHERE final_path=?", (receipt[-2],)
                    ).fetchone()
                ),
                receipt,
            )
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 2)

    def test_compatible_saved_legacy_range_namespace_is_retained(self):
        target = mail_target(self.account)
        namespace = legacy_imap_scope(target, "9001").processing_namespace
        pagination = RangePagination(namespace, None, lambda *args: True)
        _scope, messages = self.adapter.fetch_messages(
            target, "fake", lambda *_: False, range_sync=pagination
        )
        self.assertEqual(list(messages), [])
        self.assertEqual(pagination.namespace, namespace)
        self.assertTrue(pagination.complete)


class ImapFolderEncodingTests(unittest.TestCase):
    def test_wire_names_are_preserved_and_plain_names_encoded(self):
        cases = (
            ("Invoices & Reports", "Invoices &- Reports"),
            ("R&D", "R&-D"),
            ("A & B & C", "A &- B &- C"),
            ("&", "&-"),
            ("&APw", "&-APw"),
            ("&AGE-", "&-AGE-"),
            ("Archive & Entwürfe", "Archive &- Entw&APw-rfe"),
            ("Entw&APw-rfe", "Entw&APw-rfe"),
            ("&-", "&-"),
            ("~peter/mail/&U,BTFw-/&ZeVnLIqe-", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
        )
        for folder, expected in cases:
            with self.subTest(folder=folder):
                self.assertEqual(imap_folder_wire_name(folder), expected)
                self.assertEqual(imap_folder_wire_name(expected), expected)

    def test_public_folder_spelling_change_retains_cursor_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mailbox = Mailbox(
                "owner@example.org", ["Invoices & Reports"], archive_existing_messages=True
            )
            account = Account("Owner", "imap.example.org", mailbox.address, mailboxes=[mailbox])
            rule = Rule("All", targets=[RuleTarget(str(root / "archive"))])
            settings = Settings(accounts=[account], rules=[rule])
            credentials = MemoryCredentialStore()
            store_account_credentials(credentials, account, {"password": "fake"})
            server = FakeImapConnection()
            original_select = server.select

            def select(wire, readonly=False):
                self.assertEqual(wire, '"Invoices &- Reports"')
                return original_select(wire, readonly)

            server.select = select
            adapter = ImapMailbox()
            adapter._connect = Mock(return_value=server)
            source = ImapMessageSource(credentials, adapter)
            state = WorkspaceStore(root / "profile" / "workspace.sqlite3")
            state.save_settings(settings)
            service = make_service(state, Registry(source))
            self.assertEqual(service.run_once(settings)[0].archived, 1)
            before_scope = dict(state.scope(mailbox.id, "Invoices &- Reports"))
            with state.connection() as db:
                receipt = tuple(db.execute("SELECT * FROM receipt").fetchone())
            mailbox.folders = ["Invoices &- Reports"]
            state.save_settings(settings)
            self.assertEqual(dict(state.scope(mailbox.id, mailbox.folders[0])), before_scope)
            state = WorkspaceStore(state.database_path, recover=True)
            service = make_service(state, Registry(source))
            result = service.run_range(settings, {mailbox.id}, rule_id=rule.id)[0]
            self.assertEqual((result.archived, result.failed, result.already_processed), (0, 0, 1))
            self.assertEqual(len(list((root / "archive").glob("*.eml"))), 1)
            with state.connection() as db:
                self.assertEqual(tuple(db.execute("SELECT * FROM receipt").fetchone()), receipt)
                self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 1)
            self.assertEqual(state.spool_usage(), (0, 0))
