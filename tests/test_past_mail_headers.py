"""Past-mail header preselection preserves matching and avoids MIME/spool work."""

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mailarchive.application.cancellation import Cancellation, ProcessingStopped
from mailarchive.application.source_port import RemoteMessageError, RemoteMessageUnavailable
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Condition,
    Mailbox,
    MailField,
    MailHeaders,
    MailProvider,
    MatchMode,
    MatchOperator,
    Rule,
    RuleTarget,
    SaveMode,
    Settings,
)
from mailarchive.domain.mail_parser import parse_header_pairs, parse_headers, parse_mail
from mailarchive.domain.rules import rule_matches, rule_may_match_headers
from mailarchive.infrastructure.activity_repository import SqliteActivityRepository
from mailarchive.infrastructure.providers.http import ProviderHttpError
from mailarchive.infrastructure.providers.imap_client import IMAP_HEADER_BYTES
from tests.helpers import mail_target, sample_mail
from tests.past_mail_fixture import CountingImap, provider_source
from tests.test_imap_client import FakeImapConnection, FakeImapMailbox
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service

PROVIDERS = (MailProvider.MICROSOFT_GRAPH, MailProvider.GMAIL_API, MailProvider.GENERIC_IMAP)


class HeaderMatchingTests(unittest.TestCase):
    def test_every_text_operator_uses_full_matching_normalization(self):
        raw = sample_mail(subject="Straße ÄÖ Invoice", sender="Näme <USER@example.org>")
        full = parse_mail(raw)
        headers = parse_headers(raw)
        self.assertEqual(headers.sender, full.sender)
        self.assertEqual(headers.recipients, full.recipients)
        self.assertEqual(headers.subject, full.subject)
        values = [
            "STRASSE",
            "strasse äö invoice",
            " Invoice ",
            "USER@example.org",
            "CUSTOMER",
            "ORG",
            "wrong",
            "  ",
        ]
        for field, operator, value in product(
            (MailField.SENDER, MailField.RECIPIENT, MailField.SUBJECT), MatchOperator, values
        ):
            with self.subTest(field=field, operator=operator, value=value):
                rule = Rule("Match", conditions=[Condition(field, operator, value)])
                self.assertEqual(
                    rule_may_match_headers(rule, headers, "owner"),
                    rule_matches(rule, full, "owner"),
                )

    def test_and_or_preserve_unknown_body_attachment_and_missing_headers(self):
        false = Condition(MailField.SENDER, MatchOperator.EQUALS, "other@example.org")
        true = Condition(MailField.SUBJECT, MatchOperator.CONTAINS, "invoice")
        headers = MailHeaders(sender="sender@example.org", subject="Invoice")
        for unknown in (MailField.BODY, MailField.HAS_ATTACHMENT, MailField.RECIPIENT):
            with self.subTest(unknown=unknown):
                for conditions, mode, expected in (
                    ([false, Condition(unknown, value="yes")], MatchMode.ALL, False),
                    ([false, Condition(unknown, value="yes")], MatchMode.ANY, True),
                    ([false, true], MatchMode.ALL, False),
                    ([false, true], MatchMode.ANY, True),
                    ([false, false], MatchMode.ANY, False),
                    ([true, Condition(unknown, value="yes")], MatchMode.ALL, True),
                    ([false, Condition(MailField.ALL)], MatchMode.ANY, True),
                    ([], MatchMode.ALL, True),
                ):
                    self.assertEqual(
                        rule_may_match_headers(
                            Rule("Match", conditions=conditions, match_mode=mode), headers, "owner"
                        ),
                        expected,
                    )
        rule = Rule("Scoped", account_ids=["another"])
        self.assertFalse(rule_may_match_headers(rule, headers, "owner"))
        rule.account_ids = None
        rule.enabled = False
        self.assertFalse(rule_may_match_headers(rule, headers, "owner"))

    def test_header_pairs_match_mime_parser_and_ambiguities_stay_unknown(self):
        raw = (
            b'From: "Delegate" <from@example.org>\r\nSender: actual@example.org\r\n'
            b"To: Recipient <recipient@example.org>\r\nCc: cc@example.org\r\n"
            b"Subject: =?utf-8?b?U3RyYcOfZQ==?=\r\n\r\nbody"
        )
        pairs = [
            ("From", '"Delegate" <from@example.org>'),
            ("Sender", "actual@example.org"),
            ("To", "Recipient <recipient@example.org>"),
            ("Cc", "cc@example.org"),
            ("Subject", "=?utf-8?b?U3RyYcOfZQ==?="),
        ]
        expected = parse_mail(raw)
        actual = parse_header_pairs(pairs)
        self.assertEqual(
            (actual.sender, actual.recipients, actual.subject),
            (expected.sender, expected.recipients, expected.subject),
        )
        self.assertIsNone(parse_headers(b"Subject: present\r\n\r\n").sender)
        self.assertIsNone(parse_headers(b"From: broken <address\r\n\r\n").sender)
        self.assertIsNone(
            parse_header_pairs([("From", "a@example.org"), ("From", "b@example.org")]).sender
        )
        self.assertEqual(
            parse_header_pairs([("Subject", "bad\r\nFrom: injected@example.org")]), MailHeaders()
        )
        self.assertEqual(
            parse_header_pairs([("Subject\nFrom", "injected@example.org")]), MailHeaders()
        )
        self.assertEqual(
            parse_headers(b"From: wrong@example.org\r\nSubject: unfinished"), MailHeaders()
        )
        folded = "=?utf-8?b?U3RyYcOfZQ==?=\r\n\tInvoice"
        self.assertEqual(
            parse_header_pairs([("Subject", folded)]).subject,
            parse_mail(f"Subject: {folded}\r\n\r\nBody".encode()).subject,
        )


