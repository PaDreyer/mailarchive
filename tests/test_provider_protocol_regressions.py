"""Live folder moves and structured FETCH attributes preserve archive intake."""

import json
import unittest
from unittest.mock import Mock

from mailarchive.application.account_credentials import store_account_credentials
from mailarchive.application.source_port import MailboxError, RemoteMessageError
from mailarchive.domain.configuration import MailProvider, Settings
from mailarchive.domain.source_identity import MailTarget, api_scope
from mailarchive.infrastructure.providers.http import ProviderHttpError
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests import test_execution_outcomes as execution_fixture
from tests import test_synchronization as sync_fixture
from tests.test_imap_client import FakeImapConnection
from tests.test_imap_protocol_regressions import parsed_response
from tests.test_restart_core import Registry, raw_mail


class GraphFolderMoveRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sync_fixture.SynchronizationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_move_between_selected_folders_archives_once_without_transient_intake_error(self):
        fixture = self.fixture
        fixture.configure(MailProvider.MICROSOFT_GRAPH, folders=["one", "two"])
        fixture.run_http(
            [
                sync_fixture.graph_delta("/messages/delta?", next_cursor="one-start", folder="one"),
                sync_fixture.graph_delta("/messages/delta?", next_cursor="two-start", folder="two"),
            ]
        )
        result, http = fixture.run_http(
            [
                sync_fixture.graph_delta(
                    "/one-start", ["moved"], next_cursor="one-next", folder="one"
                ),
                ("json", "/mailFolders/one?$select=id", {"id": "one-id"}),
                sync_fixture.graph_message("moved", folder="two-id"),
                ("json", "/mailFolders/two?$select=id", {"id": "two-id"}),
                sync_fixture.graph_raw("moved"),
                sync_fixture.graph_delta(
                    "/two-start", ["moved"], next_cursor="two-next", folder="two"
                ),
                ("json", "/mailFolders/two?$select=id", {"id": "two-id"}),
                sync_fixture.graph_message("moved", folder="two-id"),
            ]
        )
        self.assertEqual((result.archived, result.failed), (1, 0))
        self.assertEqual(fixture.state.intake_errors(), [])
        self.assertEqual(len(list((fixture.root / "Archive").glob("*.eml"))), 1)
        self.assertEqual(sum(kind == "bytes" for kind, *_ in http.calls), 1)

    def test_move_out_of_all_selected_folders_does_not_reserve_or_download(self):
        fixture = self.fixture
        fixture.configure(MailProvider.MICROSOFT_GRAPH, folders=["one", "two"])
        fixture.run_http(
            [
                sync_fixture.graph_delta("/messages/delta?", next_cursor="one-start", folder="one"),
                sync_fixture.graph_delta("/messages/delta?", next_cursor="two-start", folder="two"),
            ]
        )
        reserve = Mock(wraps=fixture.state.discovery.reserve)
        fixture.state.discovery.reserve = reserve
        result, _http = fixture.run_http(
            [
                sync_fixture.graph_delta(
                    "/one-start", ["moved"], next_cursor="one-next", folder="one"
                ),
                ("json", "/mailFolders/one?$select=id", {"id": "one-id"}),
                sync_fixture.graph_message("moved", folder="foreign-id"),
                ("json", "/mailFolders/two?$select=id", {"id": "two-id"}),
                sync_fixture.graph_delta("/two-start", next_cursor="two-next", folder="two"),
            ]
        )
        self.assertEqual((result.archived, result.failed), (0, 0))
        reserve.assert_not_called()
        self.assertEqual(fixture.state.intake_errors(), [])

    def reserve_for_targeted_retry(self, folders):
        fixture = self.fixture
        mailbox = fixture.configure(MailProvider.MICROSOFT_GRAPH, folders=["one", "two"])
        mailbox.folders = folders
        revision = fixture.state.prepare_run_settings(fixture.settings)
        run_id = fixture.state.start_run(
            mailbox.id, "automatic", {"folders": ["one"]}, fixture.settings, revision
        )
        scope = api_scope(MailTarget(fixture.account, mailbox, "one"))
        intake = fixture.state.reserve(
            mailbox.id,
            scope.processing_namespace + "\0reserved",
            run_id,
            automatic=True,
            scope_key="one",
            remote_id="reserved",
        )
        fixture.state.mark_intake_error(intake, "Earlier download failed")
        fixture.state.finish_run(run_id)
        fixture.settings.rules = []
        fixture.state.save_settings(fixture.settings)
        return intake

    def test_direct_saved_retry_releases_mail_outside_saved_folders_with_rule_removed(self):
        intake = self.reserve_for_targeted_retry(["one", "two"])
        result, http = self.fixture.run_http(
            [
                sync_fixture.graph_message("reserved", folder="foreign-id"),
                ("json", "/mailFolders/one?$select=id", {"id": "one-id"}),
                ("json", "/mailFolders/two?$select=id", {"id": "two-id"}),
            ],
            force_retry=True,
        )
        self.assertEqual((result.archived, result.failed), (0, 0))
        self.assertFalse(any(kind == "bytes" for kind, *_ in http.calls))
        self.assertEqual(self.fixture.state.pending_automatic_intakes(), [])
        with self.fixture.state.connection() as db:
            row = db.execute("SELECT status,error FROM intake WHERE id=?", (intake,)).fetchone()
        self.assertEqual((row["status"], row["error"]), ("filtered", "Earlier download failed"))

    def test_direct_saved_retry_keeps_mail_in_any_saved_selected_folder(self):
        for parent, folders, folder_steps in (
            (
                "one-id",
                ["one", "two"],
                [
                    ("json", "/mailFolders/one?$select=id", {"id": "one-id"}),
                ],
            ),
            (
                "two-id",
                ["one", "two"],
                [
                    ("json", "/mailFolders/one?$select=id", ProviderHttpError(404, "deleted")),
                    ("json", "/mailFolders/two?$select=id", {"id": "two-id"}),
                ],
            ),
            ("any-live-folder", [], []),
        ):
            with self.subTest(parent=parent):
                self.reserve_for_targeted_retry(folders)
                result, _http = self.fixture.run_http(
                    [
                        sync_fixture.graph_message("reserved", folder=parent),
                        *folder_steps,
                        sync_fixture.graph_raw("reserved"),
                    ],
                    force_retry=True,
                )
                self.assertEqual((result.archived, result.failed), (1, 0))
                self.assertEqual(self.fixture.state.pending_automatic_intakes(), [])
                # Each variant gets a fresh immutable provider identity.
                self.fixture.doCleanups()
                self.fixture.setUp()
                self.addCleanup(self.fixture.doCleanups)

    def test_released_old_scope_retry_does_not_hide_current_scope_discovery(self):
        fixture = self.fixture
        self.reserve_for_targeted_retry(["one"])
        # Restore the current enabled rule; the retry still owns its older snapshot.
        snapshot = fixture.state.pending_automatic_intakes()[0]["settings_json"]
        fixture.settings.rules = Settings.from_dict(json.loads(snapshot)).rules
        fixture.account.mailboxes[0].folders = ["two"]
        fixture.account.mailboxes[0].archive_existing_messages = True
        fixture.state.save_settings(fixture.settings)
        result, _http = fixture.run_http(
            [
                sync_fixture.graph_message("reserved", folder="two-id"),
                ("json", "/mailFolders/one?$select=id", {"id": "one-id"}),
                sync_fixture.graph_delta(
                    "/messages/delta?", ["reserved"], next_cursor="two-next", folder="two"
                ),
                ("json", "/mailFolders/two?$select=id", {"id": "two-id"}),
                sync_fixture.graph_message("reserved", folder="two-id"),
                sync_fixture.graph_raw("reserved"),
            ],
            force_retry=True,
        )
        self.assertEqual((result.archived, result.failed), (1, 0))
        self.assertEqual(fixture.state.pending_automatic_intakes(), [])
        self.assertEqual(len(list((fixture.root / "Archive").glob("*.eml"))), 1)

    def test_direct_saved_retry_404_preserves_retryable_missing_message(self):
        self.reserve_for_targeted_retry(["one", "two"])
        result, _http = self.fixture.run_http(
            [
                (
                    "json",
                    "/messages/reserved?$select=parentFolderId,receivedDateTime",
                    ProviderHttpError(404, "missing"),
                ),
            ],
            force_retry=True,
        )
        self.assertEqual((result.archived, result.failed), (0, 1))
        self.assertEqual(len(self.fixture.state.pending_automatic_intakes()), 1)


