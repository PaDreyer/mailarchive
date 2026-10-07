"""API membership checks precede reservations across public processing paths."""

import base64
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from mailarchive.application.account_status import AuthorizationState, AuthorizationStatus
from mailarchive.application.events import ExecutionState
from mailarchive.application.source_port import RemoteMessageOutsideScope
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Condition,
    Mailbox,
    MailField,
    MailProvider,
    MatchOperator,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.domain.source_identity import MailTarget
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.http import ProviderHttpError
from tests.concurrency import THREAD_TIMEOUT
from tests.helpers import sample_mail
from tests.test_mail_sources import FakeOAuth
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service

PROVIDERS = (MailProvider.GMAIL_API, MailProvider.MICROSOFT_GRAPH)


class MembershipServer:
    """Move a real listed identity before current metadata is requested."""

    def __init__(self, provider, raw=None):
        self.provider = provider
        self.raw = sample_mail() if raw is None else raw
        self.members = {"INBOX"}
        self.after_listing = lambda: None
        self.requests = []
        self.downloads = []
        self.metadata_error = None
        self.body_error = None
        self.subject = "Invoice"
        self.enumerated_folders = ["INBOX"]

    def get_json(self, url, _token, headers=None, *, cancellation=None):
        if cancellation:
            cancellation.checkpoint()
        self.requests.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/profile"):
            return {"historyId": "100"}
        if parsed.path.endswith("/history"):
            return {"historyId": "101", "history": []}
        folder = re.search(r"/mailFolders/([^/]+)$", parsed.path)
        if folder:
            name = unquote(folder[1])
            return {"id": ("INBOX" if name.lower() == "inbox" else name) + "-id"}
        if parsed.path.endswith("/mailFolders"):
            return {
                "value": [
                    {"id": folder, "childFolderCount": 0} for folder in self.enumerated_folders
                ]
            }
        message = re.search(r"/messages/([^/]+)$", parsed.path)
        if message and message[1] != "delta":
            if self.metadata_error is not None:
                raise self.metadata_error
            metadata = {
                "internalDate": "1789948800000",
                "labelIds": sorted(self.members),
                "receivedDateTime": "2026-09-21T00:00:00Z",
                "parentFolderId": sorted(self.members)[0] + "-id" if self.members else "outside-id",
                "subject": self.subject,
            }
            if query.get("format") == ["raw"]:
                self.downloads.append("message")
                metadata["raw"] = base64.urlsafe_b64encode(self.raw).decode()
            return metadata
        selected = query.get("labelIds", [None])[0]
        if selected is None and self.provider == MailProvider.MICROSOFT_GRAPH:
            match = re.search(r"/mailFolders/([^/]+)/messages", parsed.path)
            selected = unquote(match[1]) if match else None
            if selected is not None and selected.lower() == "inbox":
                selected = "INBOX"
        listed = selected is None or selected in self.members
        self.after_listing()
        if self.provider == MailProvider.GMAIL_API:
            return {"messages": [{"id": "message"}] if listed else []}
        result = {
            "value": [{"id": "message", "receivedDateTime": "2026-09-21T00:00:00Z"}]
            if listed
            else []
        }
        if parsed.path.endswith("/delta"):
            result["@odata.deltaLink"] = (
                f"https://graph.microsoft.com{parsed.path}?$deltatoken=next"
            )
        return result

    def iter_bytes(self, url, _token, headers=None, *, cancellation=None, **_kwargs):
        if cancellation:
            cancellation.checkpoint()
        self.requests.append(url)
        self.downloads.append("message")
        if self.body_error is not None:
            error, self.body_error = self.body_error, None
            raise error
        yield self.raw

    def iter_gmail_raw(self, url, token, headers=None, *, cancellation=None):
        yield from self.iter_bytes(url, token, headers, cancellation=cancellation)


class NonStreamingServer:
    def __init__(self, server):
        self.server = server

    def get_json(self, *args, **kwargs):
        return self.server.get_json(*args, **kwargs)

    def get_bytes(self, url, token, headers=None, **kwargs):
        return b"".join(self.server.iter_bytes(url, token, headers, **kwargs))


