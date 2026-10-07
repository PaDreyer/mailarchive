"""Destination preparation failures remain isolated and locally retryable."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.domain.configuration import RuleTarget, SaveMode
from tests import test_restart_core as restart
from tests.helpers import sample_mail
from tests.workspace_fixture import WorkspaceStore, make_service


class DestinationPreparationTests(unittest.TestCase):
    def test_legacy_relative_raw_path_is_retained_and_retried_without_migration(self):
        fixture = restart.RestartCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.mailbox.archive_existing_messages = True
        obstruction = fixture.root / "offline"
        obstruction.write_text("not a directory")
        fixture.rule.targets = [RuleTarget(str(obstruction / "archive"), SaveMode.EMAIL_ONLY)]
        self.assertEqual(fixture.service.run_once(fixture.settings)[0].failed, 1)
        plan = fixture.state.open_plans()[0]
        raw = Path(plan["raw_path"])
        relative = os.path.relpath(raw)
        with fixture.state.connection() as db, db:
            db.execute("UPDATE plan SET raw_path=? WHERE id=?", (relative, plan["id"]))
        reopened = WorkspaceStore(Path(os.path.relpath(fixture.state.database_path)), recover=True)
        self.assertTrue(raw.exists())
        self.assertEqual(reopened.open_plans()[0]["raw_path"], relative)
        obstruction.unlink()
        service = make_service(reopened, restart.Registry(fixture.source))
        self.assertEqual(service.retry_activity("mail:" + plan["id"]), (1, 0))
        self.assertFalse(raw.exists())
        self.assertEqual(len(list((obstruction / "archive").glob("*.eml"))), 1)

    def test_failed_path_probes_preserve_healthy_outputs_in_either_target_order(self):
        for probe in ("occupied", "filename_limit"):
            for bad_first in (True, False):
                with self.subTest(probe=probe, bad_first=bad_first):
                    fixture = restart.RestartCoreTests()
                    fixture.setUp()
                    try:
                        self._retry_failed_probe(fixture, probe, bad_first)
                    finally:
                        fixture.tearDown()

    def _retry_failed_probe(self, fixture, probe, bad_first):
        fixture.mailbox.archive_existing_messages = True
        unavailable = fixture.root / "locked"
        healthy = fixture.root / "healthy"
        bad_target = RuleTarget(str(unavailable), SaveMode.EMAIL_ONLY)
        good_target = RuleTarget(str(healthy), SaveMode.EMAIL_ONLY)
        fixture.rule.targets = [bad_target, good_target] if bad_first else [good_target, bad_target]
        files = fixture.service.engine.output_files
        original = getattr(files, probe)

        def guarded(path):
            if path == unavailable or unavailable in path.parents:
                raise PermissionError("Destination is temporarily inaccessible")
            return original(path)

        with patch.object(files, probe, side_effect=guarded):
            result = fixture.service.run_once(fixture.settings)[0]
        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(len(list(healthy.glob("*.eml"))), 1)
        plan = fixture.state.open_plans()[0]
        outputs = [dict(row) for row in fixture.state.outputs(plan["id"])]
        self.assertEqual(sorted(row["status"] for row in outputs), ["done", "error"])
        failed = next(row for row in outputs if row["status"] == "error")
        self.assertEqual(failed["final_path"], "")
        self.assertIn("inaccessible", failed["error"])
        targets = {row["target_id"]: row for row in fixture.state.plan_targets(plan["id"])}
        self.assertEqual(targets[good_target.id]["status"], "done")
        self.assertEqual(targets[bad_target.id]["status"], "error")
        self.assertIn("inaccessible", targets[bad_target.id]["error"])
        existing_files = set(healthy.iterdir())
        with fixture.state.connection() as db:
            receipt = dict(db.execute("SELECT * FROM receipt").fetchone())
        restarted = WorkspaceStore(fixture.state.database_path, recover=True)
        service = make_service(restarted, restart.Registry(fixture.source))
        downloads = fixture.source.fetch_count
        self.assertEqual(service.retry_activity("mail:" + plan["id"]), (1, 0))
        self.assertEqual(fixture.source.fetch_count, downloads)
        self.assertEqual(set(healthy.iterdir()), existing_files)
        self.assertEqual(len(list(unavailable.glob("*.eml"))), 1)
        self.assertEqual(restarted.open_plans(), [])
        retried = {row["id"]: dict(row) for row in restarted.outputs(plan["id"])}
        for old in outputs:
            for field in ("artifact_key", "requested_path", "digest"):
                self.assertEqual(retried[old["id"]][field], old[field])
        self.assertEqual(retried[failed["id"]]["attempts"], 2)
        with restarted.connection() as db:
            unchanged = dict(
                db.execute(
                    "SELECT * FROM receipt WHERE final_path=?", (receipt["final_path"],)
                ).fetchone()
            )
            self.assertEqual(unchanged, receipt)
            self.assertEqual(db.execute("SELECT count(*) FROM output").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 2)

    def test_distinct_requested_outputs_with_same_publication_name_allocate_distinct_paths(self):
        fixture = restart.RestartCoreTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.mailbox.archive_existing_messages = True
        fixture.rule.targets[0].save_mode = SaveMode.ATTACHMENTS_ONLY
        fixture.rule.targets[0].attachments_in_destination = True
        fixture.source.messages["1"].raw = sample_mail(
            attachments=[
                ("a" * 115 + "x.abcdefghij", b"identical"),
                ("a" * 115 + "y.abcdefghij", b"identical"),
            ]
        )
        result = fixture.service.run_once(fixture.settings)[0]
        self.assertEqual((result.archived, result.failed), (1, 0))
        paths = list((fixture.root / "A").iterdir())
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.read_bytes() == b"identical" for path in paths))
        self.assertTrue(all(path.suffix == ".abcdefghij" for path in paths))
        with fixture.state.connection() as db:
            saved = db.execute("SELECT final_path FROM output").fetchall()
        self.assertEqual(len({Path(row["final_path"]) for row in saved}), 2)
