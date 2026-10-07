"""Actual desktop shutdown and recovery remain usable after profile I/O failures."""

import tkinter as tk
from unittest.mock import patch

from mailarchive.application.errors import ExecutionShutdownError
from mailarchive.application.polling import AutomaticMonitoringState
from tests import test_desktop_composition as desktop_fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import Registry
from tests.tk_test_case import TkTestCase


class LifecycleDesktopTests(TkTestCase):
    setUp = desktop_fixture.DesktopCompositionTests.setUp
    configure_stoppable_check = desktop_fixture.DesktopCompositionTests.configure_stoppable_check
    wait_until_idle = desktop_fixture.DesktopCompositionTests.wait_until_idle

    def hide_profile(self):
        parent = self.application.database_path.parent
        hidden = parent.with_name(parent.name + "-unavailable")
        parent.rename(hidden)
        self.addCleanup(lambda: hidden.rename(parent) if hidden.exists() else None)
        return parent, hidden

    def root_exists(self):
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def wait_for_close(self):
        with self.tk_timeout(self.root.quit):
            self.root.mainloop()
        self.assertFalse(self.root_exists())

    def test_idle_quit_closes_unavailable_profile_with_one_recovery_warning(self):
        self.assertTrue(self.application._background.wait(THREAD_TIMEOUT))
        parent, hidden = self.hide_profile()
        with patch("mailarchive.presentation.desktop.messagebox.showwarning") as warning:
            self.desktop.quit()
            if self.root_exists():
                self.wait_for_close()
        warning.assert_called_once()
        self.assertIn("next start will recover", warning.call_args.args[1])
        execution = self.application._context.execution
        self.assertFalse(execution._thread.is_alive())
        self.assertFalse(execution._shutdown_thread.is_alive())
        self.assertFalse(self.application._background._shutdown_thread.is_alive())
        self.assertFalse(parent.exists())
        hidden.rename(parent)

    def test_active_check_quit_delivers_cancellation_without_waiting_for_database(self):
        self.application.set_automatic_monitoring_paused(True)
        source = self.configure_stoppable_check("download")
        self.addCleanup(source.release.set)
        self.desktop.check_button.invoke()
        self.assertTrue(source.entered.wait(THREAD_TIMEOUT))
        parent, hidden = self.hide_profile()
        with patch("mailarchive.presentation.desktop.messagebox.showwarning") as warning:
            self.desktop.quit()
            self.assertTrue(
                self.application._context.execution.service._shutdown_requested.is_set()
            )
            self.assertTrue(self.application._background._closed)
            self.assertTrue(self.root_exists())
            source.release.set()
            self.wait_for_close()
        warning.assert_called_once()
        self.assertTrue(source.closed.is_set())
        self.assertFalse(self.application._context.execution._thread.is_alive())
        hidden.rename(parent)

    def test_failed_switch_displays_unavailable_then_actual_check_works_after_recovery(self):
        source = self.configure_stoppable_check("none")
        self.desktop.settings = self.application.settings
        self.desktop.refresh_all()
        self.desktop.check_button.invoke()
        self.wait_until_idle("Mail check finished.")
        parent, hidden = self.hide_profile()
        with self.assertRaises(ExecutionShutdownError):
            self.application.switch_profile(
                parent.with_name(parent.name + "-new") / "workspace.sqlite3"
            )
        self.desktop._refresh_monitoring_controls()
        self.assertIn("unavailable", self.desktop.automatic_status_var.get())
        self.assertTrue(self.desktop.automatic_button.instate(["disabled"]))
        hidden.rename(parent)
        self.wait_for_ui(
            lambda: (
                self.application.automatic_monitoring_state()
                != AutomaticMonitoringState.UNAVAILABLE
                and self.desktop._monitoring_state != AutomaticMonitoringState.UNAVAILABLE
                and not self.desktop.check_button.instate(["disabled"])
            ),
            "The restored original profile did not receive a new execution worker",
        )
        self.application._context.execution.service.source_registry = Registry(source)
        self.desktop.check_button.invoke()
        self.wait_until_idle("Mail check finished.")
        self.assertTrue(self.application._context.execution._thread.is_alive())
        self.assertFalse(self.desktop.automatic_button.instate(["disabled"]))
