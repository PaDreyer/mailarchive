from __future__ import annotations

import gc
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mailarchive.domain.archive_paths import destination_path
from mailarchive.presentation.dialogs import DestinationBlock, RuleDialog
from mailarchive.presentation.folder_picker import (
    FolderPickerDialog,
    _choose_linux_folder,
    _initial_directory,
    choose_destination_folder,
)


class FolderPickerTests(unittest.TestCase):
    def test_initial_directory_uses_existing_template_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for suffix in ("", "/missing/child", "/{year}/{month}", "/Invoices-{year}"):
                with self.subTest(suffix=suffix):
                    self.assertEqual(_initial_directory(str(base) + suffix), base)
            literal = base / "{year}"
            literal.mkdir()
            self.assertEqual(
                _initial_directory(str(literal).replace("{", "{{").replace("}", "}}")), literal
            )
        for invalid in ("", "relative/path", "/{unsupported}", "/invalid{"):
            with self.subTest(invalid=invalid):
                self.assertEqual(_initial_directory(invalid), Path.home())

    def test_windows_native_selection_escapes_literal_braces_and_restores_modal_state(self) -> None:
        parent = Mock()
        grab, focus = parent.grab_current.return_value, parent.focus_get.return_value
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "Belege ä {year}"
            selected.mkdir()
            with (
                patch("mailarchive.presentation.folder_picker.sys.platform", "win32"),
                patch(
                    "mailarchive.presentation.folder_picker.filedialog.askdirectory",
                    return_value=str(selected),
                ) as native,
            ):
                result = choose_destination_folder(parent, temporary)
            native.assert_called_once_with(
                parent=parent, initialdir=temporary, title="Choose folder", mustexist=True
            )
            self.assertEqual(destination_path(result), selected)
            grab.grab_set.assert_called_once_with()
            focus.focus_set.assert_called_once_with()

    def test_native_cancel_returns_none(self) -> None:
        with (
            patch("mailarchive.presentation.folder_picker.sys.platform", "win32"),
            patch(
                "mailarchive.presentation.folder_picker.filedialog.askdirectory", return_value=""
            ),
        ):
            self.assertIsNone(choose_destination_folder(Mock(), ""))

    def test_portal_cancel_does_not_open_fallback_but_error_does(self) -> None:
        parent = Mock()
        for result in (None, Path.cwd(), RuntimeError("unavailable")):
            with (
                self.subTest(result=result),
                patch(
                    "mailarchive.presentation.folder_picker._PortalWaitDialog",
                    return_value=SimpleNamespace(result=result),
                ),
                patch("mailarchive.presentation.folder_picker.FolderPickerDialog") as fallback,
            ):
                fallback.return_value.result = Path.home()
                actual = _choose_linux_folder(parent, Path.cwd())
                if isinstance(result, Exception):
                    self.assertEqual(actual, Path.home())
                    fallback.assert_called_once_with(parent, Path.cwd())
                else:
                    self.assertEqual(actual, result)
                    fallback.assert_not_called()

    def test_missing_portal_library_uses_fallback(self) -> None:
        with (
            patch(
                "mailarchive.presentation.folder_picker._PortalWaitDialog", side_effect=ImportError
            ),
            patch("mailarchive.presentation.folder_picker.FolderPickerDialog") as fallback,
        ):
            fallback.return_value.result = None
            self.assertIsNone(_choose_linux_folder(Mock(), Path.cwd()))
            fallback.assert_called_once()

    def test_parent_destruction_never_opens_fallback(self) -> None:
        parent = Mock()
        parent.winfo_exists.return_value = False
        with (
            patch(
                "mailarchive.presentation.folder_picker._PortalWaitDialog",
                return_value=SimpleNamespace(result=RuntimeError()),
            ),
            patch("mailarchive.presentation.folder_picker.FolderPickerDialog") as fallback,
        ):
            self.assertIsNone(_choose_linux_folder(parent, Path.cwd()))
            fallback.assert_not_called()

    def test_destination_block_uses_shared_picker_and_preserves_value_on_cancel(self) -> None:
        target = object.__new__(DestinationBlock)
        target.winfo_toplevel = Mock(return_value=Mock())
        target.path_var = Mock()
        target.path_var.get.return_value = "/original/{year}"
        with patch(
            "mailarchive.presentation.dialogs.choose_destination_folder", return_value=None
        ) as picker:
            target._choose_folder()
            picker.assert_called_once_with(target.winfo_toplevel(), "/original/{year}")
            target.path_var.set.assert_not_called()
            picker.return_value = "/new"
            target._choose_folder()
            target.path_var.set.assert_called_once_with("/new")


class FolderPickerTkTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(gc.collect)
        self.addCleanup(self.root.destroy)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root.geometry("800x600+40+40")
        self.root.update()

    def test_new_folder_is_created_opened_and_can_be_chosen(self) -> None:
        dialog = FolderPickerDialog(self.root, self.base)
        with patch(
            "mailarchive.presentation.folder_picker.simpledialog.askstring",
            return_value="Belege ä {literal}",
        ):
            dialog._new_folder()
        created = self.base / "Belege ä {literal}"
        self.assertTrue(created.is_dir())
        self.assertEqual(dialog.path_var.get(), str(created))
        dialog._choose()
        self.assertEqual(dialog.result, created)

    def test_cancel_keeps_created_folder_without_selecting_it(self) -> None:
        dialog = FolderPickerDialog(self.root, self.base)
        with patch(
            "mailarchive.presentation.folder_picker.simpledialog.askstring", return_value="New"
        ):
            dialog._new_folder()
        dialog.destroy()
        self.assertIsNone(dialog.result)
        self.assertTrue((self.base / "New").is_dir())

    def test_invalid_names_conflicts_and_permission_errors_keep_dialog_open(self) -> None:
        (self.base / "existing").mkdir()
        (self.base / "file").write_text("", encoding="utf-8")
        dialog = FolderPickerDialog(self.root, self.base)
        for name in (
            "",
            ".",
            "..",
            "one/two",
            "one\\two",
            " name",
            "name ",
            "\0",
            "existing",
            "file",
        ):
            with (
                self.subTest(name=name),
                patch(
                    "mailarchive.presentation.folder_picker.simpledialog.askstring",
                    return_value=name,
                ),
                patch("mailarchive.presentation.folder_picker.messagebox.showerror") as error,
            ):
                dialog._new_folder()
                error.assert_called_once()
                self.assertTrue(dialog.winfo_exists())
                self.assertEqual(dialog.path_var.get(), str(self.base))
        with (
            patch(
                "mailarchive.presentation.folder_picker.simpledialog.askstring",
                return_value="denied",
            ),
            patch.object(Path, "mkdir", side_effect=PermissionError("Access denied")),
            patch("mailarchive.presentation.folder_picker.messagebox.showerror") as error,
        ):
            dialog._new_folder()
            error.assert_called_once()
        self.assertFalse((self.base / "denied").exists())

    def test_navigation_selection_hidden_folders_and_unreadable_directory(self) -> None:
        (self.base / "Visible").mkdir()
        (self.base / ".hidden").mkdir()
        dialog = FolderPickerDialog(self.root, self.base)
        self.assertEqual(dialog.folder_list.get(0, "end"), ("Visible",))
        dialog.hidden_var.set(True)
        dialog._refresh()
        self.assertEqual(dialog.folder_list.get(0, "end"), (".hidden", "Visible"))
        dialog.folder_list.selection_set(1)
        dialog._open_selected()
        self.assertEqual(dialog.path_var.get(), str(self.base / "Visible"))
        dialog._up()
        with (
            patch.object(Path, "iterdir", side_effect=PermissionError("Access denied")),
            patch("mailarchive.presentation.folder_picker.messagebox.showerror") as error,
        ):
            dialog._refresh()
            error.assert_called_once()
        dialog.path_var.set("missing")
        with patch("mailarchive.presentation.folder_picker.messagebox.showerror") as error:
            dialog._choose()
            error.assert_called_once()
        dialog.path_var.set(str(self.base))
        dialog.folder_list.selection_set(1)
        dialog._choose()
        self.assertEqual(dialog.result, self.base / "Visible")

    @unittest.skipUnless(sys.platform == "linux", "Linux portal UI")
    def test_native_wait_keeps_ui_responsive_and_restores_rule_grab(self) -> None:
        rule = RuleDialog(self.root)
        request = Mock()
        request.results = Queue()
        ticks = []
        self.root.after(20, lambda: ticks.append("responsive"))
        self.root.after(70, lambda: request.results.put(self.base))
        with patch(
            "mailarchive.presentation.linux_folder_picker.PortalFolderRequest", return_value=request
        ):
            rule.destinations.blocks[0]._choose_folder()
        self.assertEqual(rule.destinations.blocks[0].path_var.get(), str(self.base))
        self.assertEqual(ticks, ["responsive"])
        self.assertIs(self.root.grab_current(), rule)
        request.start.assert_called_once()
        request.cancel.assert_called_once()

    @unittest.skipUnless(sys.platform == "linux", "Linux portal UI")
    def test_rule_to_fallback_to_created_destination(self) -> None:
        rule = RuleDialog(self.root)
        rule.destinations.blocks[0].path_var.set(str(self.base))
        request = Mock()
        request.results = Queue()
        request.results.put(RuntimeError("No desktop portal"))

        def choose_new_folder() -> None:
            picker = next(
                child for child in rule.winfo_children() if isinstance(child, FolderPickerDialog)
            )
            picker._new_folder()
            picker._choose()

        self.root.after(70, choose_new_folder)
        with (
            patch(
                "mailarchive.presentation.linux_folder_picker.PortalFolderRequest",
                return_value=request,
            ),
            patch(
                "mailarchive.presentation.folder_picker.simpledialog.askstring",
                return_value="Belege ä {year}",
            ),
        ):
            rule.destinations.blocks[0]._choose_folder()
        created = self.base / "Belege ä {year}"
        self.assertTrue(created.is_dir())
        self.assertEqual(destination_path(rule.destinations.blocks[0].path_var.get()), created)
        self.assertEqual(rule.destinations.blocks[0].preview_var.get(), str(created))
        self.assertIs(self.root.grab_current(), rule)
        rule.name_var.set("Archive")
        rule.sender_value_vars[0].set("mail@example.com")
        rule._save()
        self.assertEqual(destination_path(rule.result.targets[0].path), created)

    @unittest.skipUnless(sys.platform == "linux", "Linux portal UI")
    def test_second_destination_to_fallback_to_saved_target(self) -> None:
        rule = RuleDialog(self.root)
        rule.destinations.blocks[0].path_var.set(str(self.base))
        rule.destinations.add()
        target = rule.destinations.blocks[1]
        target.path_var.set(str(self.base))
        request = Mock()
        request.results = Queue()
        request.results.put(RuntimeError("No desktop portal"))

        def choose_new_folder() -> None:
            picker = next(
                child for child in rule.winfo_children() if isinstance(child, FolderPickerDialog)
            )
            picker._new_folder()
            picker._choose()

        self.root.after(70, choose_new_folder)
        with (
            patch(
                "mailarchive.presentation.linux_folder_picker.PortalFolderRequest",
                return_value=request,
            ),
            patch(
                "mailarchive.presentation.folder_picker.simpledialog.askstring",
                return_value="Invoices",
            ),
        ):
            target._choose_folder()
        rule.name_var.set("Archive")
        rule._save()
        self.assertEqual(destination_path(rule.result.targets[1].path), self.base / "Invoices")

    @unittest.skipUnless(sys.platform == "linux", "Linux portal UI")
    def test_cancel_button_preserves_rule_destination_and_closes_native_request(self) -> None:
        rule = RuleDialog(self.root)
        rule.destinations.blocks[0].path_var.set(str(self.base))
        request = Mock()
        request.results = Queue()

        def cancel_selection() -> None:
            next(
                child
                for child in rule.winfo_children()
                if isinstance(child, tk.Toplevel) and child.title() == "Choose folder"
            ).destroy()

        self.root.after(30, cancel_selection)
        with (
            patch(
                "mailarchive.presentation.linux_folder_picker.PortalFolderRequest",
                return_value=request,
            ),
            patch("mailarchive.presentation.folder_picker.FolderPickerDialog") as fallback,
        ):
            rule.destinations.blocks[0]._choose_folder()
        self.assertEqual(rule.destinations.blocks[0].path_var.get(), str(self.base))
        self.assertIs(self.root.grab_current(), rule)
        request.cancel.assert_called_once()
        fallback.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Linux portal UI")
    def test_parent_close_cancels_portal_request(self) -> None:
        rule = RuleDialog(self.root)
        request = Mock()
        request.results = Queue()
        self.root.after(30, rule.destroy)
        with patch(
            "mailarchive.presentation.linux_folder_picker.PortalFolderRequest", return_value=request
        ):
            self.assertIsNone(choose_destination_folder(rule, str(self.base)))
        request.cancel.assert_called_once()