class ProviderScopeRegressionTests(unittest.TestCase):
    def setup_provider(self, provider, *, folders=None, raw=None, streaming=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mailbox = Mailbox(
            "owner@example.org",
            ["INBOX"] if folders is None else folders,
            archive_existing_messages=True,
        )
        account = Account(
            "Owner",
            username=mailbox.address,
            provider=provider,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="fake-client",
            mailboxes=[mailbox],
        )
        rule = Rule("All", targets=[RuleTarget(str(root / "archive"))])
        settings = Settings(
            accounts=[account], rules=[rule], start_at_login=False, automatic_monitoring_paused=True
        )
        server = MembershipServer(provider, raw)
        transport = server if streaming else NonStreamingServer(server)
        source = (
            GmailMessageSource
            if provider == MailProvider.GMAIL_API
            else MicrosoftGraphMessageSource
        )(FakeOAuth(), transport)
        state = WorkspaceStore(root / "profile" / "workspace.sqlite3")
        service = make_service(state, Registry(source))
        return root, account, settings, server, source, state, service

    def test_manual_move_outside_is_not_reserved_or_downloaded_in_either_transport(self):
        for provider in PROVIDERS:
            for streaming in (True, False):
                with self.subTest(provider=provider, streaming=streaming):
                    root, account, settings, server, _, state, service = self.setup_provider(
                        provider, streaming=streaming
                    )
                    server.after_listing = lambda server=server: setattr(
                        server, "members", {"TRASH"}
                    )
                    reserve = Mock(wraps=state.discovery.reserve)
                    state.discovery.reserve = reserve
                    result = service.run_range(settings, {account.mailboxes[0].id})[0]
                    self.assertEqual((result.archived, result.failed), (0, 0))
                    reserve.assert_not_called()
                    self.assertEqual(server.downloads, [])
                    self.assertEqual(list((root / "archive").glob("*.eml")), [])

    def test_automatic_discovery_and_expanded_manual_all_folders_check_current_membership(self):
        for provider in PROVIDERS:
            for automatic, folders in ((True, ["INBOX"]), (False, [])):
                with self.subTest(provider=provider, automatic=automatic):
                    _, account, settings, server, _, state, service = self.setup_provider(
                        provider, folders=folders
                    )
                    server.after_listing = lambda server=server: setattr(
                        server, "members", {"TRASH"}
                    )
                    reserve = Mock(wraps=state.discovery.reserve)
                    state.discovery.reserve = reserve
                    result = (
                        service.run_once(settings)[0]
                        if automatic
                        else service.run_range(settings, {account.mailboxes[0].id})[0]
                    )
                    expected = int(not automatic and provider == MailProvider.GMAIL_API)
                    self.assertEqual((result.archived, result.failed), (expected, 0))
                    self.assertEqual(len(server.downloads), expected)
                    if not expected:
                        reserve.assert_not_called()

    def test_manual_moves_within_actual_selection_archive_but_unselected_configured_folder_does_not(
        self,
    ):
        for provider in PROVIDERS:
            for selected, expected in (({"INBOX", "STARRED"}, 1), ({"INBOX"}, 0)):
                with self.subTest(provider=provider, selected=selected):
                    _, account, settings, server, _, _, service = self.setup_provider(
                        provider, folders=["INBOX", "STARRED"]
                    )
                    server.after_listing = lambda server=server: setattr(
                        server, "members", {"STARRED"}
                    )
                    result = service.run_range(
                        settings,
                        {account.mailboxes[0].id},
                        folders={account.mailboxes[0].id: selected},
                    )[0]
                    self.assertEqual((result.archived, result.failed), (expected, 0))
                    self.assertEqual(len(server.downloads), expected)

    def test_direct_scope_outcome_and_missing_message_remain_distinct(self):
        for provider in PROVIDERS:
            for folders in (["INBOX", "STARRED"], []):
                with self.subTest(provider=provider, folders=folders):
                    _, account, _, server, source, _, _ = self.setup_provider(
                        provider, folders=folders
                    )
                    target = MailTarget(account, account.mailboxes[0], "INBOX", tuple(folders))
                    server.members = {"STARRED"}
                    remote = source.fetch_message(target, "message", target.mailbox_namespace)
                    self.assertIsNotNone(remote)
                    self.assertTrue(b"".join(remote.iter_raw()))
                    server.members = {"TRASH"}
                    if folders:
                        with self.assertRaises(RemoteMessageOutsideScope):
                            source.fetch_message(target, "message", target.mailbox_namespace)
                    else:
                        self.assertIsNotNone(
                            source.fetch_message(target, "message", target.mailbox_namespace)
                        )
                    server.metadata_error = ProviderHttpError(404, "missing")
                    self.assertIsNone(
                        source.fetch_message(target, "message", target.mailbox_namespace)
                    )

    def test_saved_retry_releases_outside_mail_retains_history_and_missing_is_retryable(self):
        for provider in PROVIDERS:
            for missing in (False, True):
                with self.subTest(provider=provider, missing=missing):
                    _, _, settings, server, _, state, service = self.setup_provider(provider)
                    server.body_error = ProviderHttpError(503, "Earlier body failure")
                    self.assertEqual(service.run_once(settings)[0].failed, 1)
                    intake = state.pending_automatic_intakes()[0]
                    settings.rules = []
                    state.save_settings(settings)
                    server.downloads.clear()
                    server.members = {"TRASH"}
                    if missing:
                        server.metadata_error = ProviderHttpError(404, "missing")
                    result = service.run_once(settings, force_retry=True)[0]
                    self.assertEqual((result.archived, result.failed), (0, int(missing)))
                    self.assertEqual(server.downloads, [])
                    self.assertEqual(len(state.pending_automatic_intakes()), int(missing))
                    with state.connection() as db:
                        row = db.execute(
                            "SELECT status,error FROM intake WHERE id=?", (intake["id"],)
                        ).fetchone()
                    if not missing:
                        self.assertEqual(row["status"], "filtered")
                        self.assertIn("Earlier body failure", row["error"])

    def test_released_frozen_retry_allows_current_selection_and_rule(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                root, account, settings, server, _, state, service = self.setup_provider(provider)
                server.body_error = ProviderHttpError(503, "Earlier body failure")
                self.assertEqual(service.run_once(settings)[0].failed, 1)
                account.mailboxes[0].folders = ["TRASH"]
                settings.rules[0].targets = [RuleTarget(str(root / "current"))]
                state.save_settings(settings)
                server.members = {"TRASH"}
                result = service.run_once(settings, force_retry=True)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))
                self.assertEqual(state.pending_automatic_intakes(), [])
                self.assertEqual(len(list((root / "current").glob("*.eml"))), 1)
                self.assertEqual(list((root / "archive").glob("*.eml")), [])

    def test_graph_saved_retry_uses_frozen_scope_when_current_selection_changes(self):
        cases = (
            (["INBOX"], ["INBOX", "STARRED"], "STARRED", False),
            (["INBOX"], [], "STARRED", False),
            ([], ["STARRED"], "STARRED", True),
            (["INBOX", "STARRED"], ["STARRED", "INBOX"], "STARRED", True),
            (["INBOX"], ["INBOX"], "INBOX", True),
            (["INBOX"], ["inbox"], "INBOX", True),
        )
        for saved, current, membership, retains_old_rule in cases:
            with self.subTest(saved=saved, current=current, membership=membership):
                root, account, settings, server, _, state, service = self.setup_provider(
                    MailProvider.MICROSOFT_GRAPH, folders=saved
                )
                server.body_error = ProviderHttpError(503, "Earlier body failure")
                self.assertEqual(service.run_once(settings)[0].failed, 1)
                intake = state.pending_automatic_intakes()[0]
                account.mailboxes[0].folders = current
                settings.rules[0].targets = [RuleTarget(str(root / "current"))]
                state.save_settings(settings)
                server.enumerated_folders = ["INBOX", "STARRED"]
                server.members = {membership}
                result = service.run_once(settings, force_retry=True)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))
                self.assertEqual(len(list((root / "archive").glob("*.eml"))), int(retains_old_rule))
                self.assertEqual(
                    len(list((root / "current").glob("*.eml"))), int(not retains_old_rule)
                )
                self.assertEqual(state.pending_automatic_intakes(), [])
                with state.connection() as db:
                    old = db.execute(
                        "SELECT status,error FROM intake WHERE id=?", (intake["id"],)
                    ).fetchone()
                if not retains_old_rule:
                    self.assertEqual(old["status"], "filtered")
                    self.assertIn("Earlier body failure", old["error"])

    def test_manual_existing_reservation_releases_when_returned_id_left_scope(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                _, account, settings, server, _, state, service = self.setup_provider(provider)
                revision = state.prepare_run_settings(settings)
                run = state.start_run(
                    account.mailboxes[0].id,
                    "manual",
                    {"folders": ["INBOX"], "start_utc": None, "end_utc": None},
                    settings,
                    revision,
                )
                intake = state.reserve(
                    account.mailboxes[0].id,
                    "message",
                    run,
                    automatic=False,
                    scope_key="gmail-mailbox" if provider == MailProvider.GMAIL_API else "INBOX",
                    remote_id="message",
                )
                state.mark_intake_error(intake, "Earlier download failure")
                state.finish_run(run, error="Earlier scan failure")
                server.after_listing = lambda server=server: setattr(server, "members", {"TRASH"})
                result = service.resume_range_run(run)
                self.assertEqual((result.archived, result.failed), (0, 0))
                self.assertEqual(state.unresolved_intakes(run), [])
                self.assertEqual(server.downloads, [])
                with state.connection() as db:
                    row = db.execute(
                        "SELECT status,error FROM intake WHERE id=?", (intake,)
                    ).fetchone()
                self.assertEqual(tuple(row), ("filtered", "Earlier download failure"))

    def test_graph_empty_metadata_subject_reads_raw_for_absent_and_present_empty_headers(self):
        for header, value, expected in (
            (b"", "(no subject)", 1),
            (b"Subject:\r\n", "(no subject)", 0),
        ):
            for automatic in (False, True):
                with self.subTest(header=header, value=value, automatic=automatic):
                    raw = b"From: sender@example.org\r\n" + header + b"\r\nBody"
                    _, account, settings, server, _, _, service = self.setup_provider(
                        MailProvider.MICROSOFT_GRAPH, raw=raw
                    )
                    server.subject = ""
                    settings.rules[0].conditions = [
                        Condition(MailField.SUBJECT, MatchOperator.EQUALS, value)
                    ]
                    result = (
                        service.run_once(settings)[0]
                        if automatic
                        else service.run_range(settings, {account.mailboxes[0].id})[0]
                    )
                    self.assertEqual((result.archived, result.failed), (expected, 0))
                    self.assertEqual(server.downloads, ["message"])

    def test_public_facade_manual_scope_check_and_successful_operation_completion(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                root, account, settings, server, source, _, _ = self.setup_provider(provider)
                store = ConfigStore(root / "facade")
                store.save(settings)
                server.after_listing = lambda server=server: setattr(server, "members", {"TRASH"})
                with (
                    patch(
                        "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(source)
                    ),
                    patch(
                        "mailarchive.bootstrap.OAuthManager.authorization_status",
                        return_value=AuthorizationStatus(AuthorizationState.AUTHORIZED),
                    ),
                    patch("mailarchive.bootstrap.set_start_at_login"),
                ):
                    app = create_application(store, MemoryCredentialStore())
                    app.start()
                    self.addCleanup(app.close)
                    self.assertTrue(app._background.wait(THREAD_TIMEOUT))
                    app.dispatch_callbacks()
                    done = threading.Event()
                    outcomes = []

                    def observe(progress, outcomes=outcomes, done=done):
                        if progress.origin == "operation" and not progress.active:
                            outcomes.append(progress.state)
                            done.set()

                    app.set_observers(lambda _event: None, observe)
                    operation = app.apply_rule_to_past_mail(settings.rules[0].id, None, None, "UTC")
                    self.assertTrue(done.wait(THREAD_TIMEOUT))
                    self.assertEqual(outcomes, [ExecutionState.COMPLETED])
                    self.assertEqual(
                        app._context.execution.service.operations.manual_operation(operation)[
                            "status"
                        ],
                        "completed",
                    )
                    self.assertEqual(server.downloads, [])
