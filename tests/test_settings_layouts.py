"""Settings and desktop setup stay usable within scaled desktop viewports."""

import gc
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mailarchive.application.desktop_integration import IntegrationState, IntegrationStatus
from mailarchive.presentation.desktop_setup import DesktopIntegrationDialog, DesktopIntegrationUI
from tests import test_desktop_composition
from tests.tk_test_case import TkTestCase

DPI_VALUES = (96, 120, 144, 192)


def assert_inside(test, widget, viewport):
    test.assertTrue(widget.winfo_ismapped())
    test.assertGreaterEqual(widget.winfo_rootx(), viewport.winfo_rootx())
    test.assertGreaterEqual(widget.winfo_rooty(), viewport.winfo_rooty())
    test.assertLessEqual(
        widget.winfo_rootx() + widget.winfo_width(), viewport.winfo_rootx() + viewport.winfo_width()
    )
    test.assertLessEqual(
        widget.winfo_rooty() + widget.winfo_height(),
        viewport.winfo_rooty() + viewport.winfo_height(),
    )


class SettingsLayoutTests(unittest.TestCase):
    def run_desktop_probe(self, dpi, probe):
        class LayoutFixture(test_desktop_composition.DesktopCompositionTests):
            def setUp(self):
                try:
                    seed = tk.Tk()
                except tk.TclError as exc:
                    self.skipTest(str(exc))
                original = seed.tk.call("tk", "scaling")
                seed.tk.call("tk", "scaling", dpi / 72)
                seed.destroy()
                super().setUp()
                self.addCleanup(self.root.tk.call, "tk", "scaling", original)

            def run_layout_probe(self):
                probe(self)

        result = unittest.TestResult()
        LayoutFixture("run_layout_probe").run(result)
        if result.skipped:
            self.skipTest(result.skipped[0][1])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])

    def test_all_general_autosave_controls_and_advanced_draft_are_reachable(self):
        def probe(case):
            desktop, root = case.desktop, case.root
            desktop.notebook.select(desktop.settings_tab)
            desktop.settings_pages.select(0)
            root.focus_force()
            root.update()
            scroll = desktop.general_settings_scroll
            children = scroll.content.winfo_children()
            checks = [child for child in children if child.winfo_class() == "TCheckbutton"]
            combo = next(child for child in children if child.winfo_class() == "TCombobox")
            poll = next(child for child in children if child.winfo_class() == "TEntry")
            desktop.poll_var.set("37")
            for control in (*checks, combo, poll):
                control.focus_set()
                root.update()
                assert_inside(case, control, scroll.canvas)
            case.assertEqual(desktop.poll_var.get(), "37")
            warning = next(child for child in checks if "error occurs" in child.cget("text"))
            previous_warning = case.application.settings.warn_on_error
            warning.invoke()
            case.assertEqual(desktop.poll_var.get(), "37")
            case.assertEqual(case.application.settings.warn_on_error, not previous_warning)
            poll.event_generate("<Return>")
            root.update()
            case.assertEqual(case.application.settings.default_poll_minutes, 37)
            combo.focus_set()
            root.update()
            desktop.timezone_var.set("Europe/Berlin")
            combo.event_generate("<<ComboboxSelected>>")
            root.update()
            case.assertEqual(case.application.settings.archive_timezone, "Europe/Berlin")
            case.assertIs(root.focus_get(), combo)
            assert_inside(case, combo, scroll.canvas)
            desktop.settings_pages.select(1)
            root.update()
            database = next(
                child
                for child in desktop.advanced_settings_scroll.content.winfo_children()
                if child.winfo_class() == "TEntry"
            )
            database.focus_set()
            root.update()
            assert_inside(case, database, desktop.advanced_settings_scroll.canvas)
            saved = desktop.database_var.get()
            draft = saved + "/" + "long draft database path " * 12
            desktop.database_var.set(draft)
            case.assertEqual(database.get(), draft)
            case.assertIs(root.focus_get(), database)
            desktop.database_var.set(saved)

        for dpi in DPI_VALUES:
            with self.subTest(dpi=dpi):
                self.run_desktop_probe(dpi, probe)

    def test_integration_settings_long_paths_and_configure_remain_reachable(self):
        def probe(case):
            desktop, root = case.desktop, case.root
            integration = Mock()
            integration.status.return_value = IntegrationStatus(
                IntegrationState(installed_version="0.0.1", menu_entry=True),
                Path("/tmp") / ("long installation directory/" * 15) / "MailArchive.AppImage",
                True,
                True,
            )
            ui = DesktopIntegrationUI(
                root, integration, lambda: False, case.application.submit_background
            )
            ui.add_settings_page(desktop.settings_pages)
            desktop.notebook.select(desktop.settings_tab)
            desktop.settings_pages.select(2)
            root.focus_force()
            root.update()
            button = next(
                child
                for child in ui.settings_scroll.content.winfo_children()
                if child.winfo_class() == "TButton"
            )
            button.focus_set()
            root.update()
            assert_inside(case, button, ui.settings_scroll.canvas)
            button.invoke()
            case.assertIsNotNone(ui.dialog)
            ui._cancel()
            integration.apply.assert_not_called()
            case.assertIsNone(ui.dialog)

        for dpi in DPI_VALUES:
            with self.subTest(dpi=dpi):
                self.run_desktop_probe(dpi, probe)


