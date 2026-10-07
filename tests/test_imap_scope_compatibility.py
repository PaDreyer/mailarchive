"""INBOX spelling compatibility across durable scopes and frozen range jobs."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.source_identity import MailTarget, imap_scope
from mailarchive.infrastructure.operation_repository import manual_operation_can_retry
from mailarchive.infrastructure.profile_integrity import range_namespace_matches
from tests import test_restart_core as restart
from tests.workspace_fixture import WorkspaceStore


class ImapScopeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = restart.RestartCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.mailbox.folders = ["inbox"]

    def add_mail(self, uid):
        self.fixture.source.messages[uid] = RemoteMessage(
            uid, restart.raw_mail(), datetime(2026, 1, 2, tzinfo=timezone.utc), "imap_internaldate"
        )

    def namespace(self):
        f = self.fixture
        return imap_scope(MailTarget(f.account, f.mailbox, "INBOX"), "1").processing_namespace

    def legacy_scope(self, spelling="inBox"):
        f = self.fixture
        with f.state.connection() as db, db:
            db.execute(
                "UPDATE source_scope SET scope_key=? WHERE source_id=? AND scope_key='INBOX'",
                (spelling, f.mailbox.id),
            )

    def test_case_only_edit_retains_legacy_cursor_and_archives_new_arrivals(self):
        f = self.fixture
        self.assertEqual(f.service.run_once(f.settings)[0].skipped_existing, 1)
        self.legacy_scope()
        self.add_mail("2")
        f.mailbox.folders = ["INBOX"]
        f.state.save_settings(f.settings)
        self.assertEqual(f.state.scope(f.mailbox.id, "INBOX")["cursor"], "1")
        self.assertEqual(
            f.state.source_monitoring_status(f.mailbox.id, f.account.provider, ["iNBoX"]), "active"
        )
        result = f.service.run_once(f.settings)[0]
        self.assertEqual((result.archived, result.skipped_existing, result.failed), (1, 0, 0))
        self.assertEqual(f.state.scope(f.mailbox.id, "inbox")["cursor"], "2")
        with f.state.connection() as db:
            keys = [
                row[0]
                for row in db.execute(
                    "SELECT scope_key FROM source_scope WHERE source_id=?", (f.mailbox.id,)
                )
            ]
        self.assertEqual(keys, ["inBox"])
        # Restart and reapply a rule: the unchanged message identity reuses its receipt.
        f.state = WorkspaceStore(f.state.database_path, recover=True)
        f.service = restart.make_service(f.state, restart.Registry(f.source))
        self.assertEqual(f.service.run_once(f.settings)[0].archived, 0)
        f.service.run_range(f.settings, {f.mailbox.id}, rule_id=f.rule.id)
        self.assertEqual(len(list((f.root / "A").glob("*.eml"))), 2)
        # UID 1 was previously baselined; the explicit range archives it. UID 2
        # keeps its existing receipt, yielding two physical mails rather than three.

    def test_dotless_i_folder_keeps_distinct_scopes_and_receipts_from_inbox(self):
        f = self.fixture
        f.mailbox.folders = ["INBOX", "ınbox"]
        f.mailbox.archive_existing_messages = True
        self.assertEqual(f.service.run_once(f.settings)[0].archived, 2)
        inbox = f.state.scope(f.mailbox.id, "INBOX")
        unicode_scope = f.state.scope(f.mailbox.id, "&ATE-nbox")
        self.assertNotEqual(inbox["processing_namespace"], unicode_scope["processing_namespace"])
        self.assertTrue(
            range_namespace_matches(
                f.account, f.mailbox, "ınbox", unicode_scope["processing_namespace"]
            )
        )
        self.assertFalse(
            range_namespace_matches(f.account, f.mailbox, "ınbox", inbox["processing_namespace"])
        )
        f.mailbox.folders = ["INBOX", "&ATE-nbox"]
        f.state.save_settings(f.settings)
        self.assertEqual(f.service.run_once(f.settings)[0].archived, 0)
        f.service.run_range(f.settings, {f.mailbox.id}, rule_id=f.rule.id)
        self.assertEqual(len(list((f.root / "A").glob("*.eml"))), 2)
        f.state.pause_scope(f.mailbox.id, "inbox", "manual pause")
        f.state.reset_scope_baseline(f.mailbox.id, "iNbOx")
        self.assertEqual(f.state.scope(f.mailbox.id, "&ATE-nbox")["cursor"], "1")
        self.assertEqual(f.state.scope(f.mailbox.id, "&ATE-nbox")["status"], "active")

    def test_mixed_scope_keys_keep_highest_cursor_and_any_existing_pause(self):
        f = self.fixture
        f.mailbox.archive_existing_messages = True
        f.service.run_once(f.settings)
        self.legacy_scope("inbox")
        with f.state.connection() as db, db:
            db.execute(
                "INSERT INTO source_scope(source_id, scope_key, processing_namespace, synchronization_namespace, baseline_done, cursor, status) "
                "SELECT source_id, 'INBOX', processing_namespace, synchronization_namespace, baseline_done, '2', status FROM source_scope "
                "WHERE source_id=? AND scope_key='inbox'",
                (f.mailbox.id,),
            )
        f.mailbox.folders = ["INBOX", "iNbOx"]
        f.state.save_settings(f.settings)
        self.assertEqual(f.state.scope(f.mailbox.id, "INBOX")["cursor"], "2")
        f.state.pause_scope(f.mailbox.id, "iNbOx", "UIDVALIDITY changed")
        self.assertEqual(f.state.scope(f.mailbox.id, "inbox")["status"], "paused")
        self.assertEqual(
            f.state.source_monitoring_status(f.mailbox.id, f.account.provider, f.mailbox.folders),
            "paused",
        )
        f.state.reset_scope_baseline(f.mailbox.id, "InBoX")
        with f.state.connection() as db:
            rows = db.execute(
                "SELECT baseline_done, cursor, status FROM source_scope WHERE source_id=?",
                (f.mailbox.id,),
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(0, None, "new"), (0, None, "new")])

    def test_legacy_pending_intake_survives_discovery_and_is_rechecked_or_reset(self):
        f = self.fixture
        f.service.run_once(f.settings)
        self.legacy_scope("inbox")
        self.add_mail("2")
        with patch.object(
            f.service.engine, "stage", side_effect=OSError("temporary download failure")
        ):
            self.assertEqual(f.service.run_once(f.settings)[0].failed, 1)
        with f.state.connection() as db, db:
            db.execute(
                "UPDATE intake SET scope_key='iNbOx' WHERE source_id=? AND status='error'",
                (f.mailbox.id,),
            )
        f.state.prepare_scope_discovery(f.mailbox.id, {"INBOX"})
        self.assertEqual(f.state.pending_rechecks(f.mailbox.id, "INBOX", force_retry=True), {"2"})
        self.assertEqual(f.state.scope(f.mailbox.id, "INBOX")["cursor"], "2")
        self.assertEqual(f.state.reset_scope_baseline(f.mailbox.id, "inbox"), 1)
        self.assertEqual(f.state.pending_rechecks(f.mailbox.id, "INBOX", force_retry=True), set())
        with f.state.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM active_message").fetchone()[0], 0)

    def test_legacy_pending_download_retries_with_unchanged_message_identity(self):
        f = self.fixture
        f.service.run_once(f.settings)
        self.legacy_scope("InBoX")
        self.add_mail("2")
        with patch.object(f.service.engine, "stage", side_effect=OSError("download interrupted")):
            self.assertEqual(f.service.run_once(f.settings)[0].failed, 1)
        with f.state.connection() as db, db:
            db.execute(
                "UPDATE intake SET scope_key='inbox' WHERE source_id=? AND status='error'",
                (f.mailbox.id,),
            )
        f.mailbox.folders = ["INBOX"]
        f.state.save_settings(f.settings)
        result = f.service.run_once(f.settings, force_retry=True)[0]
        self.assertEqual((result.archived, result.failed), (1, 0))
        self.assertEqual(f.state.pending_rechecks(f.mailbox.id, "INBOX", force_retry=True), set())
        self.assertEqual(len(list((f.root / "A").glob("*.eml"))), 1)

    def manual_run(self):
        f = self.fixture
        revision = f.state.prepare_run_settings(f.settings)
        return f.state.start_run(
            f.mailbox.id, "manual", {"folders": ["inbox", "INBOX"]}, f.settings, revision
        )

    def test_legacy_range_checkpoint_resumes_earliest_unfinished_uid_without_rewriting_snapshot(
        self,
    ):
        f = self.fixture
        run_id = self.manual_run()
        targets = {
            key: {"namespace": self.namespace(), "token": token, "complete": False}
            for key, token in (("inbox", "1"), ("InBoX", "2"))
        }
        with f.state.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET checkpoint=? WHERE id=?",
                (json.dumps({"range_targets": targets}), run_id),
            )
            original_settings = db.execute(
                "SELECT settings_json FROM scan_run WHERE id=?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(f.state.range_target_checkpoint(run_id, "INBOX")["token"], "1")
        f.state = WorkspaceStore(f.state.database_path, recover=True)
        self.assertEqual(f.state.range_target_checkpoint(run_id, "inbox")["token"], "1")
        f.state.restart_run(run_id)
        self.assertTrue(
            f.state.update_range_target_checkpoint(run_id, "inbox", self.namespace(), None, True)
        )
        f.state.finish_run(run_id)
        with f.state.connection() as db:
            row = db.execute(
                "SELECT settings_json, checkpoint, status FROM scan_run WHERE id=?", (run_id,)
            ).fetchone()
        self.assertEqual(row["settings_json"], original_settings)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(set(json.loads(row["checkpoint"])["range_targets"]), {"INBOX"})

    def test_legacy_range_intake_blocks_progress_through_alias(self):
        f = self.fixture
        run_id = self.manual_run()
        intake = f.state.reserve(
            f.mailbox.id,
            self.namespace() + "\0" + "2",
            run_id,
            automatic=False,
            scope_key="inbox",
            remote_id="2",
        )
        self.assertIsNotNone(intake)
        self.assertFalse(
            f.state.update_range_target_checkpoint(run_id, "INBOX", self.namespace(), "2", False)
        )
        f.state.release_intake(intake)
        self.assertTrue(
            f.state.update_range_target_checkpoint(run_id, "iNbOx", self.namespace(), "2", False)
        )

    def test_complete_legacy_checkpoint_with_only_rejections_cannot_retry(self):
        f = self.fixture
        operation_id = f.service.prepare_range_operation(
            f.settings, {f.mailbox.id}, rule_id=f.rule.id
        )
        self.assertTrue(f.state.claim_manual_operation(operation_id))
        f.state.mark_operation_source(operation_id, f.mailbox.id, "running")
        operation = f.state.manual_operation(operation_id)
        run_id = f.state.start_run(
            f.mailbox.id,
            "manual",
            {"folders": ["inbox"]},
            f.settings,
            operation["config_revision"],
            operation_id=operation_id,
        )
        intake = f.state.reserve(
            f.mailbox.id,
            self.namespace() + "\0" + "2",
            run_id,
            automatic=False,
            scope_key="inbox",
            remote_id="2",
        )
        f.state.reject_intake(intake, "message permanently exceeds limit")
        target = {"namespace": self.namespace(), "token": None, "complete": True}
        with f.state.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET checkpoint=? WHERE id=?",
                (json.dumps({"range_targets": {"inbox": target}}), run_id),
            )
        f.state.finish_run(run_id)
        f.state.mark_operation_source(operation_id, f.mailbox.id, "failed", "message rejected")
        f.state.finish_manual_operation(operation_id)
        f.state = WorkspaceStore(f.state.database_path, recover=True)
        with f.state.connection() as db:
            self.assertFalse(manual_operation_can_retry(db, operation_id))
        self.assertFalse(f.state.claim_manual_operation(operation_id))

    def assert_unresolved_alias_retries(self, positions):
        f = self.fixture
        f.mailbox.folders = ["inbox", "INBOX"]
        operation_id = f.service.prepare_range_operation(
            f.settings, {f.mailbox.id}, rule_id=f.rule.id
        )
        self.assertTrue(f.state.claim_manual_operation(operation_id))
        f.state.mark_operation_source(operation_id, f.mailbox.id, "running")
        operation = f.state.manual_operation(operation_id)
        run_id = f.state.start_run(
            f.mailbox.id,
            "manual",
            {"folders": ["inbox", "INBOX"]},
            f.settings,
            operation["config_revision"],
            operation_id=operation_id,
        )
        intake = f.state.reserve(
            f.mailbox.id,
            self.namespace() + "\0" + "1",
            run_id,
            automatic=False,
            scope_key="inbox",
            remote_id="1",
        )
        f.state.mark_intake_error(intake, "Temporary download failure")
        targets = {
            key: {"namespace": self.namespace(), "token": token, "complete": complete}
            for key, token, complete in positions
        }
        with f.state.connection() as db, db:
            db.execute(
                "UPDATE scan_run SET checkpoint=? WHERE id=?",
                (json.dumps({"range_targets": targets}), run_id),
            )
        f.state.finish_run(run_id)
        f.state.mark_operation_source(operation_id, f.mailbox.id, "failed", "download failed")
        f.state.finish_manual_operation(operation_id)
        checkpoint = f.state.range_target_checkpoint(run_id, "INBOX")
        self.assertFalse(checkpoint["complete"])
        self.assertIsNone(checkpoint["token"])
        results = f.service.run_range_operation(operation_id)
        self.assertEqual((results[0].archived, results[0].failed), (1, 0))
        self.assertEqual(f.source.fetch_count, 1)
        self.assertEqual(f.state.manual_operation(operation_id)["status"], "completed")
        with f.state.connection() as db:
            self.assertEqual(
                db.execute("SELECT status FROM intake WHERE id=?", (intake,)).fetchone()[0],
                "accepted",
            )

    def test_mixed_complete_legacy_alias_still_retries_unresolved_intake(self):
        self.assert_unresolved_alias_retries([("inbox", None, False), ("INBOX", None, True)])

    def test_completed_checkpoint_is_reopened_for_unresolved_legacy_alias(self):
        self.assert_unresolved_alias_retries([("INBOX", None, True)])

    def test_incomplete_alias_cursor_cannot_skip_an_older_unresolved_uid(self):
        self.assert_unresolved_alias_retries([("inbox", "1", False), ("INBOX", "2", False)])
