import gc
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from mailarchive.domain.configuration import Rule, RuleTarget, SaveMode
from mailarchive.presentation.dialogs import RuleDialog


class RuleDestinationsTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.addCleanup(lambda: self.assertEqual(self.callback_errors, []))
        self.root.geometry("980x640+20+20")
        self.root.update()
        self.archive = str(Path.cwd() / "archive")

    def dialog(self, rule: Rule | None = None) -> RuleDialog:
        dialog = RuleDialog(self.root, rule=rule)
        dialog.focus_force()
        self.root.update()
        return dialog

    def test_new_rule_starts_with_one_neutral_destination(self) -> None:
        dialog = self.dialog()
        editor = dialog.destinations
        block = editor.blocks[0]
        self.assertEqual(editor.cget("text"), "Destinations")
        self.assertEqual(len(editor.blocks), 1)
        self.assertEqual(block.path_var.get(), "")
        self.assertEqual(block.preview_var.get(), "Enter a destination path.")
        self.assertEqual(block.save_var.get(), "Email and attachments")
        self.assertFalse(block.attachments_in_destination_var.get())
        self.assertTrue(block.remove_button.instate(["disabled"]))
        block.remove_button.invoke()
        self.assertEqual(len(editor.blocks), 1)

    def test_add_edit_remove_first_and_save_preserves_target_identity(self) -> None:
        rule = Rule(
            "Invoices",
            id="rule-id",
            targets=[
                RuleTarget(self.archive, SaveMode.EMAIL_ONLY, id="first"),
                RuleTarget(self.archive + "/files", SaveMode.ATTACHMENTS_ONLY, True, id="second"),
            ],
        )
        before = rule.to_dict()
        dialog = self.dialog(rule)
        editor = dialog.destinations
        editor.add_button.invoke()
        added = editor.blocks[2]
        self.assertIs(dialog.focus_get(), added.path_entry)
        added.path_var.set(self.archive + "/{year}/{month}")
        added.save_var.set("Email and attachments")
        new_id = added.target_id
        editor.blocks[0].remove_button.invoke()
        editor.blocks[0].path_var.set(self.archive + "/updated")
        self.root.update()
        self.assertEqual(
            [block.cget("text") for block in editor.blocks],
            ["Destination 1", "Destination 2"],
        )
        self.assertEqual(rule.to_dict(), before)
        dialog.save_button.invoke()
        result = dialog.result
        self.assertEqual(result.id, "rule-id")
        self.assertEqual([target.id for target in result.targets], ["second", new_id])
        self.assertEqual(result.targets[0].path, self.archive + "/updated")
        self.assertEqual(result.targets[0].save_mode, SaveMode.ATTACHMENTS_ONLY)
        self.assertTrue(result.targets[0].attachments_in_destination)
        self.assertEqual(result.targets[1].path, self.archive + "/{year}/{month}")
        self.assertEqual(rule.to_dict(), before)

    def test_cancel_discards_edits_additions_and_removals(self) -> None:
        rule = Rule(
            "Invoices",
            targets=[RuleTarget(self.archive), RuleTarget(self.archive + "/files")],
        )
        before = rule.to_dict()
        dialog = self.dialog(rule)
        editor = dialog.destinations
        editor.blocks[1].path_var.set(self.archive + "/changed")
        editor.blocks[0].remove_button.invoke()
        editor.add_button.invoke()
        dialog.cancel_button.invoke()
        self.assertIsNone(dialog.result)
        self.assertEqual(rule.to_dict(), before)

    def test_attachment_options_and_previews_are_independent(self) -> None:
        dialog = self.dialog()
        editor = dialog.destinations
        editor.add()
        first, second = editor.blocks
        first.path_var.set(self.archive + "/{year}/{month}")
        self.assertEqual(first.preview_var.get(), str(Path(self.archive) / "YYYY" / "MM"))
        second.path_var.set(self.archive + "/files")
        second.attachments_in_destination_var.set(True)
        second.save_var.set("Email only (.eml)")
        self.assertTrue(second.attachments_in_destination_box.instate(["disabled"]))
        self.assertFalse(first.attachments_in_destination_box.instate(["disabled"]))
        second.save_var.set("Attachments only")
        self.assertFalse(second.attachments_in_destination_box.instate(["disabled"]))
        self.assertTrue(second.attachments_in_destination_var.get())
        dialog.name_var.set("Invoices")
        dialog.save_button.invoke()
        self.assertFalse(dialog.result.targets[0].attachments_in_destination)
        self.assertTrue(dialog.result.targets[1].attachments_in_destination)

    def test_many_destinations_scroll_and_invalid_last_path_gets_focus(self) -> None:
        rule = Rule(
            "Invoices",
            targets=[RuleTarget(self.archive + f"/{index}") for index in range(12)],
        )
        before = rule.to_dict()
        with patch.object(RuleDialog, "winfo_screenheight", return_value=716):
            dialog = self.dialog(rule)
            editor = dialog.destinations
            last = editor.blocks[-1]
            last.path_var.set("relative")
            editor.canvas.yview_moveto(0)
            self.root.update()
            self.assertLess(editor.canvas.yview()[1], 1)
            self.assertLessEqual(dialog.winfo_height(), 668)
            for button in (editor.add_button, dialog.save_button, dialog.cancel_button):
                bottom = button.winfo_rooty() + button.winfo_height()
                self.assertLessEqual(bottom, dialog.winfo_rooty() + dialog.winfo_height())
            with patch("mailarchive.presentation.dialogs.messagebox.showerror") as error:
                dialog.save_button.invoke()
            self.root.update()
            self.assertIn("Destination 12", error.call_args.args[1])
            self.assertIs(dialog.focus_get(), last.path_entry)
            self.assertGreater(editor.canvas.yview()[0], 0)
            self.assertGreaterEqual(last.path_entry.winfo_rooty(), editor.canvas.winfo_rooty())
            self.assertLessEqual(
                last.path_entry.winfo_rooty() + last.path_entry.winfo_height(),
                editor.canvas.winfo_rooty() + editor.canvas.winfo_height(),
            )
            self.assertIsNone(dialog.result)
            self.assertEqual(rule.to_dict(), before)

    def test_mousewheel_and_keyboard_focus_scroll_destination_blocks(self) -> None:
        dialog = self.dialog(Rule("Invoices", targets=[RuleTarget(self.archive) for _ in range(8)]))
        editor = dialog.destinations
        editor.blocks[0].path_entry.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(editor.canvas.yview()[0], 0)
        editor.canvas.yview_moveto(0)
        last = editor.blocks[-1]
        last.path_entry.focus_set()
        self.root.update()
        self.assertGreater(editor.canvas.yview()[0], 0)
        editor.blocks[0].path_entry.focus_set()
        self.root.update()
        self.assertLess(editor.canvas.yview()[0], 0.02)

    def test_last_remaining_destination_cannot_be_removed(self) -> None:
        dialog = self.dialog()
        editor = dialog.destinations
        first_id = editor.blocks[0].target_id
        editor.add()
        remaining = editor.blocks[1]
        editor.remove(editor.blocks[0])
        self.assertNotEqual(remaining.target_id, first_id)
        self.assertTrue(remaining.remove_button.instate(["disabled"]))
        editor.remove(remaining)
        self.assertEqual(editor.blocks, [remaining])

    def test_adding_and_removing_destinations_preserves_window_and_viewport_size(self) -> None:
        with patch.object(RuleDialog, "winfo_screenheight", return_value=1600):
            dialog = self.dialog()
            editor = dialog.destinations
            geometry = dialog.geometry()
            viewport_height = editor.canvas.winfo_height()
            for _ in range(8):
                editor.add_button.invoke()
                self.root.update()
                self.assertEqual(dialog.geometry(), geometry)
                self.assertEqual(editor.canvas.winfo_height(), viewport_height)
                self.assertLess(editor.canvas.yview()[1] - editor.canvas.yview()[0], 1)
                self.assertIs(dialog.focus_get(), editor.blocks[-1].path_entry)
            editor.blocks[-1].path_var.set(self.archive + "/Long path component" * 40)
            self.root.update()
            self.assertEqual(dialog.geometry(), geometry)
            self.assertEqual(editor.canvas.winfo_height(), viewport_height)
            while len(editor.blocks) > 1:
                editor.blocks[0].remove_button.invoke()
                self.root.update()
                self.assertEqual(dialog.geometry(), geometry)
                self.assertEqual(editor.canvas.winfo_height(), viewport_height)

    def test_existing_multiple_destinations_open_at_the_same_size_as_one_destination(self) -> None:
        with patch.object(RuleDialog, "winfo_screenheight", return_value=1600):
            single = self.dialog(Rule("Invoices", targets=[RuleTarget(self.archive)]))
            size = single.winfo_width(), single.winfo_height()
            viewport_height = single.destinations.canvas.winfo_height()
            single.destroy()
            multiple = self.dialog(
                Rule("Invoices", targets=[RuleTarget(self.archive) for _ in range(12)])
            )
            self.assertEqual((multiple.winfo_width(), multiple.winfo_height()), size)
            self.assertEqual(multiple.destinations.canvas.winfo_height(), viewport_height)
            self.assertLess(multiple.destinations.canvas.yview()[1], 1)


if __name__ == "__main__":
    unittest.main()
