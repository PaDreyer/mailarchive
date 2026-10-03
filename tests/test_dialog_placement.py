import re
import tkinter as tk
import unittest
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
                        if isinstance(dialog, RuleDialog):
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