class PastMailHeaderIntegrationTests(unittest.TestCase):
    def setup_provider(self, provider, messages):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = Account(
            "Owner",
            "imap.example.org",
            mailbox.address,
            provider=provider,
            auth_mode=AuthMode.PASSWORD
            if provider == MailProvider.GENERIC_IMAP
            else AuthMode.OAUTH_USER,
            client_id="test-client",
            mailboxes=[mailbox],
        )
        source, server = provider_source(provider, account, messages)
        state = WorkspaceStore(root / "profile" / "workspace.sqlite3")
        service = make_service(state, Registry(source))
        rule = Rule(
            "Selected",
            conditions=[Condition(MailField.SENDER, MatchOperator.EQUALS, "wanted@example.org")],
            targets=[RuleTarget(str(root / "archive"))],
        )
        settings = Settings(accounts=[account], rules=[rule])
        return root, account, state, service, server, settings, rule

    def test_thousand_unmatched_per_provider_never_download_or_create_spool_files(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                raw = sample_mail(sender="other@example.org")
                _, account, state, service, server, settings, rule = self.setup_provider(
                    provider, {str(uid): raw for uid in range(1, 1001)}
                )
                with patch.object(state.spool, "stage", wraps=state.spool.stage) as stage:
                    result = service.run_range(
                        settings, {account.mailboxes[0].id}, rule_id=rule.id
                    )[0]
                self.assertEqual((result.unmatched, result.archived, result.failed), (1000, 0, 0))
                stage.assert_not_called()
                self.assertEqual(list(state.spool_dir.iterdir()), [])
                with state.connection() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM intake WHERE status='unmatched'"
                        ).fetchone()[0],
                        1000,
                    )
                    metadata = db.execute(
                        "SELECT received_at, received_origin, sender_at, subject FROM source_message"
                    ).fetchall()
                self.assertTrue(
                    all(row[0] and row[1] and row[3] == "Monthly invoice" for row in metadata)
                )
                self.assertTrue(
                    all(
                        (row[2] is None) == (provider == MailProvider.MICROSOFT_GRAPH)
                        for row in metadata
                    )
                )
                if provider == MailProvider.GENERIC_IMAP:
                    fetches = [call for call in server.calls if call[0] == "fetch"]
                    self.assertEqual(len(fetches), 10)
                    self.assertTrue(
                        all(
                            len(call[1].split(b",")) <= 100 and "<0.65536>" in call[-1]
                            for call in fetches
                        )
                    )
                else:
                    self.assertEqual(server.downloads, [])
                    if provider == MailProvider.MICROSOFT_GRAPH:
                        self.assertEqual(len(server.requests), 1003)
                        membership_requests = [
                            url for url in server.requests if "?$select=parentFolderId" in url
                        ]
                        self.assertEqual(len(membership_requests), 1000)
                    else:
                        self.assertEqual(len(server.requests), 1002)
                        metadata_url = next(
                            url for url in server.requests if "format=metadata" in url
                        )
                        self.assertEqual(
                            parse_qs(urlsplit(metadata_url).query)["metadataHeaders"],
                            ["From", "To", "Cc", "Bcc", "Subject", "Date"],
                        )

    def test_optimized_archives_and_reused_outputs_equal_full_download_path(self):
        for provider, optimized in product(PROVIDERS, (False, True)):
            with self.subTest(provider=provider, optimized=optimized):
                messages = {
                    "1": sample_mail(
                        sender="wanted@example.org", attachments=[("bill.pdf", b"pdf")]
                    ),
                    "2": sample_mail(
                        sender="other@example.org", attachments=[("bill.pdf", b"pdf")]
                    ),
                    "3": sample_mail(sender="wanted@example.org", body="wrong body"),
                }
                root, account, state, service, _, settings, rule = self.setup_provider(
                    provider, messages
                )
                rule.conditions.append(
                    Condition(MailField.BODY, MatchOperator.CONTAINS, "attached")
                )
                rule.targets[0].save_mode = SaveMode.EMAIL_AND_ATTACHMENTS
                with (
                    patch.object(service, "_reject_from_headers", return_value=False)
                    if not optimized
                    else patch.object(
                        service, "_reject_from_headers", wraps=service._reject_from_headers
                    )
                ):
                    result = service.run_range(
                        settings, {account.mailboxes[0].id}, rule_id=rule.id
                    )[0]
                    self.assertEqual((result.archived, result.unmatched, result.failed), (1, 2, 0))
                    emls = list((root / "archive").rglob("*.eml"))
                    self.assertEqual(len(emls), 1)
                    self.assertEqual(emls[0].read_bytes(), messages["1"])
                    self.assertEqual(
                        [path.read_bytes() for path in (root / "archive").rglob("*.pdf")], [b"pdf"]
                    )
                    again = service.run_range(settings, {account.mailboxes[0].id}, rule_id=rule.id)[
                        0
                    ]
                    self.assertEqual(again.failed, 0)
                    self.assertEqual(len(list((root / "archive").rglob("*.eml"))), 1)
                with state.connection() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM output").fetchone()[0], 4)
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM output WHERE status='done'").fetchone()[0],
                        4,
                    )
                    self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 2)
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM output_attempt").fetchone()[0], 2
                    )
                activity = SqliteActivityRepository(state.connection)
                self.assertEqual(activity.history().items[0].previously_archived_outputs, 2)

    def test_graph_delegation_uses_from_and_old_list_metadata_falls_back_once(self):
        messages = {
            "1": b"Sender: delegate@example.org\n" + sample_mail(sender="wanted@example.org")
        }
        _, account, _, service, server, settings, rule = self.setup_provider(
            MailProvider.MICROSOFT_GRAPH, messages
        )
        server.omit_list_metadata = True
        server.metadata_override["1"] = {
            "sender": {"emailAddress": {"address": "delegate@example.org"}}
        }
        result = service.run_range(settings, {account.mailboxes[0].id}, rule_id=rule.id)[0]
        self.assertEqual(result.archived, 1)
        self.assertEqual(
            len([url for url in server.requests if "?$select=parentFolderId" in url]), 1
        )
        self.assertEqual(server.downloads, ["1"])

    def test_header_preselection_uses_frozen_selected_rule(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                _, account, state, service, server, settings, rule = self.setup_provider(
                    provider, {"1": sample_mail(sender="other@example.org")}
                )
                operation = service.prepare_range_operation(
                    settings, {account.mailboxes[0].id}, rule_id=rule.id
                )
                rule.conditions[0].value = "other@example.org"
                state.save_settings(settings)
                result = service.run_range_operation(operation)[0]
                self.assertEqual((result.unmatched, result.archived, result.failed), (1, 0, 0))
                if isinstance(server, CountingImap):
                    self.assertFalse(any("BODY.PEEK[]" in str(call[-1]) for call in server.calls))
                else:
                    self.assertEqual(server.downloads, [])

    def test_or_with_unknown_body_can_archive_even_when_sender_is_false(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                _, account, _, service, _, settings, rule = self.setup_provider(
                    provider, {"1": sample_mail(sender="other@example.org")}
                )
                rule.match_mode = MatchMode.ANY
                rule.conditions.append(Condition(MailField.BODY, value="attached"))
                result = service.run_range(settings, {account.mailboxes[0].id}, rule_id=rule.id)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))

    def test_provider_unauthorized_refreshes_oauth_without_starting_raw_download(self):
        for provider in PROVIDERS[:2]:
            with self.subTest(provider=provider):
                _, account, _, service, server, settings, rule = self.setup_provider(
                    provider, {"1": sample_mail(sender="other@example.org")}
                )
                get_json = server.get_json
                attempted = []

                def expire_once(
                    url,
                    token,
                    headers=None,
                    *,
                    cancellation=None,
                    attempted=attempted,
                    get_json=get_json,
                ):
                    attempted.append(token)
                    if len(attempted) == 1:
                        raise ProviderHttpError(401, "expired")
                    return get_json(url, token, headers, cancellation=cancellation)

                server.get_json = expire_once
                result = service.run_range(settings, {account.mailboxes[0].id}, rule_id=rule.id)[0]
                self.assertEqual((result.unmatched, result.failed), (1, 0))
                self.assertIn("refreshed-token", attempted[-1])
                self.assertEqual(server.downloads, [])

    def test_stop_after_metadata_prevents_download_and_next_reservation(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                _, account, _, service, server, _, _ = self.setup_provider(
                    provider, {"1": sample_mail(), "2": sample_mail()}
                )
                source = service.source_registry.get(account)
                stop = threading.Event()
                reserved = []
                _, messages = source.search_messages(
                    mail_target(account),
                    lambda _scope, uid, reserved=reserved: reserved.append(uid) or True,
                    None,
                    None,
                    cancellation=Cancellation(stop.is_set),
                )
                remote = next(messages)
                self.assertEqual(reserved, ["1"])
                stop.set()
                with self.assertRaises(ProcessingStopped):
                    list(remote.iter_raw())
                with self.assertRaises(ProcessingStopped):
                    next(messages)
                self.assertEqual(reserved, ["1"])
                remote.release_resources()
                if isinstance(server, CountingImap):
                    self.assertFalse(any("BODY.PEEK[]" in str(call[-1]) for call in server.calls))
                    self.assertTrue(server.closed and server.logged_out)
                else:
                    self.assertEqual(server.downloads, [])

    def test_imap_batch_duplicate_foreign_missing_and_short_headers_are_safe(self):
        good = b'1 (UID 77 RFC822.SIZE 100 INTERNALDATE "21-Sep-2026 00:00:00 +0000")'
        account = Account("Mail", "imap.example.org", "owner@example.org")
        for response in (
            [good, good],
            [good.replace(b"UID 77", b"UID 999")],
            [
                (
                    b'1 (UID 77 RFC822.SIZE 100 INTERNALDATE "21-Sep-2026 00:00:00 +0000" BODY[HEADER]<0> {4}',
                    b"From",
                ),
                b")",
            ],
        ):
            with self.subTest(response=response):
                connection = FakeImapConnection(fetch_response=response)
                result = FakeImapMailbox(connection)._metadata_batch(connection, [b"77"], "9001")[
                    b"77"
                ]
                if isinstance(response[0], tuple):
                    self.assertIsNone(result[2])
                else:
                    self.assertIsInstance(result, RemoteMessageError)
        source, server = provider_source(
            MailProvider.GENERIC_IMAP, account, {"77": sample_mail(), "78": sample_mail()}
        )
        _, messages = source.search_messages(mail_target(account), lambda *_args: True, None, None)
        del server.messages[b"77"]
        first, second = list(messages)
        self.assertIsInstance(first.error, RemoteMessageUnavailable)
        self.assertIsNone(second.error)
        self.assertIn(("search", None, "UID 77"), server.calls)

    def test_missing_or_malformed_header_metadata_falls_back_to_full_matching(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                raw = sample_mail(sender="wanted@example.org")
                _, account, _, service, server, settings, rule = self.setup_provider(
                    provider, {"1": raw}
                )
                if provider == MailProvider.MICROSOFT_GRAPH:
                    server.metadata_override["1"] = {
                        "from": {"emailAddress": {"address": "broken <address"}}
                    }
                elif provider == MailProvider.GMAIL_API:
                    server.metadata_override["1"] = {
                        "payload": {"headers": [{"name": "From", "value": "broken <address"}]}
                    }
                else:
                    # A bounded header ending halfway through a field stays unknown.
                    raw = b"X-Padding: " + b"x" * IMAP_HEADER_BYTES + b"\r\n" + raw
                    server.messages[b"1"] = raw
                result = service.run_range(settings, {account.mailboxes[0].id}, rule_id=rule.id)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))
                if isinstance(server, CountingImap):
                    self.assertEqual(
                        len([call for call in server.calls if "BODY.PEEK[]" in str(call[-1])]), 1
                    )
                else:
                    self.assertEqual(server.downloads, ["1"])

    def test_imap_attributes_after_literal_and_header_uid_text_are_not_confused(self):
        raw = b"From: from@example.org\r\nSubject: UID 999 RFC822.SIZE 999\r\n\r\n"
        connection = FakeImapConnection(
            fetch_response=[
                (b"1 (BODY[HEADER]<0> {" + str(len(raw)).encode() + b"}", raw),
                b' UID 77 RFC822.SIZE 100 INTERNALDATE "21-Sep-2026 00:00:00 +0000")',
            ]
        )
        metadata = FakeImapMailbox(connection)._metadata_batch(connection, [b"77"], "9001")
        received, size, headers = metadata[b"77"]
        self.assertEqual(received, datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual(size, 100)
        self.assertEqual(headers.sender, "from@example.org")

    def test_imap_header_larger_than_declared_size_cannot_reject_message(self):
        raw = b"From: other@example.org\r\n\r\n"
        connection = FakeImapConnection(
            fetch_response=[
                (
                    b'1 (UID 77 RFC822.SIZE 1 INTERNALDATE "21-Sep-2026 00:00:00 +0000" BODY[HEADER]<0> {'
                    + str(len(raw)).encode()
                    + b"}",
                    raw,
                ),
                b")",
            ]
        )
        metadata = FakeImapMailbox(connection)._metadata_batch(connection, [b"77"], "9001")
        self.assertIsNone(metadata[b"77"][2])

    def test_imap_reserves_only_current_intake_and_skipped_uids_need_no_metadata(self):
        account = Account("Mail", "imap.example.org", "owner@example.org")
        source, server = provider_source(
            MailProvider.GENERIC_IMAP, account, {str(uid): sample_mail() for uid in range(1, 101)}
        )
        reserved = []
        _, messages = source.search_messages(
            mail_target(account), lambda _scope, uid: reserved.append(uid) or True, None, None
        )
        self.assertEqual(next(messages).id, "1")
        self.assertEqual(reserved, ["1"])
        self.assertEqual(len([call for call in server.calls if call[0] == "fetch"]), 1)
        messages.close()
        self.assertTrue(server.closed and server.logged_out)
        server.calls.clear()
        _, skipped = source.search_messages(mail_target(account), lambda *_args: False, None, None)
        self.assertEqual(list(skipped), [])
        self.assertFalse(any(call[0] == "fetch" for call in server.calls))
