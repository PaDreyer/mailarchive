import gc
import re
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

from mailarchive.presentation.dialogs import (
    AccountDialog,
    MailboxDialog,
    RangeDialog,
    RuleDialog,
    _center_on_parent,
)
from tests.tk_test_case import TkTestCase


class DialogPlacementTests(unittest.TestCase):
    def test_dialog_follows_parent_across_monitor_coordinates(self) -> None:
        for parent_x, parent_y, expected in (
            (300, 100, "620x480+480+200"),
            (2260, 100, "620x480+2440+200"),
            (-1700, 100, "620x480+-1520+200"),
            (300, -980, "620x480+480+-880"),
        ):
            with self.subTest(parent_x=parent_x, parent_y=parent_y):
                parent = Mock()
                parent.winfo_rootx.return_value = parent_x
                parent.winfo_rooty.return_value = parent_y
                parent.winfo_width.return_value = 980
                parent.winfo_height.return_value = 680
                dialog = Mock()
                dialog.winfo_reqwidth.return_value = 620
                dialog.winfo_reqheight.return_value = 480

                _center_on_parent(dialog, parent)

                dialog.geometry.assert_called_once_with(expected)

    def test_fixed_account_size_is_used_instead_of_current_layout_size(self) -> None:
        parent = Mock()
        parent.winfo_rootx.return_value = 2260
        parent.winfo_rooty.return_value = 100
        parent.winfo_width.return_value = 980
        parent.winfo_height.return_value = 680
        dialog = Mock()
        dialog.winfo_reqwidth.return_value = 500
        dialog.winfo_reqheight.return_value = 420

        _center_on_parent(dialog, parent, width=620, height=480)

        dialog.geometry.assert_called_once_with("620x480+2440+200")


