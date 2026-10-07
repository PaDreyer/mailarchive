from __future__ import annotations

import tempfile
import threading
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from mailarchive import __version__
from mailarchive.application.background import BackgroundResult, BackgroundTasks
from mailarchive.application.desktop_integration import (
    IntegrationError,
    IntegrationOptions,
    IntegrationResult,
    IntegrationState,
    IntegrationStatus,
)
from mailarchive.infrastructure.linux_integration import (
    AppImageIntegration,
    IntegrationPaths,
)
from mailarchive.presentation.desktop_setup import DesktopIntegrationDialog, DesktopIntegrationUI
from tests.concurrency import THREAD_TIMEOUT
from tests.test_app import FakeVariable, make_desktop
from tests.test_linux_integration import APPIMAGE
from tests.tk_test_case import TkTestCase


class DesktopIntegrationUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = MagicMock()
        self.integration = MagicMock()
        self.integration.status.return_value = IntegrationStatus(
            IntegrationState(), Path("/tmp/MailArchive.AppImage"), True, False
        )
        self.submit_background = MagicMock()
        self.ui = DesktopIntegrationUI(
            self.root, self.integration, lambda: True, self.submit_background
        )

    def test_first_run_offer_is_scheduled_after_window_is_ready(self) -> None:
        with patch.object(self.ui, "configure") as configure:
            self.ui.offer_once()
            configure.assert_not_called()
            self.root.after_idle.call_args.args[0]()
        configure.assert_called_once_with(initial=True)
        self.integration.status.return_value = replace(
            self.integration.status.return_value,
            state=IntegrationState(prompt_seen=True),
        )
        self.root.reset_mock()
        self.ui.offer_once()
        self.root.after_idle.assert_not_called()

    def test_unreadable_receipt_is_reported_without_showing_setup(self) -> None:
        self.integration.status.side_effect = IntegrationError("unsupported receipt")
        with patch("mailarchive.presentation.desktop_setup.messagebox.showerror") as showerror:
            self.ui.offer_once()
        showerror.assert_called_once()
        self.root.after_idle.assert_not_called()

    def test_dialog_is_not_opened_twice(self) -> None:
        with patch("mailarchive.presentation.desktop_setup.DesktopIntegrationDialog") as dialog:
            self.ui.configure(initial=True)
            self.ui.configure()
        dialog.assert_called_once()
        dialog.return_value.lift.assert_called_once()

    def test_configure_reports_filesystem_errors(self) -> None:
        self.integration.status.side_effect = OSError("permission denied")
        with patch("mailarchive.presentation.desktop_setup.messagebox.showerror") as showerror:
            self.ui.configure()
        self.assertIsNone(self.ui.dialog)
        showerror.assert_called_once()

    def test_initial_skip_is_persisted_but_manual_cancel_changes_nothing(self) -> None:
        for initial in (True, False):
            with self.subTest(initial=initial):
                self.integration.reset_mock()
                dialog = MagicMock()
                self.ui.dialog = dialog
                self.ui.initial = initial
                self.ui._cancel()
                self.assertEqual(self.integration.mark_prompt_seen.call_count, int(initial))
                self.integration.apply.assert_not_called()
                dialog.destroy.assert_called_once()
                self.assertIsNone(self.ui.dialog)

    def test_failed_skip_persistence_does_not_prevent_using_the_application(self) -> None:
        dialog = MagicMock()
        self.ui.dialog = dialog
        self.ui.initial = True
        self.integration.mark_prompt_seen.side_effect = OSError("disk full")
        with patch("mailarchive.presentation.desktop_setup.messagebox.showerror") as showerror:
            self.ui._cancel()
        showerror.assert_called_once()
        dialog.destroy.assert_called_once()

    def test_install_is_off_ui_thread_and_completes_through_ui_queue(self) -> None:
        dialog = MagicMock()
        self.ui.dialog = dialog
        state = IntegrationState(prompt_seen=True, installed_version=__version__, menu_entry=True)
        self.integration.apply.return_value = IntegrationResult(state, ("Allow Launching",))
        with patch("mailarchive.presentation.desktop_setup.messagebox.showinfo") as showinfo:
            self.ui._submit(IntegrationOptions())
            self.ui._submit(IntegrationOptions())
            self.ui._cancel()
            self.assertTrue(self.ui.busy)
            self.submit_background.assert_called_once()
            dialog.destroy.assert_not_called()
            showinfo.assert_not_called()
            work, callback = self.submit_background.call_args.args
            result = work()
            self.integration.apply.assert_called_once_with(
                IntegrationOptions(), start_at_login=True
            )
            callback(BackgroundResult(value=result))
        self.assertFalse(self.ui.busy)
        self.assertIsNone(self.ui.dialog)
        self.assertIn("Allow Launching", showinfo.call_args.args[1])
        dialog.destroy.assert_called_once()

    def test_failed_install_remains_open_and_can_be_retried(self) -> None:
        dialog = MagicMock()
        self.ui.dialog = dialog
        self.integration.apply.side_effect = OSError("disk full")
        with patch("mailarchive.presentation.desktop_setup.messagebox.showerror") as showerror:
            self.ui._submit(IntegrationOptions())
            showerror.assert_not_called()
            self.submit_background.call_args.args[1](BackgroundResult(error=OSError("disk full")))
        self.assertFalse(self.ui.busy)
        self.assertIs(self.ui.dialog, dialog)
        dialog.set_busy.assert_called_with(False)
        dialog.destroy.assert_not_called()
        self.assertIn("disk full", showerror.call_args.args)

    def test_background_submission_failure_restores_controls_immediately(self) -> None:
        self.ui.dialog = MagicMock()
        self.submit_background.side_effect = RuntimeError("cannot start task")
        with patch("mailarchive.presentation.desktop_setup.messagebox.showerror") as showerror:
            self.ui._submit(IntegrationOptions())
        self.assertFalse(self.ui.busy)
        showerror.assert_called_once()

    def test_status_page_and_error_summary(self) -> None:
        self.ui.summary = FakeVariable()
        self.ui._refresh_summary()
        self.assertIn("Not installed", self.ui.summary.get())
        self.integration.status.return_value = replace(
            self.integration.status.return_value,
            state=IntegrationState(
                installed_version=__version__,
                menu_entry=True,
                desktop_path="/tmp/Desktop/MailArchive.desktop",
            ),
            application_present=True,
        )
        self.ui._refresh_summary()
        self.assertIn("Application menu: Enabled", self.ui.summary.get())
        self.integration.status.side_effect = IntegrationError("bad receipt")
        self.ui._refresh_summary()
        self.assertIn("bad receipt", self.ui.summary.get())

    def test_settings_page_exposes_configuration_and_update_instructions(self) -> None:
        notebook = MagicMock()
        with (
            patch("mailarchive.presentation.desktop_setup.ttk") as widgets,
            patch("mailarchive.presentation.desktop_setup.ScrollableFrame") as scroll,
            patch("mailarchive.presentation.desktop_setup.tk.StringVar", FakeVariable),
        ):
            self.ui.add_settings_page(notebook)
        notebook.add.assert_called_once_with(widgets.Frame.return_value, text="Desktop integration")
        self.assertEqual(widgets.Button.call_args.kwargs["command"], self.ui.configure)
        scroll.return_value.bind_widgets.assert_called_once_with()
        self.assertTrue(
            any("update" in call.kwargs.get("text", "") for call in widgets.Label.call_args_list)
        )

    def test_quit_and_hide_are_blocked_while_transaction_is_running(self) -> None:
        desktop = make_desktop()
        desktop.desktop_integration = MagicMock(busy=True)
        desktop.quit()
        desktop.hide_to_tray()
        self.assertEqual(desktop.desktop_integration.show_busy.call_count, 2)
        desktop.root.destroy.assert_not_called()
        desktop.root.withdraw.assert_not_called()
        desktop.application.close.assert_not_called()
        self.assertFalse(desktop._closing)

    def test_offer_is_delegated_only_when_appimage_ui_exists(self) -> None:
        desktop = make_desktop()
        desktop.offer_desktop_integration()
        desktop.desktop_integration = self.ui
        with patch.object(self.ui, "offer_once") as offer:
            desktop.offer_desktop_integration()
        offer.assert_called_once()


