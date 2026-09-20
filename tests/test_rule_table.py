import gc
import tkinter as tk
import unittest
from tkinter import ttk
from unittest.mock import Mock

from mailarchive.desktop import DesktopApp


class RuleTableTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        self.root.geometry("980x580+40+40")
        self.app = object.__new__(DesktopApp)
        self.app.root = self.root
        self.app.edit_rule = Mock()
        self.app._configure_style()
        container = ttk.Frame(self.root, padding=(22, 18))
        container.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self.app.rules_tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(self.app.rules_tab, text="Rules")
        self.app._build_rules()
        self.tree = self.app.rule_tree
        self.root.update()

    def widths(self) -> dict[str, int]:
        return {key: self.tree.column(key, "width") for key in self.tree["columns"]}

    def manual_widths(self) -> dict[str, int]:
        return {key: width for key, width in self.widths().items() if key != "active"}

    def assert_headers_fill_table(self) -> None:
        self.assertGreaterEqual(sum(self.widths().values()), self.tree.winfo_width() - 4)
        self.assertGreaterEqual(
            self.tree.column("active", "width"), self.tree.column("active", "minwidth")
        )

    def separator(self, column: str) -> tuple[int, int]:
        display_column = f"#{self.tree['columns'].index(column) + 1}"
        for y in range(30):
            for x in range(self.tree.winfo_width()):
                if (
                    self.tree.identify_region(x, y) == "separator"
                    and self.tree.identify_column(x) == display_column
                ):
                    return x, y
        self.fail(f"No visible separator for {column}")

    def drag(self, column: str, delta: int) -> dict[str, int]:
        self.tree.xview_moveto(0)
        self.root.update()
        x, y = self.separator(column)
        self.tree.event_generate("<ButtonPress-1>", x=x, y=y)
        self.tree.event_generate("<B1-Motion>", x=x + delta, y=y)
        self.root.update()
        during_drag = self.widths()
        self.tree.event_generate("<ButtonRelease-1>", x=x + delta, y=y)
        self.root.update()
        self.assertEqual(
            self.manual_widths(),
            {key: width for key, width in during_drag.items() if key != "active"},
        )
        self.assert_headers_fill_table()
        return self.widths()

    def test_dragged_width_survives_release_and_allows_horizontal_scrolling(self) -> None:
        for window_width in (820, 980):
            with self.subTest(window_width=window_width):
                self.root.geometry(f"{window_width}x580")
                self.root.update()
                before = self.widths()
                after = self.drag("condition", 240)
                self.assertGreater(after["condition"], before["condition"] + 200)
                for column in before:
                    if column not in {"condition", "active"}:
                        self.assertEqual(after[column], before[column])
                self.assertLess(self.tree.xview()[1], 1.0)
                self.tree.xview_moveto(1)
                self.root.update()
                self.assertEqual(self.tree.xview()[1], 1.0)

    def test_manual_widths_survive_window_resize_and_tab_switch(self) -> None:
        self.drag("destination", 180)
        after = self.manual_widths()
        for width in (1440, 820, 980):
            self.root.geometry(f"{width}x580")
            self.root.update()
            self.assertEqual(self.manual_widths(), after)
            self.assert_headers_fill_table()
        other_tab = ttk.Frame(self.notebook)
        self.notebook.add(other_tab, text="Other")
        self.notebook.select(other_tab)
        self.root.update()
        self.notebook.select(self.app.rules_tab)
        self.root.update()
        self.assertEqual(self.manual_widths(), after)
        self.assert_headers_fill_table()

    def test_shrinking_columns_fills_the_remaining_space_with_the_last_column(self) -> None:
        self.root.geometry("1440x580")
        self.root.update()
        before = self.widths()
        after = self.drag("name", -100)
        minimum_width = self.tree.column("name", "minwidth")
        self.assertLessEqual(after["name"], max(before["name"] - 80, minimum_width))
        self.assertGreaterEqual(after["name"], minimum_width)
        freed_width = before["name"] - after["name"]
        self.assertEqual(after["active"], before["active"] + freed_width)
        for column in before:
            if column not in {"name", "active"}:
                self.assertEqual(after[column], before[column])

    def test_shrinking_a_column_clamps_at_its_minimum_width(self) -> None:
        self.root.geometry("1440x580")
        minimum_width = self.tree.column("name", "minwidth")
        self.tree.column("name", width=minimum_width + 44, stretch=False)
        self.root.update()
        before = self.widths()
        self.assertEqual(before["name"], minimum_width + 44)

        after = self.drag("name", -100)

        self.assertEqual(after["name"], minimum_width)
        self.assertEqual(after["active"], before["active"] + 44)
        for column in before:
            if column not in {"name", "active"}:
                self.assertEqual(after[column], before[column])

    def test_last_column_cannot_leave_a_gap_at_the_right_edge(self) -> None:
        self.root.geometry("1440x580")
        self.root.update()
        self.drag("name", -100)
        self.assertGreater(
            self.tree.column("active", "width"), self.tree.column("active", "minwidth") + 40
        )
        before = self.manual_widths()
        self.drag("active", -40)
        self.assertEqual(self.manual_widths(), before)
        self.assert_headers_fill_table()

    def test_outer_separator_cannot_move_even_during_dragging_or_repeated_clicks(self) -> None:
        self.root.geometry("1440x580")
        self.root.update()
        self.drag("name", -100)
        before = self.widths()
        x, y = self.separator("active")
        for delta in (-40, 60, -120):
            with self.subTest(delta=delta):
                self.tree.event_generate("<Motion>", x=x, y=y)
                self.root.update()
                self.assertEqual(str(self.tree["cursor"]), "")
                self.tree.event_generate("<ButtonPress-1>", x=x, y=y)
                self.tree.event_generate("<B1-Motion>", x=x + delta, y=y)
                self.root.update()
                self.assertEqual(self.widths(), before)
                self.assertEqual(str(self.tree["cursor"]), "")
                self.tree.event_generate("<ButtonRelease-1>", x=x + delta, y=y)
                self.root.update()
                self.assertEqual(self.widths(), before)
                self.assertEqual(str(self.tree["cursor"]), "")
        self.app.edit_rule.assert_not_called()

    def test_inner_separator_can_drag_across_the_outer_edge(self) -> None:
        x, _ = self.separator("condition")
        before = self.widths()
        after = self.drag("condition", self.tree.winfo_width() - 2 - x)
        self.assertGreater(after["condition"], before["condition"] + 100)
        x, y = self.separator("name")
        self.tree.event_generate("<Motion>", x=x, y=y)
        self.root.update()
        self.assertTrue(str(self.tree["cursor"]))

    def test_repeated_drags_can_widen_and_narrow_a_column(self) -> None:
        before = self.widths()
        wider = self.drag("name", 140)
        self.assertGreater(wider["name"], before["name"] + 100)
        narrower = self.drag("name", -80)
        self.assertLess(narrower["name"], wider["name"] - 60)
        self.assertGreaterEqual(narrower["name"], self.tree.column("name", "minwidth"))
        self.app.edit_rule.assert_not_called()

    def test_selecting_a_row_keeps_automatic_sizing_until_a_separator_is_dragged(self) -> None:
        self.tree.insert("", "end", iid="rule", values=(1, "Example"))
        self.root.update()
        x, y, _, height = self.tree.bbox("rule", "name")
        self.tree.event_generate("<ButtonPress-1>", x=x + 10, y=y + height // 2)
        self.tree.event_generate("<ButtonRelease-1>", x=x + 10, y=y + height // 2)
        self.root.update()
        self.assertEqual(self.tree.selection(), ("rule",))
        self.assertTrue(self.tree.column("condition", "stretch"))
        before = self.widths()
        self.root.geometry("1440x580")
        self.root.update()
        self.assertGreater(self.widths()["condition"], before["condition"])
