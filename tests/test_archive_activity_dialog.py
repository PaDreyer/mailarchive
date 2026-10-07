from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from mailarchive.application.activity import (
    ActivityDetail,
    ActivityItem,
    ActivityPage,
    MailResult,
    OperationAttempt,
    OutputAttempt,
    OutputResult,
    SourceResult,
)
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.presentation.archive_activity_dialog import ArchiveActivityDialog, _status_label
from tests.tk_test_case import TkTestCase


class ActivityStatusTextTests(unittest.TestCase):
    def test_internal_states_have_clear_display_labels(self) -> None:
        self.assertEqual(_status_label("waiting"), "Waiting to retry")
        self.assertEqual(_status_label("working"), "Processing")
        self.assertEqual(_status_label("done"), "Completed")


class ArchiveActivityDialogTests(TkTestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output_path = Path(self.temporary.name) / "saved.eml"
        self.output_path.write_bytes(b"From: sender@example.com\n\nMail")
        self.operation = ActivityItem(
            key="operation:one",
            kind="operation",
            status="running",
            occurred_at="2026-10-01T10:00:00Z",
            finished_at=None,
            source_id=None,
            address=None,
            subject=None,
            rule_name="Invoices",
            summary="Apply Invoices to past mail",
            can_stop=True,
            mail_count=1,
            completed_outputs=1,
        )
        self.history = ActivityItem(
            key="mail:one",
            kind="mail",
            status="complete",
            occurred_at="2026-09-30T10:00:00Z",
            finished_at="2026-09-30T10:01:00Z",
            source_id="source",
            address="owner@example.com",
            subject="Invoice",
            rule_name="Invoices",
            summary="Invoice",
            completed_outputs=1,
        )
        self.output = OutputResult(
            output_id=1,
            status="done",
            final_path=str(self.output_path),
            requested_path=str(self.output_path),
            error=None,
            completed_at="2026-10-01T10:01:00Z",
            attempts=(),
        )
        mail = MailResult(
            key="mail:one",
            source_id="source",
            address="owner@example.com",
            subject="Invoice",
            status="complete",
            received_at=None,
            rule_name="Invoices",
            error=None,
            outputs=(self.output,),
        )
        self.application = MagicMock()
        self.application.automatic_monitoring_state.return_value = AutomaticMonitoringState.ACTIVE
        self.application.current_jobs.return_value = (self.operation,)
        self.application.activity_page.return_value = ActivityPage((self.history,), None)
        self.application.activity_detail.side_effect = lambda key: ActivityDetail(
            self.operation if key == self.operation.key else self.history, (mail,)
        )
        self.open_output = MagicMock()
        self.dialog = ArchiveActivityDialog(self.root, self.application, self.open_output)
        self.addCleanup(self.dialog.destroy)

    def test_split_lists_show_actual_current_job_and_history(self) -> None:
        self.assertEqual(self.dialog.current_tree.get_children(), (self.operation.key,))
        self.assertEqual(self.dialog.history_tree.get_children(), (self.history.key,))
        self.assertEqual(str(self.dialog.more_button["state"]), "disabled")

    def test_initial_window_and_all_actions_fit_small_screen_at_high_dpi(self):
        self.dialog.destroy()
        scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        self.root.tk.call("tk", "scaling", 192 / 72)
        self.root.deiconify()
        with (
            patch.object(ArchiveActivityDialog, "winfo_screenwidth", return_value=1024),
            patch.object(ArchiveActivityDialog, "winfo_screenheight", return_value=720),
        ):
            self.dialog = ArchiveActivityDialog(self.root, self.application, self.open_output)
        self.root.update()
        self.assertLessEqual(self.dialog.winfo_width(), 976)
        self.assertLessEqual(self.dialog.winfo_height(), 640)
        self.assertGreaterEqual(self.dialog.result_tree.winfo_height(), 80)
        for button in (
            self.dialog.stop_button,
            self.dialog.retry_button,
            self.dialog.open_button,
            self.dialog.more_button,
        ):
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
            self.assertGreaterEqual(button.winfo_rootx(), self.dialog.winfo_rootx())
            self.assertGreaterEqual(button.winfo_rooty(), self.dialog.winfo_rooty())
            self.assertLessEqual(
                button.winfo_rootx() + button.winfo_width(),
                self.dialog.winfo_rootx() + self.dialog.winfo_width(),
            )
            self.assertLessEqual(
                button.winfo_rooty() + button.winfo_height(),
                self.dialog.winfo_rooty() + self.dialog.winfo_height(),
            )

    def test_long_error_summary_scrolls_without_hiding_results_or_losing_selection(self):
        self.dialog.destroy()
        scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        self.root.tk.call("tk", "scaling", 192 / 72)
        self.root.deiconify()
        error = (
            "Folder permission denied. " + "A readable detailed explanation. " * 50 + "END OF ERROR"
        )
        mail = MailResult(
            key="mail:one",
            source_id="source",
            address="owner@example.org",
            subject="Invoice",
            status="complete",
            received_at=None,
            rule_name="Invoices",
            error=None,
            outputs=(self.output,),
        )
        self.application.activity_detail.side_effect = None
        self.application.activity_detail.return_value = ActivityDetail(
            self.operation, (mail,), error=error
        )
        with (
            patch.object(ArchiveActivityDialog, "winfo_screenwidth", return_value=1024),
            patch.object(ArchiveActivityDialog, "winfo_screenheight", return_value=720),
        ):
            self.dialog = ArchiveActivityDialog(self.root, self.application, self.open_output)
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        self.root.update()
        tree = self.dialog.result_tree
        tree.selection_set("output:1")
        tree.see("output:1")
        self.dialog._select_result()
        text = self.dialog.detail_text
        self.assertEqual(text.get("1.0", "end-1c"), self.dialog.detail_summary.get())
        self.assertTrue(text.get("1.0", "end-1c").endswith("END OF ERROR"))
        self.assertLess(text.yview()[1], 1)
        text.yview_moveto(1)
        self.root.update()
        self.assertIsNotNone(text.dlineinfo("end-1c"))
        position = text.yview()
        self.assertGreaterEqual(tree.winfo_height(), 80)
        self.assertTrue(tree.bbox("output:1"))
        self.assertTrue(self.dialog.open_button.instate(["!disabled"]))
        self.assert_complete_row(self.dialog.current_tree, self.operation.key)
        self.assert_complete_row(tree, "output:1")
        self.dialog.history_tree.selection_set(self.history.key)
        self.dialog._select_history()
        self.root.update()
        self.assert_complete_row(self.dialog.history_tree, self.history.key)
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        tree.selection_set("output:1")
        tree.see("output:1")
        self.dialog._select_result()
        text.yview_moveto(1)
        self.root.update()
        position = text.yview()
        self.assertGreaterEqual(
            self.dialog.more_button.winfo_height(), self.dialog.more_button.winfo_reqheight()
        )
        self.dialog.refresh()
        self.root.update()
        self.assertEqual(text.yview(), position)
        self.assertEqual(self.dialog.current_tree.selection(), (self.operation.key,))
        self.assertEqual(self.dialog._selected_output.output_id, self.output.output_id)
        self.assertTrue(self.dialog.open_button.instate(["!disabled"]))

    def assert_complete_row(self, tree, key):
        box = tree.bbox(key)
        self.assertTrue(box)
        _x, y, _width, height = box
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + height, tree.winfo_height())

    def _show_scrolling_job(self, mail_count: int = 40) -> None:
        self.mail_results = tuple(
            MailResult(
                key=f"mail:{index}",
                source_id="source",
                address="owner@example.com",
                subject=f"Invoice {index}",
                status="complete",
                received_at=None,
                rule_name="Invoices",
                error=None,
                outputs=(replace(self.output, output_id=index + 1),),
            )
            for index in range(80)
        )
        self._set_job_mail_count(mail_count)
        self.root.deiconify()
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        self.root.update()

    def _set_job_mail_count(self, mail_count: int) -> None:
        self.application.activity_detail.side_effect = None
        self.application.activity_detail.return_value = ActivityDetail(
            replace(self.operation, mail_count=mail_count), self.mail_results[:mail_count]
        )

    def test_refresh_follows_new_mail_at_bottom_and_keeps_selected_output(self) -> None:
        self._show_scrolling_job()
        tree = self.dialog.result_tree
        tree.selection_set("output:40")
        self.root.update()
        tree.yview_moveto(1)
        self.root.update()
        self.assertGreater(tree.yview()[0], 0)
        self.assertEqual(tree.yview()[1], 1)

        self._set_job_mail_count(60)
        self.dialog._tick()
        self.root.update()

        self.assertEqual(tree.yview()[1], 1)
        self.assertTrue(tree.bbox("output:60"))
        self.assertEqual(self.dialog._selected_output.output_id, 40)
        self.assertTrue(self.dialog.open_button.instate(["!disabled"]))

    def test_scrolling_up_pauses_following_until_returning_to_bottom(self) -> None:
        self._show_scrolling_job()
        tree = self.dialog.result_tree
        tree.yview_moveto(0.25)
        self.root.update()
        first_row_bounds = tree.bbox("mail:10")
        self.assertTrue(first_row_bounds)
        self.assertLess(tree.yview()[1], 1)

        self._set_job_mail_count(60)
        self.dialog.refresh()
        self.root.update()

        self.assertEqual(tree.bbox("mail:10"), first_row_bounds)
        self.assertLess(tree.yview()[1], 1)

        tree.yview_moveto(1)
        self.root.update()
        self._set_job_mail_count(80)
        self.dialog._tick()
        self.root.update()
        self.assertEqual(tree.yview()[1], 1)
        self.assertTrue(tree.bbox("output:80"))

    def test_short_job_keeps_following_when_results_first_overflow(self) -> None:
        self._show_scrolling_job(mail_count=1)
        tree = self.dialog.result_tree
        self.assertEqual(tree.yview(), (0, 1))

        self._set_job_mail_count(40)
        self.dialog._tick()
        self.root.update()

        self.assertGreater(tree.yview()[0], 0)
        self.assertEqual(tree.yview()[1], 1)
        self.assertTrue(tree.bbox("output:40"))

    def test_selecting_different_job_starts_at_top(self) -> None:
        self._show_scrolling_job()
        tree = self.dialog.result_tree
        tree.yview_moveto(1)
        self.root.update()
        self.application.activity_detail.return_value = ActivityDetail(
            self.history, self.mail_results
        )

        self.dialog.history_tree.selection_set(self.history.key)
        self.dialog._select_history()
        self.root.update()

        self.assertEqual(tree.yview()[0], 0)
        self.assertTrue(tree.bbox("mail:0"))
        self.assertLess(tree.yview()[1], 1)

    def test_stop_is_scoped_to_selected_operation(self) -> None:
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        with patch(
            "mailarchive.presentation.archive_activity_dialog.messagebox.askyesno",
            return_value=True,
        ):
            self.dialog.stop_selected()
        self.application.stop_operation.assert_called_once_with(self.operation.key)

    def test_detail_identity_preserves_selection_focus_expansion_and_scroll_after_new_results(self):
        self._show_scrolling_job(40)
        attempt = OutputAttempt(
            1, "error", "", "2026-10-01T10:00:00Z", "2026-10-01T10:01:00Z", "Disk unavailable"
        )
        selected_mail = self.mail_results[19]
        output = replace(
            selected_mail.outputs[0],
            status="error",
            final_path="",
            completed_at=None,
            error="Disk unavailable",
            attempts=(attempt,),
        )
        self.mail_results = tuple(
            replace(mail, status="failed", error="Disk unavailable", outputs=(output,))
            if mail.key == selected_mail.key
            else mail
            for mail in self.mail_results
        )
        self._set_job_mail_count(40)
        self.dialog.refresh()
        tree = self.dialog.result_tree
        tree.item("output:20", open=True)
        tree.item("mail:8", open=False)
        selected = "output:20:attempt:1"
        tree.selection_set(selected)
        tree.focus(selected)
        tree.event_generate("<<TreeviewSelect>>")
        self.root.focus_force()
        tree.focus_set()
        self.root.update()
        tree.yview_moveto(0.25)
        tree.xview_moveto(0.1)
        self.root.update()
        anchor = tree.identify_row(30)
        position = tree.bbox(anchor)
        horizontal = tree.xview()
        next_attempt = replace(
            attempt, number=2, status="done", final_path=str(self.output_path), error=None
        )
        completed = replace(
            output,
            status="done",
            final_path=str(self.output_path),
            completed_at=next_attempt.finished_at,
            error=None,
            attempts=(attempt, next_attempt),
        )
        mail = replace(selected_mail, outputs=(completed,))
        inserted = replace(self.mail_results[0], key="mail:new", outputs=())
        changed = tuple(mail if item.key == mail.key else item for item in self.mail_results[:40])
        self.application.activity_detail.return_value = ActivityDetail(
            replace(self.operation, mail_count=41), (inserted, *changed)
        )
        self.dialog.refresh()
        self.root.update()
        self.assertEqual(tree.selection(), (selected,))
        self.assertEqual(tree.focus(), selected)
        self.assertIs(self.root.focus_get(), tree)
        self.assertTrue(tree.item("output:20", "open"))
        self.assertFalse(tree.item("mail:8", "open"))
        self.assertTrue(tree.exists("output:20:attempt:2"))
        self.assertEqual(tree.bbox(anchor), position)
        self.assertEqual(tree.xview(), horizontal)
        self.assertTrue(self.dialog.open_button.instate(["disabled"]))

    def test_scan_source_and_archived_detail_keys_survive_reordered_projections(self):
        sources = (
            SourceResult("one", "one@example.org", "completed", None),
            SourceResult("two", "two@example.org", "completed", None),
        )
        attempts = (OperationAttempt(1, "start", "end", "completed", None, sources),)
        output = replace(self.output, previously_archived=True)
        mail = MailResult(
            "mail:one",
            "one",
            "one@example.org",
            "Invoice",
            "complete",
            None,
            "Invoices",
            None,
            (output,),
        )
        self.application.activity_detail.side_effect = None
        self.application.activity_detail.return_value = ActivityDetail(
            self.operation, (mail,), sources=sources, attempts=attempts
        )
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        tree = self.dialog.result_tree
        tree.item("attempt:1", open=True)
        tree.item("group:sources", open=False)
        for selected in ("attempt:1:source:two", "output:1:archived"):
            with self.subTest(selected=selected):
                tree.selection_set(selected)
                tree.focus(selected)
                tree.event_generate("<<TreeviewSelect>>")
                self.root.update()
                reordered = tuple(reversed(sources))
                self.application.activity_detail.return_value = ActivityDetail(
                    self.operation,
                    (mail,),
                    sources=reordered,
                    attempts=(
                        replace(attempts[0], sources=reordered),
                        OperationAttempt(2, "start2", None, "running", None, reordered),
                    ),
                )
                self.dialog.refresh()
                self.root.update()
                self.assertEqual(tree.selection(), (selected,))
                self.assertEqual(tree.focus(), selected)
                self.assertTrue(tree.item("attempt:1", "open"))
                self.assertFalse(tree.item("group:sources", "open"))
                self.assertTrue(tree.exists("attempt:2:source:two"))
                self.assertTrue(self.dialog.open_button.instate(["disabled"]))

    def test_detail_state_resets_for_other_jobs_and_colliding_keys_in_other_profiles(self):
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        tree = self.dialog.result_tree
        tree.item("output:1", open=True)
        tree.selection_set("output:1")
        tree.focus("output:1")
        self.dialog._select_result()
        self.assertFalse(self.dialog.open_button.instate(["disabled"]))
        self.dialog.history_tree.selection_set(self.history.key)
        self.dialog._select_history()
        self.assertFalse(tree.item("output:1", "open"))
        self.assertEqual(tree.selection(), ())
        self.assertEqual(tree.focus(), "")
        tree.item("output:1", open=True)
        tree.selection_set("output:1")
        self.application.database_path = Path(self.temporary.name) / "new.sqlite3"
        self.dialog.refresh()
        self.dialog.history_tree.selection_set(self.history.key)
        self.dialog._select_history()
        self.assertFalse(tree.item("output:1", "open"))
        self.assertEqual(tree.selection(), ())
        self.assertEqual(tree.focus(), "")
        self.assertTrue(self.dialog.open_button.instate(["disabled"]))

    def test_same_job_detail_state_survives_unavailable_and_transient_read_failure(self):
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        tree = self.dialog.result_tree
        for unavailable in (True, False):
            with self.subTest(unavailable=unavailable):
                tree.item("output:1", open=True)
                tree.selection_set("output:1")
                tree.focus("output:1")
                self.dialog._select_result()
                original = self.application.activity_detail.side_effect
                if unavailable:
                    self.application.automatic_monitoring_state.return_value = (
                        AutomaticMonitoringState.UNAVAILABLE
                    )
                else:
                    self.application.activity_detail.side_effect = RuntimeError(
                        "Temporary read failure"
                    )
                self.dialog.refresh()
                self.assertEqual(tree.get_children(), ())
                self.assertTrue(self.dialog.open_button.instate(["disabled"]))
                self.application.automatic_monitoring_state.return_value = (
                    AutomaticMonitoringState.ACTIVE
                )
                self.application.activity_detail.side_effect = original
                self.dialog.refresh()
                self.root.update()
                self.assertTrue(tree.item("output:1", "open"))
                self.assertEqual(tree.selection(), ("output:1",))
                self.assertEqual(tree.focus(), "output:1")
                self.assertFalse(self.dialog.open_button.instate(["disabled"]))

    def test_completion_moves_selection_and_output_to_history(self) -> None:
        self._show_scrolling_job()
        tree = self.dialog.result_tree
        tree.selection_set("output:40")
        self.dialog._select_result()
        tree.yview_moveto(1)
        self.root.update()
        completed = replace(self.operation, status="complete", can_stop=False)
        self.application.current_jobs.return_value = ()
        self.application.activity_page.return_value = ActivityPage((completed, self.history), None)
        self.application.activity_detail.return_value = replace(
            self.application.activity_detail.return_value, item=completed
        )

        self.dialog._tick()
        self.root.update()

        self.assertEqual(self.dialog.history_tree.selection(), (completed.key,))
        self.assertEqual(self.dialog._detail_key, completed.key)
        self.assertIn("Completed", self.dialog.detail_summary.get())
        self.assertEqual(self.dialog._selected_output.output_id, 40)
        self.assertEqual(tree.yview()[1], 1)
        self.assertFalse(self.dialog.open_button.instate(["disabled"]))

    def test_retry_moves_selection_to_current_without_discarding_loaded_history(self) -> None:
        older = replace(self.history, key="mail:older", occurred_at="2026-09-29T10:00:00Z")
        failed = replace(self.operation, status="failed", can_stop=False, can_retry=True)
        self.application.current_jobs.return_value = ()
        self.application.activity_page.side_effect = [
            ActivityPage((failed, self.history), (self.history.occurred_at, self.history.key)),
            ActivityPage((older,), None),
        ]
        self.dialog.refresh()
        self.dialog.load_more()
        self.dialog.history_tree.selection_set(failed.key)
        self.dialog._select_history()
        self.dialog.result_tree.selection_set("output:1")
        self.dialog._select_result()
        self.application.current_jobs.return_value = (self.operation,)
        self.application.activity_page.side_effect = None
        self.application.activity_page.return_value = ActivityPage((self.history, older), None)

        self.dialog.retry_selected()
        self.root.update()

        self.application.retry_activity.assert_called_once_with(failed.key)
        self.assertEqual(self.dialog.current_tree.selection(), (failed.key,))
        self.assertEqual(self.dialog.history_items, [self.history, older])
        self.assertEqual(self.dialog._history_floor, (older.occurred_at, older.key))
        self.assertEqual(self.dialog._selected_output.output_id, 1)
        self.assertEqual(self.dialog._detail_key, failed.key)

    def test_completion_below_loaded_floor_is_selected_without_skipping_history(self) -> None:
        operation = replace(self.operation, occurred_at="2026-09-20T10:00:00Z")
        self.application.current_jobs.return_value = (operation,)
        cursor = self.history.occurred_at, self.history.key
        self.application.activity_page.return_value = ActivityPage((self.history,), cursor)
        self.dialog.refresh()
        self.dialog.current_tree.selection_set(operation.key)
        self.dialog._select_current()
        self.dialog.result_tree.selection_set("output:1")
        self.dialog._select_result()
        completed = replace(operation, status="complete", can_stop=False)
        detail = self.application.activity_detail(operation.key)
        self.application.activity_detail.side_effect = None
        self.application.activity_detail.return_value = replace(detail, item=completed)
        self.application.current_jobs.return_value = ()

        self.dialog._tick()

        self.assertEqual(self.dialog.history_tree.selection(), (operation.key,))
        self.assertEqual(self.dialog._history_floor, cursor)
        self.assertEqual(self.dialog._next_before, cursor)
        self.assertEqual(self.dialog._selected_output.output_id, 1)
        self.application.activity_page.return_value = ActivityPage((completed,), None)
        self.dialog.load_more()
        self.assertEqual(self.dialog.history_tree.get_children().count(operation.key), 1)

    def test_profile_switch_does_not_restore_a_previous_profiles_selection(self) -> None:
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        self.dialog.result_tree.selection_set("output:1")
        self.dialog._select_result()
        self.application.database_path = Path(self.temporary.name) / "other.sqlite3"
        self.dialog._tick()
        self.root.update()
        self.assertFalse(self.dialog.current_tree.selection())
        self.assertFalse(self.dialog.history_tree.selection())
        self.assertIsNone(self.dialog._detail_key)
        self.assertIsNone(self.dialog._selected_output)

    def test_only_selected_completed_output_can_be_opened(self) -> None:
        self.dialog.history_tree.selection_set(self.history.key)
        self.dialog._select_history()
        output_row = next(row for row in self.dialog.result_tree.get_children("mail:one"))
        self.dialog.result_tree.selection_set(output_row)
        self.dialog._select_result()
        self.dialog.open_selected()
        self.open_output.assert_called_once_with(self.output_path)

    def test_reused_receipt_is_labeled_previously_archived(self) -> None:
        reused = replace(self.output, previously_archived=True)
        item = replace(
            self.history,
            completed_outputs=0,
            previously_archived_outputs=1,
            summary="0 outputs saved, 1 previously archived, 0 failed",
        )
        mail = MailResult(
            "mail:one",
            "source",
            "owner@example.com",
            "Invoice",
            "complete",
            None,
            "Invoices",
            None,
            (reused,),
        )
        self.application.activity_page.return_value = ActivityPage((item,), None)
        self.application.activity_detail.return_value = ActivityDetail(item, (mail,))
        self.application.activity_detail.side_effect = None
        self.dialog.refresh()
        self.dialog.history_tree.selection_set(item.key)
        self.dialog._select_history()
        row = self.dialog.result_tree.get_children("mail:one")[0]
        self.assertEqual(self.dialog.result_tree.item(row, "text"), "Previously archived")
        self.assertEqual(self.dialog.result_tree.set(row, "status"), "Previously archived")
        archive_time = self.dialog.result_tree.get_children(row)[0]
        self.assertIn(reused.completed_at, self.dialog.result_tree.item(archive_time, "text"))
        self.assertIn("1 previously archived outputs", self.dialog.detail_summary.get())

    def test_history_uses_keyset_pagination(self) -> None:
        older = ActivityItem(
            key="mail:older",
            kind="mail",
            status="failed",
            occurred_at="2026-09-29T10:00:00Z",
            finished_at=None,
            source_id="source",
            address="owner@example.com",
            subject="Older",
            rule_name="Invoices",
            summary="Older failed mail",
            can_retry=True,
        )
        self.application.activity_page.side_effect = [
            ActivityPage((self.history,), (self.history.occurred_at, self.history.key)),
            ActivityPage((older,), None),
        ]
        self.dialog.refresh()
        self.dialog.load_more()
        self.assertEqual(self.dialog.history_tree.get_children(), (self.history.key, older.key))
        self.application.activity_page.assert_called_with(
            before=(self.history.occurred_at, self.history.key), limit=100
        )

    def test_automatic_history_refresh_keeps_loaded_pages_and_scroll_position(self) -> None:
        created = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
        items = tuple(
            replace(
                self.history,
                key=f"mail:{index:03d}",
                occurred_at=(created - timedelta(seconds=index)).isoformat(),
            )
            for index in range(200)
        )
        self.application.activity_page.side_effect = [
            ActivityPage(items[:100], (items[99].occurred_at, items[99].key)),
            ActivityPage(items[100:], (items[-1].occurred_at, items[-1].key)),
        ]
        self.dialog.refresh()
        self.dialog.load_more()
        self.root.deiconify()
        self.root.update()
        self.dialog.history_tree.yview_moveto(0.25)
        self.root.update()
        previous_bounds = self.dialog.history_tree.bbox(items[50].key)
        self.assertTrue(previous_bounds)
        self.dialog.history_tree.selection_set(items[60].key)
        added = replace(
            self.history, key="mail:new", occurred_at=(created + timedelta(seconds=1)).isoformat()
        )
        self.application.activity_page.side_effect = [
            ActivityPage((added, *items[:-1]), (items[-2].occurred_at, items[-2].key)),
            ActivityPage((items[-1],), None),
        ]
        self.dialog._tick()
        self.root.update()
        self.application.activity_page.assert_any_call(limit=200)
        self.assertEqual(len(self.dialog.history_items), 201)
        self.assertEqual(self.dialog.history_items[0], added)
        self.assertEqual(self.dialog.history_tree.selection(), (items[60].key,))
        self.assertEqual(self.dialog.history_tree.bbox(items[50].key), previous_bounds)

    def test_new_history_keeps_selected_tail_and_does_not_expand_the_lower_boundary(self) -> None:
        created = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
        items = tuple(
            replace(
                self.history,
                key=f"mail:{index:03d}",
                occurred_at=(created - timedelta(seconds=index)).isoformat(),
            )
            for index in range(250)
        )
        self.application.activity_page.side_effect = [
            ActivityPage(items[:100], (items[99].occurred_at, items[99].key)),
            ActivityPage(items[100:200], (items[199].occurred_at, items[199].key)),
        ]
        self.dialog.refresh()
        self.dialog.load_more()
        self.dialog.history_tree.selection_set(items[199].key)
        self.dialog._select_history()
        mail = self.application.activity_detail(self.history.key).mail[0]
        self.dialog.result_tree.selection_set("output:1")
        self.dialog._select_result()
        self.application.activity_detail.side_effect = lambda key: ActivityDetail(
            next(item for item in items if item.key == key), (replace(mail, key=key),)
        )
        added = replace(
            self.history, key="mail:new", occurred_at=(created + timedelta(seconds=1)).isoformat()
        )
        self.application.activity_page.side_effect = [
            ActivityPage((added, *items[:199]), (items[198].occurred_at, items[198].key)),
            ActivityPage(items[199:], None),
            ActivityPage((added, *items[:200]), (items[199].occurred_at, items[199].key)),
        ]
        for _ in range(2):
            self.dialog._tick()
            self.assertEqual(len(self.dialog.history_items), 201)
            self.assertEqual(self.dialog.history_tree.selection(), (items[199].key,))
            self.assertEqual(self.dialog._detail_key, items[199].key)
            self.assertEqual(self.dialog._next_before, (items[199].occurred_at, items[199].key))
            self.assertEqual(self.dialog._history_floor, (items[199].occurred_at, items[199].key))
            self.assertEqual(self.dialog._selected_output.output_id, self.output.output_id)
            self.assertFalse(self.dialog.open_button.instate(["disabled"]))

    def test_profile_change_resets_pagination_before_loading_more(self) -> None:
        self.dialog._next_before = (self.history.occurred_at, self.history.key)
        self.application.database_path = Path(self.temporary.name) / "new.sqlite3"
        self.application.current_jobs.return_value = ()
        self.application.activity_page.return_value = ActivityPage((), None)
        self.application.activity_page.reset_mock()
        self.dialog.load_more()
        self.application.activity_page.assert_called_once_with(limit=100)
        self.assertEqual(self.dialog.history_items, [])
        self.assertEqual(self.dialog.current_items, ())
        self.assertIsNone(self.dialog._next_before)

    def test_operation_failure_source_and_retry_attempts_are_visible(self) -> None:
        source = SourceResult("source", "owner@example.com", "failed", "Access denied")
        attempt = OperationAttempt(
            1,
            "2026-10-02T09:00:00Z",
            "2026-10-02T09:01:00Z",
            "failed",
            "Mailbox scan failed",
            (source,),
        )
        output_attempt = OutputAttempt(
            1,
            "error",
            "",
            "2026-10-02T09:02:00Z",
            "2026-10-02T09:02:01Z",
            "Share unavailable",
        )
        output = replace(self.output, attempts=(output_attempt,))
        mail = MailResult(
            "mail:one",
            "source",
            "owner@example.com",
            "Invoice",
            "failed",
            None,
            "Invoices",
            "Share unavailable",
            (output,),
        )
        self.application.activity_detail.side_effect = lambda key: ActivityDetail(
            self.operation, (mail,), "Mailbox scan failed", (source,), (attempt,)
        )
        self.dialog.current_tree.selection_set(self.operation.key)
        self.dialog._select_current()
        self.assertIn("Mailbox scan failed", self.dialog.detail_summary.get())
        source_group = next(
            row
            for row in self.dialog.result_tree.get_children()
            if self.dialog.result_tree.item(row, "text") == "Selected mailboxes"
        )
        self.assertIn("source:source", self.dialog.result_tree.get_children(source_group))
        self.assertEqual(self.dialog.result_tree.set("source:source", "error"), "Access denied")
        self.assertEqual(self.dialog.result_tree.set("attempt:1", "error"), "Mailbox scan failed")
        self.assertEqual(
            self.dialog.result_tree.set("output:1:attempt:1", "error"),
            "Share unavailable",
        )


if __name__ == "__main__":
    unittest.main()
