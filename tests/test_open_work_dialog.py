import gc
import json
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from mailarchive.open_work_dialog import OpenWorkDialog


class OpenWorkDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        self.state = Mock()
        self.state.work_plans.return_value = [
            {
                "id": "plan-1",
                "created_at": "2026-09-21T10:00:00+00:00",
                "rule_json": json.dumps({"rule": {"name": "Invoices"}}),
                "status": "open",
                "source_id": "source-1",
                "error": "working copy is damaged",
            }
        ]
        self.state.plan_targets.return_value = [
            {
                "target_id": "target-1",
                "path": "/archive",
                "status": "error",
                "error": "share offline",
            }
        ]
        self.state.outputs.return_value = [
            {"status": "error", "final_path": "/archive/mail.eml", "error": "share offline"}
        ]
        self.state.incomplete_manual_runs.return_value = [
            {
                "id": "run-1",
                "started_at": "2026-09-21T09:00:00+00:00",
                "source_id": "source-1",
                "status": "failed",
                "error": "message vanished",
                "checkpoint": json.dumps({"last_remote_id": "42"}),
                "selection_json": json.dumps({"timezone": "Europe/Berlin"}),
            }
        ]
        intake_errors = [
            {
                "id": "intake-1",
                "created_at": "2026-09-21T09:01:00+00:00",
                "source_id": "source-1",
                "subject": "Invoice",
                "remote_id": "42",
                "error": "message vanished",
            }
        ]
        self.state.intake_errors.return_value = intake_errors
        self.state.intake_error_snapshot.return_value = (intake_errors, 1)
        self.service = Mock()
        self.events = []
        self.refresh_application = Mock()
        self.dialog = OpenWorkDialog(
            self.root,
            self.state,
            self.service,
            self.events.append,
            lambda callback: callback(),
            self.refresh_application,
        )
        self.addCleanup(self.dialog.destroy)

    def test_displays_destination_run_timezone_and_intake_errors(self) -> None:
        self.dialog.work_list.selection_set(0)
        self.dialog.show_plan()
        self.assertIn("working copy is damaged", self.dialog.detail.get())
        self.assertIn("Destination error: /archive", self.dialog.detail.get())
        self.dialog.run_list.selection_set(0)
        self.dialog.show_run()
        self.assertEqual(self.dialog.detail.get(), "message vanished")
        self.assertIn("timezone: Europe/Berlin", self.dialog.run_list.get(0))
        self.dialog.intake_list.selection_set(0)
        self.dialog.show_intake()
        self.assertIn("Message 42", self.dialog.detail.get())
        self.assertIn("message vanished", self.dialog.detail.get())

    def test_pause_and_cancel_actions_reach_the_service(self) -> None:
        self.dialog.work_list.selection_set(0)
        self.dialog.pause_selected()
        self.service.pause_plan.assert_called_once_with("plan-1")
        self.dialog.run_list.selection_set(0)
        with patch("mailarchive.open_work_dialog.messagebox.askyesno", return_value=True):
            self.dialog.cancel_selected_range()
        self.service.cancel_run.assert_called_once_with("run-1")
        self.dialog.intake_list.selection_set(0)
        with patch("mailarchive.open_work_dialog.messagebox.askyesno", return_value=True):
            self.dialog.cancel_selected_intake()
        self.service.cancel_intake.assert_called_once_with("intake-1")
        self.assertGreaterEqual(self.refresh_application.call_count, 2)

    def test_load_more_makes_every_intake_error_reachable(self) -> None:
        first = self.state.intake_errors.return_value[0]
        second = {
            **first,
            "id": "intake-older",
            "created_at": "2026-09-20T09:01:00+00:00",
            "remote_id": "41",
        }
        self.dialog.intakes = [first]
        self.dialog.intake_total = 2
        self.dialog.intake_list.delete(0, "end")
        self.dialog._insert_intakes([first])
        self.dialog._update_intake_controls()
        self.state.intake_errors.reset_mock()
        self.state.intake_errors.return_value = [second]

        self.dialog.load_more_intakes()

        self.state.intake_errors.assert_called_once_with(
            100,
            before=(first["created_at"], first["id"]),
        )
        self.assertEqual([item["id"] for item in self.dialog.intakes], ["intake-1", "intake-older"])
        self.assertEqual(
            self.dialog.intake_summary.get(), "Showing 2 of 2 unresolved intake errors"
        )
        self.assertEqual(str(self.dialog.more_intakes_button["state"]), "disabled")

    def test_newer_errors_do_not_expand_an_active_snapshot(self) -> None:
        first = self.dialog.intakes[0]
        second = {
            **first,
            "id": "intake-older",
            "created_at": "2026-09-20T09:01:00+00:00",
            "remote_id": "41",
        }
        self.dialog.intake_total = 2
        self.dialog._update_intake_controls()
        self.state.intake_errors.return_value = [second]

        self.dialog.load_more_intakes()

        self.assertEqual(len(self.dialog.intakes), 2)
        self.assertEqual(self.dialog.intake_total, 2)
        self.state.intake_error_count.assert_not_called()
        self.assertEqual(str(self.dialog.more_intakes_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