class DialogPlacementTkTests(TkTestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self.root.destroy)
        self.archive_path = str(Path.cwd() / "archive")
        x = min(1920, max(40, self.root.winfo_screenwidth() - 1100))
        self.root.geometry(f"980x1000+{x}+50")
        self.root.update()

    def assert_centered(self, dialog: tk.Toplevel, parent: tk.Misc) -> None:
        self.root.update()
        self.assertEqual(str(dialog.transient()), str(parent))
        # wm geometry reports the frame position and content size. rootx/rooty
        # report the content origin on Windows, adding the native title bar.
        geometry = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", dialog.geometry())
        self.assertIsNotNone(geometry)
        width, height, x, y = map(int, geometry.groups())
        self.assertAlmostEqual(
            x + width / 2,
            parent.winfo_rootx() + parent.winfo_width() / 2,
            delta=0.5,
        )
        self.assertAlmostEqual(
            y + height / 2,
            parent.winfo_rooty() + parent.winfo_height() / 2,
            delta=0.5,
        )

    def test_account_and_rule_dialogs_follow_main_window_after_it_moves(self) -> None:
        for x in (40, min(1920, max(40, self.root.winfo_screenwidth() - 1100))):
            self.root.geometry(f"980x1000+{x}+50")
            self.root.update()
            for factory in (
                lambda: AccountDialog(self.root, 10),
                lambda: RuleDialog(self.root),
            ):
                with self.subTest(x=x, factory=factory):
                    dialog = factory()
                    try:
                        if isinstance(dialog, (RuleDialog, AccountDialog)):
                            self.root.update()
                            self.assertGreaterEqual(dialog.winfo_y(), 24)
                            self.assertLessEqual(
                                dialog.winfo_y() + dialog.winfo_height(),
                                dialog.winfo_screenheight() - 24,
                            )
                        else:
                            self.assert_centered(dialog, self.root)
                    finally:
                        dialog.destroy()

    def test_nested_mailbox_dialog_is_centered_over_account_dialog(self) -> None:
        account = AccountDialog(self.root, 10)
        mailbox = MailboxDialog(account, address="mail@example.org")

        self.assert_centered(mailbox, account)

    def test_account_form_fits_a_constrained_screen_and_keeps_actions_visible(self) -> None:
        original_scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", original_scaling)
        for scaling in (96 / 72, 120 / 72):
            self.root.tk.call("tk", "scaling", scaling)
            with (
                patch.object(AccountDialog, "winfo_screenheight", return_value=720),
                patch.object(AccountDialog, "winfo_screenwidth", return_value=1024),
            ):
                dialog = AccountDialog(self.root, 10)
            try:
                self.root.update()
                self.assertLessEqual(dialog.winfo_height(), 640)
                self.assertLessEqual(dialog.winfo_width(), 976)
                for provider, auth in (
                    ("Generic IMAP", "Password"),
                    ("Generic IMAP", "Microsoft OAuth (XOAUTH2)"),
                    ("Gmail (Google API)", "Google OAuth - user sign-in"),
                    ("Gmail (Google API)", "Google Workspace - domain-wide delegation"),
                    (
                        "Outlook / Microsoft 365 (Microsoft Graph)",
                        "Microsoft OAuth - application access",
                    ),
                ):
                    dialog.variables["provider"].set(provider)
                    dialog.variables["auth"].set(auth)
                    dialog._update_fields()
                    dialog.authorization_detail.configure(text="A long authorization error. " * 50)
                    self.root.update()
                    self.assertGreaterEqual(dialog.save_button.winfo_rooty(), dialog.winfo_rooty())
                    self.assertLessEqual(
                        dialog.save_button.winfo_rooty() + dialog.save_button.winfo_height(),
                        dialog.winfo_rooty() + dialog.winfo_height(),
                    )
                    dialog.form_scroll.see(dialog.widgets["poll"])
                    self.root.update()
                    entry = dialog.widgets["poll"]
                    viewport = dialog.form_scroll.canvas
                    self.assertGreaterEqual(entry.winfo_rooty(), viewport.winfo_rooty())
                    self.assertLessEqual(
                        entry.winfo_rooty() + entry.winfo_height(),
                        viewport.winfo_rooty() + viewport.winfo_height(),
                    )
                    dialog.form_scroll.see(dialog.widgets["label"])
                    self.root.update()
                    self.assertGreaterEqual(
                        dialog.widgets["label"].winfo_rooty(), viewport.winfo_rooty()
                    )
            finally:
                dialog.destroy()

    def test_closed_account_dialogs_release_traces_and_entered_secrets(self) -> None:
        baseline = len(self.root.tk.call("info", "commands"))
        for _ in range(5):
            dialog = AccountDialog(self.root, 10)
            dialog.variables["secret"].set("obviously-fake-test-password")
            reference = weakref.ref(dialog)
            secret = weakref.ref(dialog.variables["secret"])
            commands = [handle for _, handle in dialog._variable_traces]
            variables = list(dialog.variables.values())
            dialog.destroy()
            self.assertTrue(all(not variable.trace_info() for variable in variables))
            self.assertTrue(
                all(not self.root.tk.call("info", "commands", handle) for handle in commands)
            )
            del variables, dialog
            self.root.update()
            gc.collect()
            self.assertIsNone(reference())
            self.assertIsNone(secret())
        self.assertEqual(len(self.root.tk.call("info", "commands")), baseline)

    def test_mailbox_dialog_saves_the_first_check_choice(self) -> None:
        mailbox = MailboxDialog(self.root, address="mail@example.org")
        mailbox.existing.set(True)

        mailbox._save()

        self.assertTrue(mailbox.result.archive_existing_messages)

    def test_rule_attachment_option_is_english_and_restored_when_editing(self) -> None:
        from mailarchive.domain.configuration import Rule, RuleTarget, SaveMode

        for rule in (
            None,
            Rule(
                "Invoices",
                targets=[RuleTarget(self.archive_path, SaveMode.EMAIL_AND_ATTACHMENTS, True)],
            ),
        ):
            with self.subTest(editing=rule is not None):
                dialog = RuleDialog(self.root, rule=rule)
                try:
                    self.assertEqual(
                        dialog.destinations.blocks[0].attachments_in_destination_var.get(),
                        rule is not None,
                    )
                    self.assertEqual(
                        dialog.destinations.blocks[0].attachments_in_destination_box.cget("text"),
                        "Save attachments directly in destination folder",
                    )
                    dialog.destinations.blocks[0].save_var.set("Email only (.eml)")
                    self.assertTrue(
                        dialog.destinations.blocks[0].attachments_in_destination_box.instate(
                            ["disabled"]
                        )
                    )
                    dialog.destinations.blocks[0].save_var.set("Attachments only")
                    self.assertFalse(
                        dialog.destinations.blocks[0].attachments_in_destination_box.instate(
                            ["disabled"]
                        )
                    )
                    dialog.name_var.set("Invoices")
                    dialog.destinations.blocks[0].path_var.set(self.archive_path)
                    with patch(
                        "mailarchive.presentation.dialogs.messagebox.showerror",
                        side_effect=AssertionError("Unexpected rule validation dialog"),
                    ):
                        dialog._save()
                    self.assertEqual(
                        dialog.result.targets[0].attachments_in_destination, rule is not None
                    )
                finally:
                    if dialog.winfo_exists():
                        dialog.destroy()

    def test_resizing_rule_content_preserves_user_position(self) -> None:
        dialog = RuleDialog(self.root)
        dialog.geometry("+120+24")
        self.root.update()
        position = dialog.winfo_rootx(), dialog.winfo_rooty()

        dialog.field_var.set("Sender")
        dialog._update_fields()
        dialog._add_sender_field()
        self.root.update()

        self.assertEqual((dialog.winfo_rootx(), dialog.winfo_rooty()), position)

    def test_many_sender_values_remain_editable_with_visible_destinations_and_save(self) -> None:
        dialog = RuleDialog(self.root)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        dialog.name_var.set("Many senders")
        dialog.field_var.set("Sender")
        dialog._update_fields()
        dialog.destinations.blocks[0].path_var.set(self.archive_path)
        canvas_callbacks = len(dialog.sender_scroll.canvas._tclCommands)
        for index in range(20):
            if index:
                dialog.add_sender_button.invoke()
            dialog.sender_value_vars[index].set(f"sender{index}@example.org")
        self.root.update()
        self.assertEqual(len(dialog.sender_scroll.canvas._tclCommands), canvas_callbacks)
        window_bottom = dialog.winfo_rooty() + dialog.winfo_height()
        for button in (dialog.save_button, dialog.cancel_button, dialog.add_sender_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), window_bottom)
        path_entry = dialog.destinations.blocks[0].path_entry
        self.assertLessEqual(path_entry.winfo_rooty() + path_entry.winfo_height(), window_bottom)
        dialog.sender_scroll.canvas.yview_moveto(0)
        dialog.focus_force()
        dialog.sender_entries[-1].focus_set()
        self.root.update()
        entry = dialog.sender_entries[-1]
        canvas = dialog.sender_scroll.canvas
        self.assertGreater(canvas.yview()[0], 0)
        self.assertGreaterEqual(entry.winfo_rooty(), canvas.winfo_rooty())
        self.assertLessEqual(
            entry.winfo_rooty() + entry.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height()
        )
        dialog.sender_value_vars[-1].set("edited@example.org")
        dialog.save_button.invoke()
        self.assertEqual(len(dialog.result.conditions), 20)
        self.assertEqual(dialog.result.conditions[-1].value, "edited@example.org")

    def test_range_dialog_returns_the_confirmed_timezone(self) -> None:
        from mailarchive.domain.configuration import Rule, RuleTarget

        dialog = RangeDialog(
            self.root,
            Rule("All", targets=[RuleTarget("/archive")]),
            "UTC",
        )
        try:
            self.assertFalse(hasattr(dialog, "source_box"))
            self.assertFalse(hasattr(dialog, "folder_list"))
            self.assertEqual(str(dialog.zone_box.cget("state")), "readonly")
            self.assertEqual(dialog.zone_var.get(), "UTC")
            self.assertIn("Europe/Berlin", dialog.zone_box.cget("values"))
            dialog.start_var.set("2026-03-29")
            dialog.end_var.set("2026-03-29")
            dialog.zone_var.set("Europe/Berlin")
            with patch("mailarchive.presentation.dialogs.messagebox.askyesno", return_value=True):
                dialog._save()
            self.assertEqual(dialog.result.timezone_name, "Europe/Berlin")
            self.assertEqual(dialog.result.start.isoformat(), "2026-03-28T23:00:00+00:00")
            self.assertEqual(dialog.result.end.isoformat(), "2026-03-29T22:00:00+00:00")
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    def test_dense_rule_at_high_dpi_keeps_width_actions_and_nested_inputs_visible(self):
        from mailarchive.domain.configuration import Account

        scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        self.root.tk.call("tk", "scaling", 192 / 72)
        accounts = [
            Account(f"Mail {i}", "imap.example.org", f"mail{i}@example.org") for i in range(20)
        ]
        with (
            patch.object(RuleDialog, "winfo_screenwidth", return_value=1024),
            patch.object(RuleDialog, "winfo_screenheight", return_value=720),
        ):
            dialog = RuleDialog(self.root, accounts=accounts)
            self.addCleanup(dialog.destroy)
            dialog.field_var.set("Sender")
            dialog._update_fields()
            for _ in range(19):
                dialog.add_sender_button.invoke()
            dialog.account_scope_var.set("selected")
            dialog._update_account_selection()
            for _ in range(5):
                dialog.destinations.add_button.invoke()
            self.root.update()
            self.assertLessEqual(dialog.winfo_width(), 976)
            self.assertLessEqual(dialog.winfo_height(), 640)
            for hint in (dialog.destinations.path_hint, dialog.value_hint):
                self.assertEqual(int(hint.cget("wraplength")), hint.winfo_width())
                self.assertGreaterEqual(hint.winfo_height(), hint.winfo_reqheight())
            for button in (dialog.save_button, dialog.cancel_button):
                self.assertGreaterEqual(button.winfo_rootx(), dialog.winfo_rootx())
                self.assertLessEqual(
                    button.winfo_rootx() + button.winfo_width(),
                    dialog.winfo_rootx() + dialog.winfo_width(),
                )
                self.assertLessEqual(
                    button.winfo_rooty() + button.winfo_height(),
                    dialog.winfo_rooty() + dialog.winfo_height(),
                )
            entry = dialog.destinations.blocks[-1].path_entry
            dialog.focus_force()
            entry.focus_set()
            self.root.update()
            for viewport in (dialog.destinations.canvas, dialog.form_scroll.canvas):
                self.assertGreaterEqual(entry.winfo_rootx(), viewport.winfo_rootx())
                self.assertLessEqual(
                    entry.winfo_rootx() + entry.winfo_width(),
                    viewport.winfo_rootx() + viewport.winfo_width(),
                )
                self.assertGreaterEqual(entry.winfo_rooty(), viewport.winfo_rooty())
                self.assertLessEqual(
                    entry.winfo_rooty() + entry.winfo_height(),
                    viewport.winfo_rooty() + viewport.winfo_height(),
                )

    def test_small_rule_window_scrolls_nested_inputs_and_keeps_footer_visible(self) -> None:
        from mailarchive.domain.configuration import Account

        accounts = [
            Account(f"Mail {i}", "imap.example.org", f"mail{i}@example.org") for i in range(20)
        ]
        dialog = RuleDialog(self.root, accounts=accounts)
        self.addCleanup(dialog.destroy)
        dialog.field_var.set("Sender")
        dialog._update_fields()
        for _ in range(19):
            dialog.add_sender_button.invoke()
        dialog.account_scope_var.set("selected")
        with patch.object(dialog, "winfo_screenheight", return_value=720):
            dialog._update_account_selection()
            self.root.update()
            self.assertLessEqual(dialog.winfo_height(), 640)
            self.assertGreaterEqual(dialog.destinations.canvas.winfo_height(), 80)
            dialog.destinations.add_button.invoke()
            entry = dialog.destinations.blocks[-1].path_entry
            dialog.focus_force()
            entry.focus_set()
            self.root.update()
            inner = dialog.destinations.canvas
            outer = dialog.form_scroll.canvas
            self.assertGreater(inner.yview()[0], 0)
            self.assertGreater(outer.yview()[0], 0)
            for viewport in (inner, outer):
                self.assertGreaterEqual(entry.winfo_rooty(), viewport.winfo_rooty())
                self.assertLessEqual(
                    entry.winfo_rooty() + entry.winfo_height(),
                    viewport.winfo_rooty() + viewport.winfo_height(),
                )
            self.assertLessEqual(
                dialog.save_button.winfo_rooty() + dialog.save_button.winfo_height(),
                dialog.winfo_rooty() + dialog.winfo_height(),
            )
            inner.yview_moveto(1)
            outer.yview_moveto(0)
            self.root.update()
            before = outer.yview()
            inner.event_generate("<MouseWheel>", delta=-120)
            self.root.update()
            self.assertGreater(outer.yview()[0], before[0])

    def test_removed_destination_releases_traces_and_widget_without_retaining_dialog(self) -> None:
        dialog = RuleDialog(self.root)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        dialog.destinations.add_button.invoke()
        block = dialog.destinations.blocks[-1]
        reference = weakref.ref(block)
        handles = tuple(handle for _, handle in block._variable_traces)
        block.remove_button.invoke()
        del block
        gc.collect()
        self.assertIsNone(reference())
        for handle in handles:
            self.assertFalse(self.root.tk.call("info", "commands", handle))

    def test_closing_rule_dialog_removes_destination_variable_callbacks(self) -> None:
        dialog = RuleDialog(self.root)
        block = dialog.destinations.blocks[0]
        variables = block.path_var, block.save_var
        handles = tuple(handle for _, handle in block._variable_traces)
        dialog.cancel_button.invoke()
        for variable in variables:
            self.assertEqual(variable.trace_info(), [])
        for handle in handles:
            self.assertFalse(self.root.tk.call("info", "commands", handle))
