import gc
import json
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mailarchive.history_dialog import ProcessingHistoryDialog


class ProcessingHistoryDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        self.state = Mock()
        self.state.processing_history.return_value = [
            {
                "item_type": "plan",
                "history_key": "plan:plan-1",
                "id": "plan-1",
                "occurred_at": "2026-09-21T10:00:00+00:00",
                "source_id": "source-1",
                "address": "owner@example.org",
                "provider": "imap",
                "subject": "Invoice",
                "received_at": "2026-09-21T09:59:00+00:00",
                "received_origin": "imap_internaldate",
                "rule_name": "Invoices",
                "rule_json": json.dumps({"rule": {"name": "Invoices"}}),
                "status": "complete",
                "error": None,
                "run_kind": "automatic",
            },
            {
                "item_type": "intake",
                "history_key": "intake:intake-1",
                "id": "intake-1",
                "occurred_at": "2026-09-21T09:00:00+00:00",
                "source_id": "source-1",
                "address": "owner@example.org",
                "provider": "imap",
                "subject": None,
                "received_at": None,
                "received_origin": None,
                "rule_name": None,
                "rule_json": None,
                "status": "error",
                "error": "download failed",
                "run_kind": "manual",
            },
        ]
        self.state.plan_targets.return_value = [
            {
                "target_id": "target-1",
                "path": "/archive",
                "status": "done",
                "error": None,
            }
        ]
        self.state.target_outputs.return_value = [
            {
                "status": "done",
                "final_path": "/archive/invoice.eml",
                "error": None,
            }
        ]
        self.open_directory = Mock()
        self.dialog = ProcessingHistoryDialog(self.root, self.state, self.open_directory)
        self.addCleanup(self.dialog.destroy)

    def test_displays_completed_plan_and_per_destination_details(self) -> None:
        self.dialog.history.selection_set("0")
        self.dialog.show_selected()
        detail = self.dialog.detail.get("1.0", "end")
        self.assertIn("Frozen rule: Invoices", detail)
        self.assertIn("Destination done: /archive", detail)
        self.assertIn("Output done: /archive/invoice.eml", detail)
        self.dialog.open_selected()
        self.open_directory.assert_called_once_with(Path("/archive"))

    def test_displays_intake_failure(self) -> None:
        self.dialog.history.selection_set("1")
        self.dialog.show_selected()
        detail = self.dialog.detail.get("1.0", "end")
        self.assertIn("Status: error", detail)
        self.assertIn("Error: download failed", detail)

    def test_load_more_uses_stable_history_cursor(self) -> None:
        first, second = self.state.processing_history.return_value
        self.state.processing_history.side_effect = [[first, second], [second]]

        with patch("mailarchive.history_dialog.PAGE_SIZE", 1):
            self.dialog.refresh()
            self.assertEqual([item["id"] for item in self.dialog.items], ["plan-1"])
            self.assertEqual(str(self.dialog.more_button["state"]), "normal")
            self.dialog.load_more()

        self.assertEqual([item["id"] for item in self.dialog.items], ["plan-1", "intake-1"])
        self.assertEqual(
            self.state.processing_history.call_args_list[-1].kwargs["before"],
            (first["occurred_at"], first["history_key"]),
        )
        self.assertEqual(str(self.dialog.more_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