class SetupLayoutTests(TkTestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.root.destroy)
        scaling = self.root.tk.call("tk", "scaling")
        self.addCleanup(self.root.tk.call, "tk", "scaling", scaling)
        self.root.geometry("980x680+0+0")
        self.root.update()

    def test_repeated_busy_setup_close_releases_timers_and_tcl_callbacks(self):
        status = IntegrationStatus(
            IntegrationState(), Path("/tmp/MailArchive.AppImage"), True, True
        )
        commands = set(self.root.tk.call("info", "commands"))
        timers = set(self.root.tk.call("after", "info"))
        for _ in range(5):
            dialog = DesktopIntegrationDialog(
                self.root, status, initial=True, submit=lambda options: None, cancel=lambda: None
            )
            dialog.set_busy(True)
            self.root.update()
            dialog.destroy()
            del dialog
            gc.collect()
            self.root.update()
            self.assertEqual(set(self.root.tk.call("after", "info")), timers)
            self.assertEqual(set(self.root.tk.call("info", "commands")), commands)

    def test_initial_and_existing_setup_long_paths_options_and_footer_fit(self):
        for dpi in DPI_VALUES:
            self.root.tk.call("tk", "scaling", dpi / 72)
            for initial in (False, True):
                with self.subTest(dpi=dpi, initial=initial):
                    status = IntegrationStatus(
                        IntegrationState(
                            installed_version="" if initial else "0.0.1", menu_entry=True
                        ),
                        Path("/tmp") / ("long application folder/" * 15) / "MailArchive.AppImage",
                        True,
                        True,
                    )
                    with (
                        patch.object(
                            DesktopIntegrationDialog, "winfo_screenwidth", return_value=1024
                        ),
                        patch.object(
                            DesktopIntegrationDialog, "winfo_screenheight", return_value=720
                        ),
                    ):
                        dialog = DesktopIntegrationDialog(
                            self.root,
                            status,
                            initial=initial,
                            submit=lambda options: None,
                            cancel=lambda: None,
                        )
                    try:
                        self.root.update()
                        self.assertLessEqual(dialog.winfo_width(), 976)
                        self.assertLessEqual(dialog.winfo_height(), 640)
                        for control in dialog.controls:
                            if control.winfo_class() == "TCheckbutton":
                                dialog.focus_force()
                                control.focus_set()
                                self.root.update()
                                assert_inside(self, control, dialog.form_scroll.canvas)
                            else:
                                assert_inside(self, control, dialog)
                        dialog.set_busy(True)
                        self.root.update()
                        for control in dialog.controls:
                            self.assertTrue(control.instate(["disabled"]))
                        dialog.set_busy(False)
                        dialog.status.set("Setup failed. You can retry or continue without setup.")
                        self.root.update()
                        for control in dialog.controls:
                            self.assertFalse(control.instate(["disabled"]))
                            if control.winfo_class() == "TButton":
                                assert_inside(self, control, dialog)
                    finally:
                        dialog.destroy()
