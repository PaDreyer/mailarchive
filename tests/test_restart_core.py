from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from mailarchive.config import ConfigStore
from mailarchive.engine import _atomic_write as real_atomic_write
from mailarchive.imap_client import MailboxError, RemoteMessage, RemoteMessageUnavailable
from mailarchive.intake_limits import MessageTooLargeError, SpoolCapacityError
from mailarchive.mail_identity import MailTarget, MessageScope, imap_scope
from mailarchive.mail_sources import ScanWideProviderError
from mailarchive.models import Account, Mailbox, Rule, RuleTarget, SaveMode, Settings
from mailarchive.service import ArchiveService
from mailarchive.time_ranges import local_days_to_utc
from mailarchive.workspace import RunNotActiveError, WorkspaceError, WorkspaceStore


def raw_mail(*, attachments: int = 0) -> bytes:
    message = EmailMessage()
    message["From"] = "sender@example.org"
    message["To"] = "recipient@example.org"
    message["Subject"] = "Archive sample"
    message.set_content("A sample message.")
    for _ in range(attachments):
        message.add_attachment(
            b"identical", maintype="application", subtype="octet-stream", filename="same.bin"
        )
    return message.as_bytes()


class FakeSource:
    def __init__(self, messages: dict[str, RemoteMessage]) -> None:
        self.messages = messages
        self.namespace = 'imap-v3:["imap.example.org",993,"owner@example.org","Project  A","1"]'
        self.fetch_count = 0
        self.folders_seen: list[str] = []

    def targets(self, account: Account, mailbox: Mailbox) -> list[MailTarget]:
        return [MailTarget(account, mailbox, folder) for folder in mailbox.folders]

    def _namespace_for(self, target: MailTarget) -> str:
        uid_validity = str(json.loads(self.namespace.removeprefix("imap-v3:"))[-1])
        return imap_scope(target, uid_validity).processing_namespace

    def fetch_messages(self, target: MailTarget, should_fetch, *, sync=None):
        self.folders_seen.append(target.folder)
        namespace = self._namespace_for(target)
        scope = MessageScope(namespace, namespace)
        cursor = sync.cursor_for(scope.synchronization_namespace) if sync else None
        ids = set(self.messages)
        if cursor is not None:
            ids = {item for item in ids if int(item) > int(cursor)}
            ids.update(sync.recheck_ids_for(scope.processing_namespace))

        def iterate():
            for message_id in sorted(ids, key=int):
                if should_fetch(scope, message_id):
                    self.fetch_count += 1
                    if message_id in self.messages:
                        yield self.messages[message_id]
            if sync:
                sync.next_cursor = str(max([int(cursor or 0)] + [int(item) for item in ids]))

        return scope, iterate()

    def fetch_message(
        self, target: MailTarget, remote_id: str, processing_namespace: str
    ) -> RemoteMessage | None:
        self.folders_seen.append(target.folder)
        if processing_namespace != self._namespace_for(target):
            raise MailboxError("message namespace changed")
        self.fetch_count += 1
        return self.messages.get(remote_id)


class PagedRangeSource(FakeSource):
    def __init__(self, messages: dict[str, RemoteMessage]) -> None:
        super().__init__(messages)
        self.enumerated: list[str] = []
        self.failed_second_page = False

    def search_messages(self, target, should_fetch, _start, _end, *, range_sync=None):
        namespace = self._namespace_for(target)
        scope = MessageScope(namespace, namespace)
        token = range_sync.start(scope.processing_namespace)
        first_page = 1 if token == "page-2" else 0

        def iterate():
            for page_index, message_id in enumerate(("1", "2")):
                if page_index < first_page:
                    continue
                if page_index == 1 and not self.failed_second_page:
                    self.failed_second_page = True
                    raise MailboxError("second provider page failed")
                self.enumerated.append(message_id)
                if should_fetch(scope, message_id):
                    yield self.messages[message_id]
                if page_index == 0:
                    range_sync.advance("page-2")
                else:
                    range_sync.finish()

        return scope, iterate()


class Registry:
    def __init__(self, source: FakeSource) -> None:
        self.source = source

    def get(self, _account):
        return self.source


class RestartCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mailbox = Mailbox("owner@example.org", folders=["Project  A"])
        self.account = Account(
            "Owner", host="imap.example.org", username="owner@example.org", mailboxes=[self.mailbox]
        )
        self.rule = Rule("First", targets=[RuleTarget(str(self.root / "A"))])
        self.settings = Settings(archive_root="", accounts=[self.account], rules=[self.rule])
        self.source = FakeSource(
            {
                "1": RemoteMessage(
                    "1",
                    raw_mail(),
                    datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
                    "imap_internaldate",
                )
            }
        )
        self.state = WorkspaceStore(self.root / "profile" / "workspace.sqlite3")
        self.service = ArchiveService(None, self.state, source_registry=Registry(self.source))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_range(self):
        return self.service.run_range(self.settings, {self.mailbox.id})[0]

    def test_empty_rules_survive_restart_without_archive(self) -> None:
        self.settings.rules = []
        store = ConfigStore(self.root / "profile")
        store.save(self.settings)
        self.assertEqual(store.load().rules, [])
        self.assertEqual(self.run_range().unmatched, 1)
        self.assertEqual(list(self.root.rglob("*.eml")), [])

    def test_manual_intake_error_is_visible_and_resumable_with_saved_rules(self) -> None:
        original = self.source.messages["1"]
        self.source.messages["1"] = RemoteMessage("1", original.raw, None, "")
        self.assertEqual(self.run_range().failed, 1)
        interrupted = self.state.incomplete_manual_runs()
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(json.loads(interrupted[0]["checkpoint"])["last_remote_id"], "1")
        self.source.messages["1"] = original
        self.assertEqual(self.service.resume_range_run(interrupted[0]["id"]).archived, 1)
        self.assertEqual(self.state.incomplete_manual_runs(), [])
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)

    def test_resume_keeps_missing_manual_intake_failed_until_explicit_cancel(self) -> None:
        original = self.source.messages["1"]
        self.source.messages["1"] = RemoteMessage("1", original.raw, None, "")
        self.assertEqual(self.run_range().failed, 1)
        run_id = self.state.incomplete_manual_runs()[0]["id"]
        self.source.messages.clear()
        resumed = self.service.resume_range_run(run_id)
        self.assertEqual(resumed.failed, 1)
        run = self.state.incomplete_manual_runs()[0]
        self.assertEqual(run["id"], run_id)
        self.assertIn("valid reception time", run["error"])
        self.assertEqual(len(self.state.intake_errors()), 1)
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 1)
        self.service.cancel_run(run_id)
        self.assertEqual(self.state.incomplete_manual_runs(), [])
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

    def test_first_matching_rule_controls_all_free_destinations(self) -> None:
        self.rule.targets = [
            RuleTarget(str(self.root / "A" / "{year}" / "{month}")),
            RuleTarget(str(self.root / "B"), SaveMode.EMAIL_ONLY),
            RuleTarget(str(self.root / "C"), SaveMode.EMAIL_ONLY),
        ]
        self.settings.rules.append(Rule("Second", targets=[RuleTarget(str(self.root / "Wrong"))]))
        self.assertEqual(self.run_range().archived, 1)
        for name in ("A/2026/01", "B", "C"):
            self.assertEqual(len(list((self.root / name).glob("*.eml"))), 1)
        self.assertFalse((self.root / "Wrong").exists())

    def test_partial_failure_and_source_deletion_resume_from_work_copy(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        result = self.run_range()
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.state.incomplete_manual_runs(), [])
        with self.state.connection() as db:
            run = db.execute("SELECT status, error FROM scan_run").fetchone()
        self.assertEqual((run["status"], run["error"]), ("completed", None))
        self.assertEqual(len(self.state.open_plans()), 1)
        plan_id = self.state.open_plans()[0]["id"]
        target_status = {row["path"]: row["status"] for row in self.state.plan_targets(plan_id)}
        self.assertEqual(target_status[str(self.root / "A")], "done")
        self.assertEqual(target_status[str(obstruction / "archive")], "error")
        failed_output = next(row for row in self.state.outputs(plan_id) if row["status"] == "error")
        self.assertEqual(failed_output["attempts"], 1)
        self.assertIsNotNone(failed_output["retry_after"])
        self.assertFalse(self.state.automatic_work_due(datetime.now(timezone.utc)))
        self.assertTrue(
            self.state.automatic_work_due(
                datetime.fromisoformat(failed_output["retry_after"]) + timedelta(seconds=1)
            )
        )
        self.service.run_once(self.settings)
        self.assertEqual(
            next(row for row in self.state.outputs(plan_id) if row["status"] == "error")[
                "attempts"
            ],
            1,
        )
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.source.messages.clear()
        obstruction.unlink()
        self.assertEqual(self.service.resume_open(), (1, 0))
        self.assertEqual(len(list((self.root / "offline" / "archive").glob("*.eml"))), 1)
        self.assertEqual(self.state.open_plans(), [])

    def test_post_accept_execution_error_stays_with_open_plan(self) -> None:
        with patch.object(
            self.service.engine,
            "execute",
            side_effect=RuntimeError("could not initialize outputs"),
        ):
            result = self.run_range()

        self.assertEqual(result.failed, 1)
        self.assertIn("could not initialize outputs", result.errors[0])
        self.assertEqual(self.state.incomplete_manual_runs(), [])
        plan = self.state.open_plans()[0]
        self.assertIn("could not initialize outputs", plan["error"])
        with self.state.connection() as db:
            run = db.execute("SELECT status, error FROM scan_run").fetchone()
            intake = db.execute("SELECT status, error FROM intake").fetchone()
        self.assertEqual((run["status"], run["error"]), ("completed", None))
        self.assertEqual((intake["status"], intake["error"]), ("accepted", None))

    def test_identical_outputs_are_shared_but_complete_each_destination(self) -> None:
        shared = str(self.root / "A")
        self.rule.targets = [RuleTarget(shared), RuleTarget(shared)]
        self.assertEqual(self.run_range().archived, 1)
        with self.state.connection() as db:
            plan_id = db.execute("SELECT id FROM plan").fetchone()[0]
            self.assertEqual(db.execute("SELECT count(*) FROM output").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM output_target").fetchone()[0], 2)
        self.assertEqual(
            [row["status"] for row in self.state.plan_targets(plan_id)], ["done", "done"]
        )

    def test_paused_plan_is_not_retried_and_resumes_from_its_work_copy(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        self.run_range()
        plan_id = self.state.open_plans()[0]["id"]
        self.service.pause_plan(plan_id)
        self.assertEqual(self.state.open_plans(), [])
        self.assertEqual(self.state.work_plans()[0]["status"], "paused")
        raw_path = Path(self.state.work_plans()[0]["raw_path"])
        WorkspaceStore(self.state.database_path, recover=True)
        self.assertTrue(raw_path.is_file())
        obstruction.unlink()
        self.assertEqual(self.service.resume_open(), (0, 0))
        self.assertFalse((self.root / "offline" / "archive").exists())
        self.source.messages.clear()
        self.assertEqual(self.service.resume_plan(plan_id), (1, 0))
        self.assertEqual(self.state.work_plans(), [])

    def test_missing_work_copy_error_is_persisted_for_open_work(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        self.run_range()
        plan = self.state.open_plans()[0]
        Path(plan["raw_path"]).unlink()
        self.assertEqual(self.service.resume_open(), (0, 1))
        visible = self.state.work_plans()[0]
        self.assertIn("working copy", visible["error"])

    def test_explicit_resume_persists_missing_work_copy_error(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        self.run_range()
        plan = self.state.open_plans()[0]
        self.service.pause_plan(plan["id"])
        Path(plan["raw_path"]).unlink()
        self.assertEqual(self.service.resume_plan(plan["id"]), (0, 1))
        visible = self.state.work_plans()[0]
        self.assertEqual(visible["status"], "open")
        self.assertIn("working copy", visible["error"])

    def test_crash_after_publication_does_not_duplicate_output(self) -> None:
        original = self.state.output_done

        def crash(*_args):
            raise RuntimeError("simulated crash after publish")

        self.state.output_done = crash
        try:
            self.assertEqual(self.run_range().failed, 1)
        finally:
            self.state.output_done = original
        files = list((self.root / "A").glob("*.eml"))
        self.assertEqual(len(files), 1)
        self.assertEqual(self.service.resume_open(), (1, 0))
        self.assertEqual(list((self.root / "A").glob("*.eml")), files)

    def test_completed_archive_is_not_reported_failed_when_work_copy_cleanup_fails(self) -> None:
        original_unlink = os.unlink

        def fail_raw_cleanup(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and Path(path).suffix == ".eml":
                raise PermissionError("simulated cleanup denial")
            return original_unlink(path, *args, **kwargs)

        with patch("mailarchive.workspace.os.unlink", side_effect=fail_raw_cleanup):
            result = self.run_range()

        self.assertEqual((result.archived, result.failed), (1, 0))
        self.assertEqual(self.state.open_plans(), [])
        with self.state.connection() as db:
            plan = db.execute("SELECT status, error, raw_path FROM plan").fetchone()
        self.assertEqual((plan["status"], plan["error"]), ("complete", None))
        self.assertTrue(Path(plan["raw_path"]).exists())
        self.state.recover()
        self.assertFalse(Path(plan["raw_path"]).exists())

    def test_repeated_range_adds_only_new_target(self) -> None:
        self.assertEqual(self.run_range().archived, 1)
        original = list((self.root / "A").glob("*.eml"))
        self.rule.targets.append(RuleTarget(str(self.root / "B")))
        self.assertEqual(self.run_range().archived, 1)
        self.assertEqual(list((self.root / "A").glob("*.eml")), original)
        self.assertEqual(len(list((self.root / "B").glob("*.eml"))), 1)
        self.run_range()
        self.assertEqual(len(list((self.root / "B").glob("*.eml"))), 1)

    def test_manually_deleted_archive_is_not_silently_repaired(self) -> None:
        self.run_range()
        path = next((self.root / "A").glob("*.eml"))
        path.unlink()
        self.run_range()
        self.assertEqual(list((self.root / "A").glob("*.eml")), [])

    def test_unrelated_name_collision_is_not_taken_as_receipt(self) -> None:
        identity = sha256(
            f"{self.mailbox.id}\x00{self.source.namespace}\x001".encode()
        ).hexdigest()[:12]
        target = self.root / "A"
        target.mkdir()
        collision = target / f"2026-01-01_10-00-00_Archive sample_{identity}.eml"
        collision.write_text("unrelated")
        self.run_range()
        self.assertEqual(collision.read_text(), "unrelated")
        self.assertEqual(len(list(target.glob("*.eml"))), 2)

    def test_capacity_limit_leaves_visible_intake_error(self) -> None:
        with patch("mailarchive.engine.MAX_SPOOL_BYTES", 10):
            self.assertEqual(self.run_range().failed, 1)
        self.assertEqual(self.state.open_plans(), [])
        with self.state.connection() as db:
            row = db.execute("SELECT status, error FROM intake").fetchone()
        self.assertEqual(row["status"], "error")
        self.assertIn("capacity", row["error"])
        self.assertIn("capacity", self.state.intake_errors()[0]["error"])

    def test_unresolved_intake_queue_has_an_atomic_admission_limit(self) -> None:
        self.source.messages = {
            str(number): RemoteMessage(str(number), raw_mail(), None, "") for number in range(1, 5)
        }
        with patch("mailarchive.workspace.MAX_ACTIVE_INTAKES", 2):
            result = self.run_range()

        self.assertEqual(result.failed, 3)
        self.assertEqual(self.source.fetch_count, 2)
        with self.state.connection() as db:
            active = db.execute(
                "SELECT count(*) FROM active_message WHERE kind='intake'"
            ).fetchone()[0]
            run = db.execute("SELECT status, error FROM scan_run").fetchone()
        self.assertEqual(active, 2)
        self.assertEqual(run["status"], "failed")
        self.assertIn("queue reached its capacity", run["error"])

    def test_message_limit_stops_stream_and_removes_partial_spool_file(self) -> None:
        yielded = []

        def chunks():
            for value in (b"123456", b"abcdef", b"must-not-be-read"):
                yielded.append(value)
                yield value

        with patch("mailarchive.engine.MAX_MESSAGE_BYTES", 10):
            with self.assertRaisesRegex(MessageTooLargeError, "256 MiB"):
                self.service.engine._spool(chunks())

        self.assertEqual(yielded, [b"123456", b"abcdef"])
        self.assertEqual(list(self.state.spool_dir.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-specific")
    def test_directory_fsync_failure_removes_published_orphan(self) -> None:
        with patch("mailarchive.engine.os.fsync", side_effect=[None, OSError("sync failed")]):
            with self.assertRaisesRegex(OSError, "sync failed"):
                self.service.engine._spool([raw_mail()])

        self.assertEqual(list(self.state.spool_dir.iterdir()), [])
        self.assertEqual(self.state.spool_usage(), (0, 0))

    @unittest.skipIf(os.name == "nt", "directory descriptor protection is POSIX-specific")
    def test_spool_rejects_parent_replaced_by_symlink_after_accounting(self) -> None:
        external = self.root / "external-spool"
        external.mkdir()
        displaced = self.root / "original-spool"
        probe = external / "symlink-probe"
        try:
            probe.symlink_to(self.state.spool_dir, target_is_directory=True)
            probe.unlink()
        except OSError as exc:
            self.skipTest(f"Symlinks are unavailable: {exc}")

        original_usage = self.state.spool_usage

        def swap_after_accounting():
            result = original_usage()
            self.state.spool_dir.rename(displaced)
            self.state.spool_dir.symlink_to(external, target_is_directory=True)
            return result

        try:
            with patch.object(self.state, "spool_usage", side_effect=swap_after_accounting):
                with self.assertRaisesRegex(WorkspaceError, "not a safe directory"):
                    self.service.engine._spool([raw_mail()])
            self.assertEqual(list(external.iterdir()), [])
            self.assertEqual(list(displaced.iterdir()), [])
        finally:
            if self.state.spool_dir.is_symlink():
                self.state.spool_dir.unlink()
            if displaced.exists():
                displaced.rename(self.state.spool_dir)

    def test_spool_usage_counts_unreferenced_raw_and_temporary_files(self) -> None:
        (self.state.spool_dir / "orphan.eml").write_bytes(b"raw")
        (self.state.spool_dir / "download.tmp").write_bytes(b"part")
        (self.state.spool_dir / "ignored.txt").write_bytes(b"ignore")

        self.assertEqual(self.state.spool_usage(), (0, 7))

    def test_work_copy_cleanup_retry_is_shared_between_store_instances(self) -> None:
        orphan = self.state.spool_dir / "cleanup-retry.eml"
        orphan.write_bytes(b"orphan")
        second_store = WorkspaceStore(self.state.database_path)
        original_unlink = os.unlink

        def fail_orphan(path, *args, **kwargs):
            if Path(path).name == orphan.name:
                raise PermissionError("simulated cleanup denial")
            return original_unlink(path, *args, **kwargs)

        with patch("mailarchive.workspace.os.unlink", side_effect=fail_orphan):
            self.state.recover()
            self.assertTrue(orphan.exists())

        self.assertEqual(second_store.spool_usage(), (0, 0))
        self.assertFalse(orphan.exists())

    def test_cancelled_run_rejects_a_late_reservation(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id, "manual", {"folders": ["Project  A"]}, self.settings, revision
        )
        self.state.cancel_run(run_id)

        with self.assertRaises(RunNotActiveError):
            self.state.reserve(
                self.mailbox.id,
                self.source.namespace + "\0late",
                run_id,
                automatic=False,
            )

        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

    def test_finish_run_serializes_against_a_late_reservation(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        self.state.update_range_target_checkpoint(
            run_id, "Project  A", self.source.namespace, None, True
        )
        second_store = WorkspaceStore(self.state.database_path)
        validation_started = threading.Event()
        allow_finish = threading.Event()
        errors: list[BaseException] = []
        original_validation = WorkspaceStore._validate_run_checkpoint

        def block_after_write_lock(run):
            validation_started.set()
            if not allow_finish.wait(5):
                raise RuntimeError("test finish wait timed out")
            return original_validation(run)

        def finish():
            try:
                self.state.finish_run(run_id)
            except BaseException as exc:  # pragma: no cover - assertion reports the thread error
                errors.append(exc)

        reservation_errors: list[BaseException] = []

        def reserve():
            try:
                second_store.reserve(
                    self.mailbox.id,
                    self.source.namespace + "\0finish-race",
                    run_id,
                    automatic=False,
                    scope_key="Project  A",
                    remote_id="finish-race",
                )
            except BaseException as exc:
                reservation_errors.append(exc)

        with patch.object(
            WorkspaceStore, "_validate_run_checkpoint", side_effect=block_after_write_lock
        ):
            finish_thread = threading.Thread(target=finish)
            finish_thread.start()
            self.assertTrue(validation_started.wait(5))
            reserve_thread = threading.Thread(target=reserve)
            reserve_thread.start()
            reserve_thread.join(0.1)
            self.assertTrue(reserve_thread.is_alive())
            allow_finish.set()
            finish_thread.join(5)
            reserve_thread.join(5)

        self.assertEqual(errors, [])
        self.assertEqual(len(reservation_errors), 1)
        self.assertIsInstance(reservation_errors[0], RunNotActiveError)
        with self.state.connection() as db:
            run = db.execute("SELECT status FROM scan_run WHERE id=?", (run_id,)).fetchone()
            unresolved = db.execute(
                "SELECT count(*) FROM intake WHERE run_id=? AND status IN ('reserved', 'error')",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(unresolved, 0)

    def test_stale_release_cannot_delete_a_newer_intake_reference(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        selection = {"folders": ["Project  A"], "start_utc": None, "end_utc": None}
        key = self.source.namespace + "\0repeated"
        first_run = self.state.start_run(
            self.mailbox.id, "manual", selection, self.settings, revision
        )
        first_intake = self.state.reserve(
            self.mailbox.id,
            key,
            first_run,
            automatic=False,
            scope_key="Project  A",
            remote_id="repeated",
        )
        self.state.mark_unmatched(
            str(first_intake),
            received_at="2026-01-01T00:00:00+00:00",
            received_origin="imap_internaldate",
            sender_at=None,
            subject="first",
        )
        second_run = self.state.start_run(
            self.mailbox.id, "manual", selection, self.settings, revision
        )
        second_intake = self.state.reserve(
            self.mailbox.id,
            key,
            second_run,
            automatic=False,
            scope_key="Project  A",
            remote_id="repeated",
        )

        with self.assertRaises(RunNotActiveError):
            self.state.release_intake(str(first_intake))

        with self.state.connection() as db:
            active = db.execute(
                "SELECT kind, ref_id FROM active_message WHERE source_id=? AND message_key=?",
                (self.mailbox.id, key),
            ).fetchone()
        self.assertEqual((active["kind"], active["ref_id"]), ("intake", second_intake))

    def test_cancelled_run_rejects_every_late_intake_transition(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id, "manual", {"folders": ["Project  A"]}, self.settings, revision
        )
        intake_id = self.state.reserve(
            self.mailbox.id,
            self.source.namespace + "\0late",
            run_id,
            automatic=False,
        )
        self.assertIsNotNone(intake_id)
        self.state.cancel_run(run_id)

        self.assertFalse(self.state.mark_intake_error(str(intake_id), "late failure"))
        with self.assertRaises(RunNotActiveError):
            self.state.mark_filtered(
                str(intake_id),
                received_at="2026-01-01T00:00:00+00:00",
                received_origin="imap_internaldate",
            )
        with self.assertRaises(RunNotActiveError):
            self.state.mark_unmatched(
                str(intake_id),
                received_at="2026-01-01T00:00:00+00:00",
                received_origin="imap_internaldate",
                sender_at=None,
                subject="late",
            )
        with self.assertRaises(RunNotActiveError):
            self.state.accept_plan(
                str(intake_id),
                self.root / "late.eml",
                "digest",
                "2026-01-01T00:00:00+00:00",
                "imap_internaldate",
                None,
                "late",
                json.dumps({"rule": self.rule.to_dict(), "timezone": "UTC"}),
            )

        with self.state.connection() as db:
            intake = db.execute("SELECT status FROM intake WHERE id=?", (intake_id,)).fetchone()
            self.assertEqual(intake["status"], "cancelled")
            self.assertEqual(db.execute("SELECT count(*) FROM plan").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

    def test_provider_disappearance_is_visible_until_later_reconciliation(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def unavailable():
            raise RemoteMessageUnavailable("message disappeared")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=unavailable,
        )

        result = self.service.run_once(self.settings)[0]

        self.assertEqual((result.archived, result.failed), (0, 1))
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 1)
            intake = db.execute(
                "SELECT status, error FROM intake ORDER BY created_at DESC"
            ).fetchone()
            message = db.execute(
                "SELECT received_at, received_origin FROM source_message WHERE message_key=?",
                (self.source.namespace + "\0" + "2",),
            ).fetchone()
        self.assertEqual(intake["status"], "error")
        self.assertIn("disappeared", intake["error"])
        self.assertEqual(message["received_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(message["received_origin"], "imap_internaldate")

        def reconcile(_target, _should_fetch, *, sync=None):
            scope = MessageScope(self.source.namespace, self.source.namespace)

            def empty():
                sync.discard("2")
                sync.next_cursor = "2"
                if False:
                    yield None

            return scope, empty()

        self.source.fetch_messages = reconcile
        recovered = self.service.run_once(self.settings, force_retry=True)[0]
        self.assertEqual((recovered.archived, recovered.failed), (0, 0))
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)
            intake = db.execute(
                "SELECT status, error FROM intake ORDER BY created_at DESC"
            ).fetchone()
        self.assertEqual(intake["status"], "filtered")
        self.assertIn("disappeared", intake["error"])

    def test_missing_direct_retry_remains_a_visible_intake_error(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        crashed_run = self.state.start_run(
            self.mailbox.id,
            "automatic",
            {"folders": ["Project  A"]},
            self.settings,
            revision,
        )
        intake_id = self.state.reserve(
            self.mailbox.id,
            self.source.namespace + "\0" + "2",
            crashed_run,
            automatic=True,
            scope_key="Project  A",
            remote_id="2",
        )
        self.assertIsNotNone(intake_id)

        result = self.service.run_once(self.settings, force_retry=True)[0]

        self.assertEqual(result.failed, 1)
        self.assertIn("no longer available before intake completed", result.errors[0])
        with self.state.connection() as db:
            intake = db.execute(
                "SELECT status, error FROM intake WHERE remote_id='2' ORDER BY created_at DESC"
            ).fetchone()
            active = db.execute("SELECT count(*) FROM active_message").fetchone()[0]
        self.assertEqual(intake["status"], "error")
        self.assertIn("no longer available before intake completed", intake["error"])
        self.assertEqual(active, 1)

    def test_download_error_keeps_known_provider_reception_metadata(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def broken_download():
            raise MailboxError("IMAP body transfer failed")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )

        result = self.service.run_once(self.settings)[0]

        self.assertEqual(result.failed, 1)
        with self.state.connection() as db:
            message = db.execute(
                "SELECT received_at, received_origin FROM source_message WHERE message_key=?",
                (self.source.namespace + "\0" + "2",),
            ).fetchone()
        self.assertEqual(message["received_at"], "2026-01-02T03:04:00+00:00")
        self.assertEqual(message["received_origin"], "imap_internaldate")

    def test_failed_automatic_intake_can_succeed_in_a_later_run(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def broken_download():
            raise MailboxError("temporary body failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)

        pending = self.state.pending_automatic_intakes()[0]
        attempts = pending["attempts"]
        deferred = self.service.run_once(self.settings)[0]
        self.assertEqual((deferred.archived, deferred.failed), (0, 0))
        self.assertEqual(self.state.pending_automatic_intakes()[0]["attempts"], attempts)
        with self.state.connection() as db, db:
            db.execute(
                "UPDATE intake SET retry_after='2000-01-01T00:00:00+00:00' WHERE id=?",
                (pending["id"],),
            )

        self.source.messages["2"] = RemoteMessage(
            "2",
            raw_mail(),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            "imap_internaldate",
        )
        recovered = self.service.run_once(self.settings, set())[0]

        self.assertEqual((recovered.archived, recovered.failed), (1, 0))
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM plan").fetchone()[0], "complete")

    def test_scan_wide_direct_retry_counts_one_attempt(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def initial_failure():
            raise MailboxError("temporary body failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=initial_failure,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)
        self.mailbox.enabled = False

        def throttled():
            raise ScanWideProviderError("HTTP 429: rate limited")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=throttled,
        )

        retried = self.service.run_once(self.settings, force_retry=True)[0]

        self.assertEqual(retried.failed, 1)
        pending = self.state.pending_automatic_intakes()[0]
        self.assertEqual(pending["attempts"], 2)
        self.assertIn("HTTP 429", pending["error"])

    def test_oversized_automatic_message_is_terminal_and_does_not_block_later_mail(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)
        valid = raw_mail()
        self.source.messages.update(
            {
                "2": RemoteMessage(
                    "2",
                    valid,
                    datetime(2026, 1, 2, tzinfo=timezone.utc),
                    "imap_internaldate",
                    raw_size=len(valid) + 1,
                ),
                "3": RemoteMessage(
                    "3",
                    valid,
                    datetime(2026, 1, 3, tzinfo=timezone.utc),
                    "imap_internaldate",
                ),
            }
        )

        with patch("mailarchive.engine.MAX_MESSAGE_BYTES", len(valid)):
            result = self.service.run_once(self.settings)[0]

        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.state.scope(self.mailbox.id, "Project  A")["cursor"], "3")
        with self.state.connection() as db:
            rejected = db.execute(
                "SELECT i.status, i.error, m.terminal_state FROM intake i "
                "JOIN source_message m ON m.source_id=i.source_id "
                "AND m.message_key=i.message_key WHERE i.remote_id='2'"
            ).fetchone()
            active = db.execute("SELECT count(*) FROM active_message").fetchone()[0]
        self.assertEqual((rejected["status"], rejected["terminal_state"]), ("rejected", "rejected"))
        self.assertIn("256 MiB", rejected["error"])
        self.assertEqual(active, 0)
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 0)

    def test_disabled_source_does_not_strand_automatic_intake(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def broken_download():
            raise MailboxError("temporary body failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)
        self.mailbox.enabled = False
        pending = self.state.pending_automatic_intakes()[0]
        self.assertEqual(pending["attempts"], 1)
        self.assertIsNotNone(pending["retry_after"])
        self.assertFalse(self.service.has_automatic_work())
        self.assertTrue(
            self.state.automatic_work_due(
                datetime.fromisoformat(pending["retry_after"]) + timedelta(seconds=1)
            )
        )
        self.source.messages["2"] = RemoteMessage(
            "2",
            raw_mail(),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            "imap_internaldate",
        )

        resumed = self.service.run_once(self.settings, force_retry=True)[0]

        self.assertEqual((resumed.archived, resumed.failed), (1, 0))
        self.assertEqual(self.state.intake_errors(), [])
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

    def test_force_retry_survives_disable_and_reenable_scope_reset(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def broken_download():
            raise MailboxError("temporary body failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)

        self.mailbox.enabled = False
        disabled = self.service.run_once(self.settings)[0]
        self.assertEqual((disabled.checked, disabled.failed), (0, 0))
        self.mailbox.enabled = True
        self.source.messages["2"] = RemoteMessage(
            "2",
            raw_mail(),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            "imap_internaldate",
        )

        resumed = self.service.run_once(self.settings, force_retry=True)[0]

        self.assertEqual((resumed.archived, resumed.failed), (1, 0))
        self.assertEqual(self.state.intake_errors(), [])
        self.assertEqual(self.state.scope(self.mailbox.id, "Project  A")["cursor"], "2")

    def test_capacity_failure_during_direct_retry_blocks_same_source_scan(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def broken_download():
            raise MailboxError("temporary body failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)
        self.source.messages["2"] = RemoteMessage(
            "2",
            raw_mail(),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            "imap_internaldate",
        )
        self.mailbox.enabled = False
        self.service.run_once(self.settings)
        self.mailbox.enabled = True

        with (
            patch.object(
                self.service.engine,
                "stage",
                side_effect=SpoolCapacityError("The local work queue is full."),
            ),
            patch.object(self.service, "_run_mailbox", wraps=self.service._run_mailbox) as scan,
        ):
            retried = self.service.run_once(self.settings, force_retry=True)[0]

        self.assertEqual(retried.failed, 1)
        scan.assert_not_called()
        pending = self.state.pending_automatic_intakes()[0]
        self.assertEqual(pending["attempts"], 2)
        self.assertIn("work queue is full", pending["error"])

    def test_unresolved_automatic_intake_can_be_cancelled_individually(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

        def broken_download():
            raise MailboxError("permanent provider failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)
        intake_id = str(self.state.intake_errors()[0]["id"])

        self.service.cancel_intake(intake_id)

        self.assertEqual(self.state.intake_errors(), [])
        with self.state.connection() as db:
            cancelled = db.execute(
                "SELECT i.status, i.source_id, i.message_key, m.terminal_state "
                "FROM intake i JOIN source_message m ON m.source_id=i.source_id "
                "AND m.message_key=i.message_key WHERE i.id=?",
                (intake_id,),
            ).fetchone()
            self.assertEqual(
                (cancelled["status"], cancelled["terminal_state"]),
                ("cancelled", "aborted"),
            )
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

        revision = self.state.prepare_run_settings(self.settings)
        repeated_run = self.state.start_run(
            self.mailbox.id,
            "automatic",
            {"folders": self.mailbox.folders},
            self.settings,
            revision,
        )
        self.assertIsNone(
            self.state.reserve(
                cancelled["source_id"],
                cancelled["message_key"],
                repeated_run,
                automatic=True,
                scope_key="Project  A",
                remote_id="2",
            )
        )

    def test_monitoring_status_stays_setting_up_until_baseline_finishes(self) -> None:
        self.state.prepare_run_settings(self.settings)
        self.assertEqual(
            self.state.source_monitoring_status(
                self.mailbox.id, self.account.provider, self.mailbox.folders
            ),
            "setting_up",
        )
        self.service.run_once(self.settings)
        self.assertEqual(
            self.state.source_monitoring_status(
                self.mailbox.id, self.account.provider, self.mailbox.folders
            ),
            "active",
        )

    def test_dynamic_folder_monitoring_waits_for_every_discovered_scope(self) -> None:
        self.mailbox.folders = []
        targets = [
            MailTarget(self.account, self.mailbox, "folder-one"),
            MailTarget(self.account, self.mailbox, "folder-two"),
        ]

        def fetch_messages(target, _should_fetch, *, sync=None):
            if target.folder == "folder-two":
                raise MailboxError("folder baseline failed")
            scope = MessageScope("processing-one", "synchronization-one")

            def empty():
                sync.next_cursor = "cursor-one"
                if False:
                    yield None

            return scope, empty()

        with (
            patch.object(self.source, "targets", return_value=targets),
            patch.object(self.source, "fetch_messages", side_effect=fetch_messages),
        ):
            result = self.service.run_once(self.settings)[0]

        self.assertEqual(result.failed, 1)
        self.assertEqual(self.state.scope(self.mailbox.id, "folder-one")["status"], "active")
        self.assertEqual(self.state.scope(self.mailbox.id, "folder-two")["status"], "new")
        self.assertEqual(
            self.state.source_monitoring_status(self.mailbox.id, self.account.provider, []),
            "setting_up",
        )

        self.state.finish_scope(
            self.mailbox.id,
            "folder-two",
            "processing-two",
            "synchronization-two",
            "cursor-two",
        )
        self.assertEqual(
            self.state.source_monitoring_status(self.mailbox.id, self.account.provider, []),
            "active",
        )

    def test_broadening_to_dynamic_folders_sets_durable_discovery_pending(self) -> None:
        self.service.run_once(self.settings)
        previous = self.state.scope(self.mailbox.id, "Project  A")
        self.assertEqual(previous["status"], "active")

        self.mailbox.folders = []
        self.state.prepare_run_settings(self.settings)

        retained = self.state.scope(self.mailbox.id, "Project  A")
        self.assertEqual((retained["status"], retained["cursor"]), ("active", "1"))
        with self.state.connection() as db:
            pending = db.execute(
                "SELECT discovery_pending FROM source WHERE id=?", (self.mailbox.id,)
            ).fetchone()[0]
        self.assertEqual(pending, 1)
        self.assertEqual(
            self.state.source_monitoring_status(self.mailbox.id, self.account.provider, []),
            "setting_up",
        )

    def test_empty_dynamic_discovery_removes_stale_scopes(self) -> None:
        self.service.run_once(self.settings)
        self.mailbox.folders = []

        with patch.object(self.source, "targets", return_value=[]):
            result = self.service.run_once(self.settings)[0]

        self.assertEqual(result.failed, 1)
        self.assertIsNone(self.state.scope(self.mailbox.id, "Project  A"))
        with self.state.connection() as db:
            pending = db.execute(
                "SELECT discovery_pending FROM source WHERE id=?", (self.mailbox.id,)
            ).fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertEqual(
            self.state.source_monitoring_status(self.mailbox.id, self.account.provider, []),
            "setting_up",
        )

    def test_dynamic_imap_discovery_filters_intake_from_removed_folder(self) -> None:
        self.service.run_once(self.settings)

        def broken_download():
            raise MailboxError("temporary body failure")
            yield b""

        self.source.messages["2"] = RemoteMessage(
            "2",
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            received_origin="imap_internaldate",
            raw_chunks=broken_download,
        )
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)
        original_error = self.state.pending_automatic_intakes()[0]["error"]
        self.mailbox.folders = []
        target = MailTarget(self.account, self.mailbox, "Replacement")

        with patch.object(self.source, "targets", return_value=[target]):
            result = self.service.run_once(self.settings, force_retry=True)[0]

        self.assertEqual(result.failed, 0)
        self.assertEqual(self.state.pending_automatic_intakes(), [])
        self.assertIsNone(self.state.scope(self.mailbox.id, "Project  A"))
        self.assertEqual(self.state.scope(self.mailbox.id, "Replacement")["status"], "active")
        with self.state.connection() as db:
            intake = db.execute(
                "SELECT status, error FROM intake WHERE remote_id='2' ORDER BY created_at DESC"
            ).fetchone()
            active = db.execute("SELECT count(*) FROM active_message").fetchone()[0]
        self.assertEqual((intake["status"], intake["error"]), ("filtered", original_error))
        self.assertEqual(active, 0)

    def test_manual_range_persists_the_confirmed_timezone(self) -> None:
        self.service.run_range(self.settings, {self.mailbox.id}, timezone_name="Europe/Berlin")
        with self.state.connection() as db:
            selection = json.loads(db.execute("SELECT selection_json FROM scan_run").fetchone()[0])
        self.assertEqual(selection["timezone"], "Europe/Berlin")

    def test_capacity_limit_stops_discovery_without_advancing_cursor(self) -> None:
        self.mailbox.folders.append("Other")
        self.source.messages.clear()
        self.assertEqual(self.service.run_once(self.settings)[0].failed, 0)
        self.assertEqual(self.state.scope(self.mailbox.id, "Project  A")["cursor"], "0")
        self.assertEqual(self.state.scope(self.mailbox.id, "Other")["cursor"], "0")
        self.source.messages = {
            str(number): RemoteMessage(
                str(number), raw_mail(), datetime(2026, 1, 1, tzinfo=timezone.utc)
            )
            for number in (1, 2)
        }
        with patch("mailarchive.engine.MAX_SPOOL_BYTES", 10):
            self.assertEqual(self.service.run_once(self.settings)[0].failed, 1)
        self.assertEqual(self.source.fetch_count, 1)
        self.assertEqual(self.state.scope(self.mailbox.id, "Project  A")["cursor"], "0")
        self.assertEqual(self.state.scope(self.mailbox.id, "Other")["cursor"], "0")
        self.assertEqual(self.state.open_plans(), [])

    def test_missing_provider_time_fails_without_using_header_date(self) -> None:
        self.source.messages["1"] = RemoteMessage("1", raw_mail(), None)
        self.assertEqual(self.run_range().failed, 1)
        self.assertEqual(self.state.open_plans(), [])

    def test_open_plan_blocks_new_rule_evaluation(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        self.run_range()
        self.settings.rules = [Rule("Changed", targets=[RuleTarget(str(self.root / "New"))])]
        self.assertEqual(self.run_range().already_processed, 1)
        self.assertFalse((self.root / "New").exists())
        obstruction.unlink()
        self.service.resume_open()
        self.assertEqual(self.run_range().archived, 1)
        self.assertEqual(len(list((self.root / "New").glob("*.eml"))), 1)

    def test_abort_preserves_success_and_explicit_range_may_retry(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        self.run_range()
        plan_id = self.state.open_plans()[0]["id"]
        failed_path = Path(
            next(
                output["final_path"]
                for output in self.state.outputs(plan_id)
                if output["status"] == "error"
            )
        )
        self.service.abort_plan(plan_id)
        self.assertEqual(self.state.open_plans(), [])
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.service.run_once(self.settings)
        self.assertEqual(self.state.open_plans(), [])
        obstruction.unlink()
        self.run_range()
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.assertEqual(len(list((self.root / "offline" / "archive").glob("*.eml"))), 1)
        self.assertTrue(failed_path.is_file())

    def test_abort_retries_work_copy_cleanup_without_reversing_the_abort(self) -> None:
        obstruction = self.root / "offline"
        obstruction.write_text("unavailable")
        self.rule.targets.append(RuleTarget(str(obstruction / "archive")))
        self.run_range()
        plan = self.state.open_plans()[0]
        raw_path = Path(plan["raw_path"])
        original_unlink = os.unlink

        def fail_raw_cleanup(path, *args, **kwargs):
            if Path(path).name == raw_path.name:
                raise PermissionError("simulated cleanup denial")
            return original_unlink(path, *args, **kwargs)

        with patch("mailarchive.workspace.os.unlink", side_effect=fail_raw_cleanup):
            self.service.abort_plan(plan["id"])
            with self.state.connection() as db:
                stored = db.execute("SELECT status FROM plan WHERE id=?", (plan["id"],)).fetchone()
                active = db.execute(
                    "SELECT count(*) FROM active_message WHERE ref_id=?", (plan["id"],)
                ).fetchone()[0]
            self.assertEqual(stored["status"], "aborted")
            self.assertEqual(active, 0)
            self.assertTrue(raw_path.exists())
            plan_count, usage = self.state.spool_usage()
            self.assertEqual(plan_count, 0)
            self.assertGreater(usage, 0)

        self.assertEqual(self.state.spool_usage(), (0, 0))
        self.assertFalse(raw_path.exists())

    def test_abort_waits_for_execution_and_cannot_split_plan_state(self) -> None:
        obstruction = self.root / "blocked-during-execution"
        obstruction.write_text("not a directory")
        self.rule.targets[0].path = str(obstruction / "archive")
        self.run_range()
        plan_id = self.state.open_plans()[0]["id"]
        obstruction.unlink()

        publication_started = threading.Event()
        allow_publication = threading.Event()
        errors: list[BaseException] = []

        def delayed_write(path, content):
            publication_started.set()
            if not allow_publication.wait(5):
                raise RuntimeError("test publication wait timed out")
            return real_atomic_write(path, content)

        def execute():
            try:
                self.service.engine.execute(plan_id, force=True)
            except BaseException as exc:  # pragma: no cover - assertion reports the thread error
                errors.append(exc)

        second_store = WorkspaceStore(self.state.database_path)

        def abort():
            try:
                second_store.abort_plan(plan_id)
            except BaseException as exc:  # pragma: no cover - assertion reports the thread error
                errors.append(exc)

        with patch("mailarchive.engine._atomic_write", side_effect=delayed_write):
            execution_thread = threading.Thread(target=execute)
            execution_thread.start()
            self.assertTrue(publication_started.wait(5))
            abort_thread = threading.Thread(target=abort)
            abort_thread.start()
            abort_thread.join(0.1)
            self.assertTrue(abort_thread.is_alive())
            allow_publication.set()
            execution_thread.join(5)
            abort_thread.join(5)

        self.assertFalse(execution_thread.is_alive())
        self.assertFalse(abort_thread.is_alive())
        self.assertEqual(errors, [])
        with self.state.connection() as db:
            plan = db.execute("SELECT status FROM plan WHERE id=?", (plan_id,)).fetchone()
            active = db.execute(
                "SELECT count(*) FROM active_message WHERE ref_id=?", (plan_id,)
            ).fetchone()[0]
            receipts = db.execute(
                "SELECT count(*) FROM receipt WHERE message_key=(SELECT message_key FROM plan WHERE id=?)",
                (plan_id,),
            ).fetchone()[0]
        self.assertEqual(plan["status"], "complete")
        self.assertEqual(active, 0)
        self.assertEqual(receipts, 1)

    def test_spool_usage_tolerates_a_concurrently_removed_work_file(self) -> None:
        disappearing = self.state.spool_dir / "disappearing.tmp"
        disappearing.write_bytes(b"temporary")

        class Entry:
            name = disappearing.name

            @staticmethod
            def stat(*, follow_symlinks):
                disappearing.unlink()
                raise FileNotFoundError(disappearing)

        class Entries:
            def __enter__(self):
                return iter([Entry()])

            def __exit__(self, *_args):
                return False

        with patch("mailarchive.workspace.os.scandir", return_value=Entries()):
            self.assertEqual(self.state.spool_usage(), (0, 0))

    def test_spool_usage_does_not_follow_external_symlinks(self) -> None:
        external = self.root / "external-mail.eml"
        external.write_bytes(b"outside the work queue")
        link = self.state.spool_dir / "linked.eml"
        try:
            link.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"Symlinks are unavailable: {exc}")

        self.assertEqual(self.state.spool_usage(), (0, 0))
        self.state.recover()
        self.assertTrue(link.is_symlink())
        self.assertEqual(external.read_bytes(), b"outside the work queue")

    def test_symlinked_work_directory_is_rejected_without_touching_its_target(self) -> None:
        profile = self.root / "unsafe-profile"
        profile.mkdir()
        external = self.root / "external-work"
        external.mkdir()
        victim = external / "victim.eml"
        victim.write_bytes(b"outside")
        try:
            (profile / "work").symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")

        with self.assertRaisesRegex(WorkspaceError, "not a safe directory"):
            WorkspaceStore(profile / "workspace.sqlite3", recover=True)
        self.assertEqual(victim.read_bytes(), b"outside")

    def test_duplicate_attachments_are_distinct_and_repeated_run_is_stable(self) -> None:
        self.source.messages["1"] = RemoteMessage(
            "1",
            raw_mail(attachments=2),
            datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
            "imap_internaldate",
        )
        self.run_range()
        files = list((self.root / "A").rglob("*.bin"))
        self.assertEqual(len(files), 2)
        self.run_range()
        self.assertEqual(len(list((self.root / "A").rglob("*.bin"))), 2)

    def test_baseline_then_imported_old_mail_is_new_discovery(self) -> None:
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)
        self.source.messages["2"] = RemoteMessage(
            "2", raw_mail(), datetime(2000, 1, 1, tzinfo=timezone.utc), "imap_internaldate"
        )
        self.assertEqual(self.service.run_once(self.settings)[0].archived, 1)
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)

    def test_imap_uidvalidity_change_pauses_scope(self) -> None:
        self.service.run_once(self.settings)
        self.source.namespace = imap_scope(
            MailTarget(self.account, self.mailbox, "Project  A"), "2"
        ).processing_namespace
        result = self.service.run_once(self.settings)[0]
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.state.scope(self.mailbox.id, "Project  A")["status"], "paused")
        self.assertEqual(self.service.reset_scope_baseline(self.mailbox.id, "Project  A"), 0)
        self.assertEqual(self.service.run_once(self.settings)[0].skipped_existing, 1)

    def test_interrupted_range_resumes_with_its_original_rule_snapshot(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        key = self.source.namespace + "\0" + "1"
        self.state.reserve(
            self.mailbox.id, key, run_id, automatic=False, scope_key="Project  A", remote_id="1"
        )
        self.state.recover()
        self.settings.rules = [Rule("Changed", targets=[RuleTarget(str(self.root / "New"))])]
        self.assertEqual(self.service.resume_range_run(run_id).archived, 1)
        self.assertEqual(len(list((self.root / "A").glob("*.eml"))), 1)
        self.assertFalse((self.root / "New").exists())

    def test_interrupted_range_resumes_at_saved_provider_page(self) -> None:
        self.source = PagedRangeSource(
            {
                message_id: RemoteMessage(
                    message_id,
                    raw_mail(),
                    datetime(2026, 1, int(message_id), tzinfo=timezone.utc),
                    "imap_internaldate",
                )
                for message_id in ("1", "2")
            }
        )
        self.service = ArchiveService(None, self.state, source_registry=Registry(self.source))

        first = self.run_range()
        run_id = self.state.incomplete_manual_runs()[0]["id"]
        resumed = self.service.resume_range_run(run_id)

        self.assertEqual((first.archived, first.failed), (1, 1))
        self.assertEqual((resumed.archived, resumed.failed), (1, 0))
        self.assertEqual(self.source.enumerated, ["1", "2"])
        self.assertEqual(self.state.incomplete_manual_runs(), [])
        checkpoint = self.state.range_target_checkpoint(run_id, "Project  A")
        self.assertTrue(checkpoint["complete"])
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM plan").fetchone()[0], 2)

    def test_dynamic_range_resume_uses_its_frozen_missing_folder(self) -> None:
        self.mailbox.folders = []
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["old-folder"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        self.state.finish_run(run_id, error="provider page failed")
        newly_discovered = MailTarget(self.account, self.mailbox, "new-folder")

        with patch.object(self.source, "targets", return_value=[newly_discovered]) as targets:
            resumed = self.service.resume_range_run(run_id)

        self.assertEqual((resumed.archived, resumed.failed), (1, 0))
        self.assertIn("old-folder", self.source.folders_seen)
        self.assertNotIn("new-folder", self.source.folders_seen)
        targets.assert_not_called()

    def test_stale_poll_snapshot_does_not_replace_newer_saved_settings(self) -> None:
        stale = deepcopy(self.settings)
        self.state.save_settings(stale)
        current = deepcopy(self.settings)
        current.default_poll_minutes = 17
        self.state.save_settings(current)

        self.service.run_range(stale, {stale.accounts[0].mailboxes[0].id})

        self.assertEqual(self.state.load_settings().default_poll_minutes, 17)
        with self.state.connection() as db:
            run = db.execute(
                "SELECT config_revision, settings_json FROM scan_run ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            revision = db.execute(
                "SELECT payload FROM config_revision WHERE id=?", (run["config_revision"],)
            ).fetchone()
        self.assertEqual(json.loads(run["settings_json"])["default_poll_minutes"], 5)
        self.assertEqual(json.loads(revision["payload"])["default_poll_minutes"], 5)

    def test_run_keeps_exact_revision_when_settings_change_before_start(self) -> None:
        snapshot = deepcopy(self.settings)
        revision = self.state.prepare_run_settings(snapshot)
        current = deepcopy(self.settings)
        current.default_poll_minutes = 23
        self.state.save_settings(current)

        run_id = self.state.start_run(
            snapshot.accounts[0].mailboxes[0].id,
            "manual",
            {"folders": ["Project  A"]},
            snapshot,
            revision,
        )

        self.assertEqual(self.state.load_settings().default_poll_minutes, 23)
        with self.state.connection() as db:
            run = db.execute(
                "SELECT config_revision, settings_json FROM scan_run WHERE id=?", (run_id,)
            ).fetchone()
            stored = db.execute(
                "SELECT payload FROM config_revision WHERE id=?", (run["config_revision"],)
            ).fetchone()
        self.assertEqual(json.loads(run["settings_json"]), json.loads(stored["payload"]))

    def test_processing_history_keeps_completed_plan_and_destination_details(self) -> None:
        self.assertEqual(self.run_range().archived, 1)
        item = self.state.processing_history()[0]
        self.assertEqual(item["item_type"], "plan")
        self.assertEqual(item["status"], "complete")
        self.assertEqual(item["rule_name"], "First")
        self.assertEqual(item["address"], "owner@example.org")
        self.assertEqual(item["received_at"], "2026-01-01T10:00:00+00:00")
        targets = self.state.plan_targets(str(item["id"]))
        self.assertEqual(
            [(row["path"], row["status"]) for row in targets], [(str(self.root / "A"), "done")]
        )
        self.assertEqual(
            self.state.target_outputs(str(item["id"]), targets[0]["target_id"])[0]["status"],
            "done",
        )

    def test_processing_history_pages_every_item_with_equal_timestamps(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id, "manual", {"folders": ["Project  A"]}, self.settings, revision
        )
        for index in range(13):
            intake_id = self.state.reserve(
                self.mailbox.id,
                f"{self.source.namespace}\0history-{index}",
                run_id,
                automatic=False,
                remote_id=str(index),
            )
            self.assertIsNotNone(intake_id)
            self.state.mark_filtered(
                str(intake_id),
                received_at="2026-01-01T10:00:00+00:00",
                received_origin="imap_internaldate",
            )
        with self.state.connection() as db, db:
            db.execute("UPDATE intake SET created_at='2026-01-02T00:00:00+00:00'")

        found = []
        before = None
        while True:
            page = self.state.processing_history(5, before=before)
            if not page:
                break
            found.extend(str(item["history_key"]) for item in page)
            last = page[-1]
            before = (str(last["occurred_at"]), str(last["history_key"]))

        self.assertEqual(len(found), 13)
        self.assertEqual(len(set(found)), 13)

    def test_intake_error_pages_reach_every_unresolved_item(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "automatic",
            {"folders": ["Project  A"]},
            self.settings,
            revision,
        )
        for index in range(105):
            intake_id = self.state.reserve(
                self.mailbox.id,
                f"{self.source.namespace}\0error-{index}",
                run_id,
                automatic=True,
                scope_key="Project  A",
                remote_id=str(index),
            )
            self.assertIsNotNone(intake_id)
            self.assertTrue(self.state.mark_intake_error(str(intake_id), "provider failed"))
        with self.state.connection() as db, db:
            db.execute("UPDATE intake SET created_at='2026-01-02T00:00:00+00:00'")

        found = []
        before = None
        while True:
            page = self.state.intake_errors(40, before=before)
            if not page:
                break
            found.extend(str(item["id"]) for item in page)
            before = (str(page[-1]["created_at"]), str(page[-1]["id"]))

        self.assertEqual(self.state.intake_error_count(), 105)
        first_page, snapshot_total = self.state.intake_error_snapshot(40)
        self.assertEqual(len(first_page), 40)
        self.assertEqual(snapshot_total, 105)
        self.assertEqual(len(found), 105)
        self.assertEqual(len(set(found)), 105)

    def test_folder_spaces_preserved_through_store_and_provider(self) -> None:
        store = ConfigStore(self.root / "profile")
        store.save(self.settings)
        loaded = store.load()
        self.assertEqual(loaded.accounts[0].mailboxes[0].folders, ["Project  A"])
        self.service.run_once(loaded)
        self.assertEqual(self.source.folders_seen, ["Project  A"])

    def test_duplicate_source_is_rejected(self) -> None:
        second = Account(
            "Other",
            host="imap.example.org",
            username="owner@example.org",
            mailboxes=[Mailbox("owner@example.org", folders=["Another"])],
        )
        self.settings.accounts.append(second)
        with self.assertRaises(WorkspaceError):
            self.state.save_settings(self.settings)

    def test_invalid_profile_not_overwritten(self) -> None:
        unknown = self.root / "unknown.sqlite3"
        sqlite3.connect(unknown).close()
        with self.assertRaises(WorkspaceError):
            WorkspaceStore(unknown)

    def test_every_core_runtime_table_is_required(self) -> None:
        for table in (
            "config_revision",
            "source",
            "source_scope",
            "source_message",
            "scan_run",
            "intake",
            "plan",
            "plan_target",
            "active_message",
            "output",
            "output_target",
            "receipt",
            "activity_event",
        ):
            with self.subTest(table=table):
                path = self.root / f"missing-{table}.sqlite3"
                WorkspaceStore(path)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(f"DROP TABLE {table}")
                with self.assertRaisesRegex(WorkspaceError, "incomplete"):
                    WorkspaceStore(path)

    def test_required_schema_constraints_and_foreign_key_integrity_are_checked(self) -> None:
        missing_index = self.root / "missing-index.sqlite3"
        WorkspaceStore(missing_index)
        with closing(sqlite3.connect(missing_index)) as db, db:
            db.execute("DROP INDEX idx_active_config_revision")
        with self.assertRaisesRegex(WorkspaceError, "incomplete"):
            WorkspaceStore(missing_index)

        wrong_partial_index = self.root / "wrong-partial-index.sqlite3"
        WorkspaceStore(wrong_partial_index)
        with closing(sqlite3.connect(wrong_partial_index)) as db, db:
            db.execute("DROP INDEX idx_active_config_revision")
            db.execute(
                "CREATE UNIQUE INDEX idx_active_config_revision "
                "ON config_revision(active) WHERE active=0"
            )
        with self.assertRaisesRegex(WorkspaceError, "incomplete"):
            WorkspaceStore(wrong_partial_index)

        missing_trigger = self.root / "missing-trigger.sqlite3"
        WorkspaceStore(missing_trigger)
        with closing(sqlite3.connect(missing_trigger)) as db, db:
            db.execute("DROP TRIGGER validate_active_message_insert")
        with self.assertRaisesRegex(WorkspaceError, "incomplete"):
            WorkspaceStore(missing_trigger)

        inert_trigger = self.root / "inert-trigger.sqlite3"
        WorkspaceStore(inert_trigger)
        with closing(sqlite3.connect(inert_trigger)) as db, db:
            db.execute("DROP TRIGGER validate_active_message_insert")
            db.execute(
                "CREATE TRIGGER validate_active_message_insert "
                "BEFORE INSERT ON active_message BEGIN SELECT 1; END"
            )
        with self.assertRaisesRegex(WorkspaceError, "incomplete"):
            WorkspaceStore(inert_trigger)

        invalid_reference = self.root / "invalid-reference.sqlite3"
        WorkspaceStore(invalid_reference)
        with closing(sqlite3.connect(invalid_reference)) as db, db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO source_scope(source_id, scope_key) VALUES ('missing', 'INBOX')")
        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(invalid_reference)

    def test_missing_active_revision_is_rejected_instead_of_loading_defaults(self) -> None:
        path = self.root / "missing-active-revision.sqlite3"
        store = WorkspaceStore(path)
        store.save_settings(deepcopy(self.settings))
        with store.connection() as db, db:
            db.execute("UPDATE config_revision SET active=0")

        with self.assertRaisesRegex(WorkspaceError, "settings are damaged"):
            store.load_settings()
        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(path)

    def test_active_configuration_requires_an_exact_source_row(self) -> None:
        path = self.root / "missing-active-source.sqlite3"
        store = WorkspaceStore(path)
        snapshot = deepcopy(self.settings)
        store.save_settings(snapshot)
        with store.connection() as db, db:
            db.execute("DELETE FROM source WHERE id=?", (snapshot.accounts[0].mailboxes[0].id,))

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(path)
        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            store.save_settings(deepcopy(snapshot))

    def test_active_source_identity_and_owner_must_match_the_config_snapshot(self) -> None:
        path = self.root / "mismatched-active-source.sqlite3"
        store = WorkspaceStore(path)
        snapshot = deepcopy(self.settings)
        store.save_settings(snapshot)
        with store.connection() as db, db:
            db.execute(
                "UPDATE source SET account_id='different-owner' WHERE id=?",
                (snapshot.accounts[0].mailboxes[0].id,),
            )

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(path)

    def test_semantically_invalid_range_checkpoint_is_rejected(self) -> None:
        path = self.root / "invalid-range-checkpoint.sqlite3"
        store = WorkspaceStore(path)
        snapshot = deepcopy(self.settings)
        revision = store.prepare_run_settings(snapshot)
        run_id = store.start_run(
            snapshot.accounts[0].mailboxes[0].id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            snapshot,
            revision,
        )
        damaged = {
            "range_targets": {"Project  A": {"namespace": None, "token": None, "complete": True}}
        }
        with store.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET checkpoint=? WHERE id=?",
                (json.dumps(damaged), run_id),
            )

        with self.assertRaisesRegex(WorkspaceError, "checkpoint is damaged"):
            store.range_target_checkpoint(run_id, "Project  A")
        with self.assertRaisesRegex(WorkspaceError, "checkpoint is damaged"):
            WorkspaceStore(path)

    def test_complete_range_checkpoint_must_match_its_frozen_provider_scope(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        damaged = {
            "range_targets": {
                "Project  A": {
                    "namespace": 'imap-v3:["wrong.example",993,"owner@example.org",'
                    '"Project  A","1"]',
                    "token": None,
                    "complete": True,
                }
            }
        }
        with self.state.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET status='failed', checkpoint=? WHERE id=?",
                (json.dumps(damaged), run_id),
            )

        with self.assertRaisesRegex(WorkspaceError, "checkpoint is damaged"):
            self.service.resume_range_run(run_id)
        self.assertEqual(self.source.fetch_count, 0)

    def test_manual_run_cannot_complete_before_every_frozen_target(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )

        self.state.finish_run(run_id)

        with self.state.connection() as db:
            run = db.execute("SELECT status, error FROM scan_run WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["status"], "failed")
        self.assertIn("every frozen target", run["error"])

    def test_run_snapshot_must_equal_its_referenced_config_revision(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        changed = deepcopy(self.settings)
        changed.warn_on_error = not changed.warn_on_error
        with self.state.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET status='failed', settings_json=? WHERE id=?",
                (json.dumps(changed.to_dict(), ensure_ascii=False, sort_keys=True), run_id),
            )

        with self.assertRaisesRegex(WorkspaceError, "snapshot is damaged"):
            self.service.resume_range_run(run_id)
        with self.assertRaisesRegex(WorkspaceError, "snapshot is damaged"):
            WorkspaceStore(self.state.database_path)

    def test_plan_snapshot_and_relational_targets_cannot_diverge(self) -> None:
        obstruction = self.root / "blocked"
        obstruction.write_text("not a directory")
        self.rule.targets[0].path = str(obstruction / "archive")
        self.run_range()
        plan = self.state.open_plans()[0]
        snapshot = json.loads(plan["rule_json"])
        diverted = self.root / "diverted"
        snapshot["rule"]["targets"][0]["path"] = str(diverted)
        with self.state.connection() as db, db:
            db.execute(
                "UPDATE plan SET rule_json=? WHERE id=?",
                (json.dumps(snapshot, ensure_ascii=False), plan["id"]),
            )

        with self.assertRaisesRegex(WorkspaceError, "plan snapshot is damaged"):
            self.service.engine.execute(str(plan["id"]), force=True)
        self.assertFalse(diverted.exists())

    def test_terminal_intake_with_mismatched_source_is_rejected_on_reopen(self) -> None:
        self.settings.rules = []
        self.run_range()
        with self.state.connection() as db, db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("UPDATE intake SET source_id='missing'")

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(self.state.database_path)

    def test_database_rejects_invalid_states_and_dangling_active_references(self) -> None:
        self.state.prepare_run_settings(self.settings)
        with self.state.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO source_scope(source_id, scope_key, status) "
                    "VALUES (?, 'broken', 'invalid')",
                    (self.mailbox.id,),
                )
        with self.state.connection() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "reference is missing"):
                db.execute(
                    "INSERT INTO active_message(source_id, message_key, kind, ref_id) "
                    "VALUES (?, 'message', 'intake', 'missing')",
                    (self.mailbox.id,),
                )

    def test_unresolved_intake_requires_its_active_message_reference(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        key = self.source.namespace + "\0pending"
        intake_id = self.state.reserve(
            self.mailbox.id,
            key,
            run_id,
            automatic=False,
            scope_key="Project  A",
            remote_id="pending",
        )
        with self.state.connection() as db, db:
            db.execute("DELETE FROM active_message WHERE ref_id=?", (intake_id,))

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(self.state.database_path)

    def test_open_plan_requires_its_active_message_reference(self) -> None:
        obstruction = self.root / "blocked"
        obstruction.write_text("not a directory")
        self.rule.targets[0].path = str(obstruction / "archive")
        self.run_range()
        plan = self.state.open_plans()[0]
        with self.state.connection() as db, db:
            db.execute("DELETE FROM active_message WHERE ref_id=?", (plan["id"],))

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(self.state.database_path)

    def test_completed_run_cannot_retain_an_active_intake(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        self.state.reserve(
            self.mailbox.id,
            self.source.namespace + "\0hidden",
            run_id,
            automatic=False,
            scope_key="Project  A",
            remote_id="hidden",
        )
        with self.state.connection() as db, db:
            db.execute("UPDATE scan_run SET status='completed' WHERE id=?", (run_id,))

        with self.assertRaisesRegex(WorkspaceError, "integrity"):
            WorkspaceStore(self.state.database_path)

    def test_accept_plan_requires_the_live_intake_reverse_reference(self) -> None:
        revision = self.state.prepare_run_settings(self.settings)
        run_id = self.state.start_run(
            self.mailbox.id,
            "manual",
            {"folders": ["Project  A"], "start_utc": None, "end_utc": None},
            self.settings,
            revision,
        )
        key = self.source.namespace + "\0pending"
        intake_id = self.state.reserve(
            self.mailbox.id,
            key,
            run_id,
            automatic=False,
            scope_key="Project  A",
            remote_id="pending",
        )
        with self.state.connection() as db, db:
            db.execute("DELETE FROM active_message WHERE ref_id=?", (intake_id,))
        raw_path = self.state.spool_dir / "pending.eml"
        raw_path.write_bytes(raw_mail())
        rule_json = json.dumps(
            {"rule": self.rule.to_dict(), "timezone": self.settings.archive_timezone},
            ensure_ascii=False,
            sort_keys=True,
        )

        with self.assertRaisesRegex(WorkspaceError, "active message reference is damaged"):
            self.state.accept_plan(
                str(intake_id),
                raw_path,
                "digest",
                datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                "imap_internaldate",
                None,
                "subject",
                rule_json,
            )
        with self.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM plan").fetchone()[0], 0)

    def test_resume_plan_requires_the_live_plan_reverse_reference(self) -> None:
        obstruction = self.root / "blocked-resume"
        obstruction.write_text("not a directory")
        self.rule.targets[0].path = str(obstruction / "archive")
        self.run_range()
        plan = self.state.open_plans()[0]
        self.state.pause_plan(plan["id"])
        with self.state.connection() as db, db:
            db.execute("DELETE FROM active_message WHERE ref_id=?", (plan["id"],))

        with self.assertRaisesRegex(WorkspaceError, "active message reference is damaged"):
            self.state.resume_plan(plan["id"])
        with self.state.connection() as db:
            status = db.execute("SELECT status FROM plan WHERE id=?", (plan["id"],)).fetchone()[0]
        self.assertEqual(status, "paused")

    def test_local_day_boundaries_across_dst(self) -> None:
        start, end = local_days_to_utc(date(2026, 3, 29), date(2026, 3, 29), "Europe/Berlin")
        self.assertEqual(start.isoformat(), "2026-03-28T23:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-03-29T22:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
