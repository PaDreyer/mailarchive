"""Real polling retains provider changes while an older frozen intake owns mail."""

import base64
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from mailarchive.application.execution import ExecutionCoordinator
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    RuleTarget,
    Settings,
)
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.http import ProviderHttpError
from tests.concurrency import THREAD_TIMEOUT
from tests.test_mail_sources import FakeOAuth
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service

START = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
RAW = b"From: sender@example.org\r\nTo: owner@example.org\r\nSubject: Invoice\r\n\r\nBody"


class ProviderClock(datetime):
    current = START

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class MovingMailboxHttp:
    """Opaque Graph deltas and Gmail histories emit each change only once per cursor."""

    supports_gmail_streaming = False

    def __init__(self, provider):
        self.provider = provider
        self.parent = "FolderA"
        self.generations = {"FolderA": 1, "FolderB": 0}
        self.history = 100
        self.failures = 2
        self.downloads = []
        self.requests = []

    def move(self):
        self.parent = "FolderB"
        self.generations = {"FolderA": 2, "FolderB": 1}
        self.history = 101

    def download(self):
        self.downloads.append((ProviderClock.current, self.parent))
        if self.failures:
            self.failures -= 1
            raise ProviderHttpError(503, "Temporary MIME download failure")
        return RAW

    def get_json(self, url, token, headers=None, *, cancellation=None):
        if cancellation:
            cancellation.checkpoint()
        self.requests.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if self.provider == MailProvider.GMAIL_API:
            if parsed.path.endswith("/profile"):
                return {"historyId": str(self.history)}
            if parsed.path.endswith("/history"):
                changes = []
                if int(query["startHistoryId"][0]) < self.history:
                    changes = [
                        {"labelsAdded": [{"message": {"id": "message"}, "labelIds": [self.parent]}]}
                    ]
                return {"historyId": str(self.history), "history": changes}
            if parsed.path.endswith("/messages"):
                selected = query.get("labelIds", [None])[0]
                return {"messages": [{"id": "message"}] if selected in (None, self.parent) else []}
            if parsed.path.endswith("/messages/message"):
                metadata = {
                    "internalDate": str(int((START - timedelta(hours=1)).timestamp() * 1000)),
                    "labelIds": [self.parent],
                }
                if query.get("format") == ["raw"]:
                    metadata["raw"] = base64.urlsafe_b64encode(self.download()).decode()
                return metadata
        else:
            if parsed.path.endswith("/mailFolders"):
                return {
                    "value": [{"id": folder, "childFolderCount": 0} for folder in self.generations]
                }
            if "/mailFolders/" in parsed.path and parsed.path.endswith("/messages/delta"):
                folder = unquote(parsed.path.split("/mailFolders/")[1].split("/")[0])
                version = str(self.generations[folder])
                cursor = query.get("$deltatoken", [None])[0]
                changed = cursor != version
                items = [{"id": "message"}] if changed and folder == self.parent else []
                if changed and cursor is not None and folder == "FolderA":
                    items = [{"id": "message", "@removed": {"reason": "changed"}}]
                return {
                    "value": items,
                    "@odata.deltaLink": f"https://graph.microsoft.com{parsed.path}?$deltatoken={version}",
                }
            if "/mailFolders/" in parsed.path and "/messages/" not in parsed.path:
                return {"id": unquote(parsed.path.rsplit("/", 1)[1])}
            if parsed.path.endswith("/messages/message"):
                return {
                    "parentFolderId": self.parent,
                    "receivedDateTime": "2026-10-07T11:00:00Z",
                    "subject": "Invoice",
                }
        raise AssertionError(url)

    def iter_bytes(self, url, token, headers=None, *, cancellation=None, **kwargs):
        if cancellation:
            cancellation.checkpoint()
        self.requests.append(url)
        yield self.download()


class DiscoveryOwnerReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        ProviderClock.current = START
        for module in ("persistence_time", "discovery_repository", "delivery_repository"):
            self.stack.enter_context(
                patch(f"mailarchive.infrastructure.{module}.datetime", ProviderClock)
            )
        self.stack.enter_context(
            patch(
                "mailarchive.application.execution.time.monotonic",
                lambda: (ProviderClock.current - START).total_seconds(),
            )
        )
        self.observed = threading.Condition()
        self.calls = []
        self.progress = []
        self.coordinators = []
        self.addCleanup(self.shutdown_workers)

    def shutdown_workers(self):
        for coordinator in self.coordinators:
            self.assertTrue(coordinator.shutdown(timeout=THREAD_TIMEOUT))

    def configure(self, provider, *, folders=("FolderA",)):
        self.mailbox = Mailbox("owner@example.org", list(folders), archive_existing_messages=True)
        self.account = Account(
            "Owner",
            username=self.mailbox.address,
            provider=provider,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="fake-client",
            mailboxes=[self.mailbox],
            poll_minutes=1,
        )
        self.rule = Rule("All", targets=[RuleTarget(str(self.root / "archive-old"))])
        self.settings = Settings(accounts=[self.account], rules=[self.rule], start_at_login=False)
        self.server = MovingMailboxHttp(provider)
        self.source = (
            GmailMessageSource(FakeOAuth(), self.server)
            if provider == MailProvider.GMAIL_API
            else MicrosoftGraphMessageSource(FakeOAuth(), self.server)
        )
        self.state = WorkspaceStore(self.root / "profile" / "workspace.sqlite3")
        self.state.save_settings(self.settings)
        self.start_worker()

    def start_worker(self):
        self.service = make_service(self.state, Registry(self.source))
        native = self.service.run_once

        def observed(settings, account_ids=None, **kwargs):
            result = native(settings, account_ids, **kwargs)
            with self.observed:
                self.calls.append((ProviderClock.current, account_ids, result))
                self.observed.notify_all()
            return result

        self.stack.enter_context(patch.object(self.service, "run_once", side_effect=observed))
        self.coordinator = ExecutionCoordinator(
            self.service,
            lambda: deepcopy(self.settings),
            self.state.operations,
            polling_schedule=self.state.polling,
            utc_now=lambda: ProviderClock.current,
            progress_handler=self.progress.append,
        )
        self.coordinators.append(self.coordinator)
        self.coordinator.start()

    def wait_call(self, count):
        with self.observed:
            self.assertTrue(
                self.observed.wait_for(lambda: len(self.calls) >= count, THREAD_TIMEOUT),
                "The production polling worker did not execute the expected scan",
            )
        deadline = time.perf_counter() + THREAD_TIMEOUT
        while not self.coordinator.is_idle() and time.perf_counter() < deadline:
            threading.Event().wait(0.002)
        self.assertTrue(self.coordinator.is_idle())

    def poll_at(self, seconds):
        count = len(self.calls) + 1
        ProviderClock.current = START + timedelta(seconds=seconds)
        self.coordinator.settings_changed(self.settings)
        self.wait_call(count)

    def move_selection(self):
        ProviderClock.current = START + timedelta(seconds=45)
        self.mailbox.folders = ["FolderB"]
        self.rule.targets = [RuleTarget(str(self.root / "archive-new"))]
        self.state.save_settings(self.settings)
        self.server.move()

    def exercise_replay(self, provider, *, restart=False, saved_selection_covers_move=False):
        folders = ("FolderA", "FolderB") if saved_selection_covers_move else ("FolderA",)
        self.configure(provider, folders=folders)
        self.assertIsInstance(self.coordinator.check_mail_now(), str)
        self.wait_call(1)
        self.assertEqual(self.calls[0][2][0].failed, 1)
        self.poll_at(31)
        self.assertEqual(self.calls[1][1], set())
        self.assertEqual(self.calls[1][2][0].failed, 1)
        pending = dict(self.state.pending_automatic_intakes()[0])
        self.assertEqual(pending["attempts"], 2)
        self.assertEqual(pending["retry_after"], (START + timedelta(seconds=91)).isoformat())
        self.move_selection()
        self.poll_at(61)
        self.assertEqual(self.calls[2][1], {self.account.id})
        self.assertEqual(dict(self.state.pending_automatic_intakes()[0]), pending)
        self.assertEqual(len(self.server.downloads), 2)
        self.assertEqual(self.calls[2][2][0].failed, 0)
        if restart:
            self.assertTrue(self.coordinator.shutdown(timeout=THREAD_TIMEOUT))
            self.state = WorkspaceStore(self.state.database_path, recover=True)
            self.settings = self.state.load_settings()
            self.start_worker()
        self.poll_at(92)
        self.assertEqual(self.calls[3][1], set())
        self.assertEqual(self.state.pending_automatic_intakes(), [])
        self.poll_at(122)
        self.assertEqual(self.calls[4][1], {self.account.id})
        self.assertEqual(self.calls[4][2][0].failed, 0)
        self.assertEqual(len(self.server.downloads), 3)
        self.assertEqual(
            len(list((self.root / "archive-new").glob("*.eml"))),
            int(not saved_selection_covers_move),
        )
        self.assertEqual(
            len(list((self.root / "archive-old").glob("*.eml"))),
            int(saved_selection_covers_move),
        )
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT status FROM intake WHERE id=?", (pending["id"],)).fetchone()[0],
                "accepted" if saved_selection_covers_move else "filtered",
            )
        self.assertEqual(self.state.spool_usage(), (0, 0))
        checkpoint = self.state.polling.load()[self.account.id]
        self.assertEqual(checkpoint.last_checked_at, START + timedelta(seconds=122))

    def test_graph_folder_move_survives_an_old_deferred_intake(self):
        self.exercise_replay(MailProvider.MICROSOFT_GRAPH)

    def test_gmail_label_move_survives_an_old_deferred_intake(self):
        self.exercise_replay(MailProvider.GMAIL_API)

    def test_graph_replay_remains_durable_across_profile_reopen(self):
        self.exercise_replay(MailProvider.MICROSOFT_GRAPH, restart=True)

    def test_gmail_replay_remains_durable_across_profile_reopen(self):
        self.exercise_replay(MailProvider.GMAIL_API, restart=True)

    def test_graph_retained_owner_uses_its_frozen_rule_without_duplicate_current_output(self):
        self.exercise_replay(MailProvider.MICROSOFT_GRAPH, saved_selection_covers_move=True)

    def test_gmail_retained_owner_uses_its_frozen_rule_without_duplicate_current_output(self):
        self.exercise_replay(MailProvider.GMAIL_API, saved_selection_covers_move=True)

    def exercise_forced_check(self, provider):
        self.configure(provider)
        self.assertIsInstance(self.coordinator.check_mail_now(), str)
        self.wait_call(1)
        self.poll_at(31)
        self.move_selection()
        ProviderClock.current = START + timedelta(seconds=61)
        self.assertIsInstance(self.coordinator.check_mail_now(), str)
        self.wait_call(3)
        self.assertEqual(self.state.pending_automatic_intakes(), [])
        self.assertEqual(len(self.server.downloads), 3)
        self.assertEqual(len(list((self.root / "archive-new").glob("*.eml"))), 1)
        self.assertEqual(list((self.root / "archive-old").glob("*.eml")), [])
        self.poll_at(122)
        self.assertEqual(len(self.server.downloads), 3)
        self.assertEqual(len(list((self.root / "archive-new").glob("*.eml"))), 1)
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 1)

    def test_graph_forced_check_resolves_old_owner_then_accepts_the_new_selection(self):
        self.exercise_forced_check(MailProvider.MICROSOFT_GRAPH)

    def test_gmail_forced_check_resolves_old_owner_then_accepts_the_new_selection(self):
        self.exercise_forced_check(MailProvider.GMAIL_API)
