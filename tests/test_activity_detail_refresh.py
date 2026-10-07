"""A native archive job keeps its inspected attempt across automatic Tk refresh."""

from unittest.mock import patch

from mailarchive.presentation.desktop import DesktopApp
from mailarchive.presentation.window import create_root
from tests import test_execution_outcomes as fixture
from tests.tk_test_case import TkTestCase


class NativeActivityDetailRefreshTests(TkTestCase):
    def test_actual_timer_preserves_completed_output_attempt_and_its_focus(self):
        case = fixture.ExecutionOutcomeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.check()
        self.root = create_root()
        self.addCleanup(self.root.destroy)
        with patch("mailarchive.presentation.desktop.TrayController"):
            desktop = DesktopApp(self.root, case.app)
        case.app.set_observers(desktop.on_service_event, desktop.on_run_progress)
        desktop.show_archive_activity()
        dialog = desktop.activity_dialog
        self.addCleanup(dialog.destroy)
        self.root.update()
        item = case.app.activity_page().items[0]
        detail = case.app.activity_detail(item.key)
        output = detail.mail[0].outputs[0]
        output_row = f"output:{output.output_id}"
        attempt_row = f"{output_row}:attempt:{output.attempts[0].number}"
        dialog.history_tree.selection_set(item.key)
        dialog.history_tree.event_generate("<<TreeviewSelect>>")
        self.root.update()
        tree = dialog.result_tree
        tree.item(output_row, open=True)
        tree.selection_set(attempt_row)
        tree.focus(attempt_row)
        self.root.focus_force()
        tree.focus_set()
        tree.event_generate("<<TreeviewSelect>>")
        self.root.update()
        original_timer = dialog._refresh_timer
        with self.tk_timeout(self.root.quit):

            def refreshed():
                if dialog._refresh_timer != original_timer:
                    self.root.quit()
                else:
                    self.root.after(10, refreshed)

            self.root.after(10, refreshed)
            self.root.mainloop()
        self.assertEqual(case.app.activity_detail(item.key), detail)
        self.assertEqual(dialog.history_tree.selection(), (item.key,))
        self.assertTrue(tree.item(output_row, "open"))
        self.assertEqual(tree.selection(), (attempt_row,))
        self.assertEqual(tree.focus(), attempt_row)
        self.assertIs(self.root.focus_get(), tree)
        self.assertTrue(dialog.open_button.instate(["disabled"]))
        tree.selection_set(output_row)
        tree.event_generate("<<TreeviewSelect>>")
        self.root.update()
        self.assertFalse(dialog.open_button.instate(["disabled"]))
