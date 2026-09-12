import tkinter as tk
import unittest
from unittest.mock import Mock

from mailarchive.dialogs import AccountDialog, MailboxDialog, RuleDialog, _center_on_parent


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


class DialogPlacementTkTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self.root.destroy)
        x = min(1920, max(40, self.root.winfo_screenwidth() - 1100))
        self.root.geometry(f"980x1000+{x}+50")
        self.root.update()

    def assert_centered(self, dialog: tk.Toplevel, parent: tk.Misc) -> None:
        self.root.update()
        self.assertEqual(str(dialog.transient()), str(parent))
        self.assertAlmostEqual(
            dialog.winfo_rootx() + dialog.winfo_width() / 2,
            parent.winfo_rootx() + parent.winfo_width() / 2,
            delta=30,
        )
        self.assertAlmostEqual(
            dialog.winfo_rooty() + dialog.winfo_height() / 2,
            parent.winfo_rooty() + parent.winfo_height() / 2,
            delta=30,
        )

    def test_account_and_rule_dialogs_follow_main_window_after_it_moves(self) -> None:
        for x in (40, min(1920, max(40, self.root.winfo_screenwidth() - 1100))):
            self.root.geometry(f"980x1000+{x}+50")
            self.root.update()
            for factory in (
                lambda: AccountDialog(self.root, 10),
                lambda: RuleDialog(self.root, "/archive"),
            ):
                with self.subTest(x=x, factory=factory):
                    dialog = factory()
                    try:
                        self.assert_centered(dialog, self.root)
                    finally:
                        dialog.destroy()

    def test_nested_mailbox_dialog_is_centered_over_account_dialog(self) -> None:
        account = AccountDialog(self.root, 10)
        mailbox = MailboxDialog(account, address="mail@example.org")

        self.assert_centered(mailbox, account)

    def test_resizing_rule_content_preserves_user_position(self) -> None:
        dialog = RuleDialog(self.root, "/archive")
        dialog.geometry("+120+100")
        self.root.update()
        position = dialog.winfo_rootx(), dialog.winfo_rooty()

        dialog.field_var.set("Sender")
        dialog._update_fields()
        dialog._add_sender_field()
        self.root.update()

        self.assertEqual((dialog.winfo_rootx(), dialog.winfo_rooty()), position)