class DesktopIntegrationDialogTkTests(TkTestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self.root.destroy)
        self.root.geometry("980x680+100+50")
        self.root.update()
        self.status = IntegrationStatus(
            IntegrationState(), Path("/tmp/Mail Archive/MailArchive.AppImage"), True, False
        )
        self.submit = MagicMock()
        self.cancel = MagicMock()

    def _dialog(self, *, initial: bool = True, state: IntegrationState | None = None):
        dialog = DesktopIntegrationDialog(
            self.root,
            replace(self.status, state=state or IntegrationState()),
            initial=initial,
            submit=self.submit,
            cancel=self.cancel,
        )
        self.addCleanup(dialog.destroy)
        return dialog

    def test_default_choices_apply_and_busy_controls(self) -> None:
        dialog = self._dialog()
        self.assertTrue(dialog.menu.get())
        self.assertFalse(dialog.desktop.get())
        self.assertEqual(dialog.grab_current(), dialog)
        self.assertEqual(str(dialog.transient()), str(self.root))
        self.assertEqual(dialog.progress.winfo_manager(), "")
        dialog.controls[2].invoke()
        self.submit.assert_called_once_with(IntegrationOptions())
        dialog.set_busy(True)
        self.assertEqual(dialog.progress.winfo_manager(), "pack")
        self.assertTrue(
            all(str(control.cget("state")) == "disabled" for control in dialog.controls)
        )
        dialog.set_busy(False)
        self.assertEqual(dialog.progress.winfo_manager(), "")
        self.assertTrue(all(str(control.cget("state")) == "normal" for control in dialog.controls))

    def test_no_desktop_directory_disables_desktop_choice(self) -> None:
        self.status = replace(self.status, desktop_available=False)
        dialog = self._dialog(
            state=IntegrationState(
                installed_version=__version__, desktop_path="/tmp/old/MailArchive.desktop"
            )
        )
        self.assertFalse(dialog.desktop.get())
        self.assertEqual(len(dialog.controls), 3)

    def test_manual_dialog_uses_existing_options_and_cancel(self) -> None:
        dialog = self._dialog(
            initial=False,
            state=replace(
                IntegrationState(),
                installed_version=__version__,
                menu_entry=False,
                desktop_path="/tmp/Desktop/MailArchive.desktop",
            ),
        )
        self.assertFalse(dialog.menu.get())
        self.assertTrue(dialog.desktop.get())
        self.assertEqual(dialog.title(), "Desktop integration")
        dialog.controls[1].invoke()
        self.cancel.assert_called_once()

    def test_real_worker_install_completes_on_main_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.AppImage"
            source.write_bytes(APPIMAGE)
            icon = directory / "source.svg"
            icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
            paths = IntegrationPaths(directory / "data", directory / "config", directory)
            manager = AppImageIntegration(source, icon, paths)
            background = BackgroundTasks()
            self.addCleanup(background.close, THREAD_TIMEOUT)
            ui = DesktopIntegrationUI(self.root, manager, lambda: False, background.submit)
            with patch("mailarchive.presentation.desktop_setup.messagebox.showinfo") as info:
                ui.configure(initial=True)
                main_thread = threading.get_ident()
                info.side_effect = lambda *args, **kwargs: self.assertEqual(
                    threading.get_ident(), main_thread
                )
                ui._submit(IntegrationOptions())
                self.assertTrue(
                    background.wait(THREAD_TIMEOUT), "Desktop setup worker did not finish"
                )

                def dispatch_completion():
                    background.dispatch()
                    return not ui.busy

                self.wait_for_ui(
                    dispatch_completion,
                    "Desktop setup completion was not dispatched",
                )
                self.assertFalse(ui.busy, "Desktop setup worker did not finish.")
                info.assert_called_once()
            self.assertIsNone(ui.dialog)
            self.assertEqual(paths.application.read_bytes(), APPIMAGE)
            self.assertTrue(paths.menu.is_file())
            self.assertTrue(manager.load_state().prompt_seen)
            self.assertTrue(background.close(THREAD_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