class StructuredFetchRegressionTests(unittest.TestCase):
    def test_combined_keyword_flags_archive_from_public_check_boundary(self):
        case = execution_fixture.ExecutionOutcomeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        account = case.app.settings.accounts[0]
        store_account_credentials(case.app._credentials, account, {"password": "fake-password"})
        connection = FakeImapConnection(
            uids=b"1", validity_data=[b"1"], raw_by_uid={b"1": raw_mail()}
        )
        uid = connection.uid

        def flagged(command, *arguments):
            status, response = uid(command, *arguments)
            if command == "fetch":
                response = [
                    b" FLAGS (UID 77 RFC822.SIZE 999)" + item
                    if isinstance(item, bytes) and item == b")"
                    else item
                    for item in response
                ]
            return status, response

        connection.uid = flagged
        mailbox = ImapMailbox()
        mailbox._connect = Mock(return_value=connection)
        case.app._context.execution.service.source_registry = Registry(
            ImapMessageSource(case.app._credentials, mailbox)
        )
        self.assertEqual(case.check().state.value, "completed")
        output = next((case.root / "archive").glob("*.eml"))
        self.assertEqual(output.read_bytes(), raw_mail())
        self.assertTrue(connection.logged_out)

    def test_stock_parser_flags_do_not_supply_uid_size_or_literal_syntax(self):
        mailbox = ImapMailbox()
        metadata = parsed_response(
            b"* 1 FETCH (FLAGS (UID 77 RFC822.SIZE 999) UID 77 RFC822.SIZE 5 "
            b'INTERNALDATE "21-Sep-2026 00:00:00 +0000")\r\n',
            "FETCH",
        )
        result = mailbox._metadata_batch(
            FakeImapConnection(fetch_response=metadata), [b"77"], "9001"
        )
        self.assertEqual(result[b"77"][1], 5)
        body = parsed_response(
            b"* 1 FETCH (BODY[]<0> {5}\r\nhello FLAGS (UID 77) UID 77)\r\n", "FETCH"
        )
        self.assertEqual(
            mailbox._message_chunk(FakeImapConnection(fetch_response=body), b"77", "9001", 0, 5),
            b"hello",
        )

    def test_malformed_duplicate_and_conflicting_fetch_fields_are_still_rejected(self):
        mailbox = ImapMailbox()
        for trailer in (
            b" FLAGS NIL UID 77)",
            b" FLAGS ((UID 77)) UID 77)",
            b" FLAGS (UID 77) FLAGS (\\Seen) UID 77)",
            b" FLAGS (UID 77) UID 77 UID 78)",
            b' FLAGS ("UID 77") UID 77)',
        ):
            with self.subTest(trailer=trailer):
                body = parsed_response(
                    b"* 1 FETCH (BODY[]<0> {5}\r\nhello" + trailer + b"\r\n", "FETCH"
                )
                with self.assertRaises(MailboxError):
                    mailbox._message_chunk(
                        FakeImapConnection(fetch_response=body), b"77", "9001", 0, 5
                    )
        only_uid = [b"1 (UID 77)"]
        result = mailbox._metadata_batch(
            FakeImapConnection(fetch_response=only_uid), [b"77"], "9001"
        )
        self.assertIsInstance(result[b"77"], RemoteMessageError)
