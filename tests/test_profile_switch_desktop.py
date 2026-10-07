"""Actual desktop profile changes and scaled main actions stay responsive."""

import sqlite3
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from contextlib import ExitStack, closing
from unittest.mock import patch

from mailarchive.application.events import EventLevel, ExecutionState, RunProgress, ServiceEvent
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.domain.configuration import Settings
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from tests import test_desktop_composition as fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.test_settings_layouts import assert_inside
from tests.tk_test_case import TkTestCase


class ProfileSwitchDesktopTests(TkTestCase):
    setUp = fixture.DesktopCompositionTests.setUp

    def advanced_entry(self):
        self.desktop.notebook.select(self.desktop.settings_tab)
        self.desktop.settings_pages.select(1)
        entry = next(
            control
            for control in self.desktop.advanced_settings_scroll.content.winfo_children()
            if control.winfo_class() == "TEntry"
        )
        self.root.focus_force()
        entry.focus_set()
        self.root.update()
        return entry

    def prepare_candidate(self):
        destination = self.application.database_path.parent / "other" / "workspace.sqlite3"
        ConfigStore(destination.parent).save(
            Settings(
                default_poll_minutes=31, start_at_login=False, automatic_monitoring_paused=True
            )
        )
        return destination, ProfileDatabase(destination)

    def test_advanced_enter_with_busy_native_profile_keeps_ui_loop_alive_and_dispatches_completion_on_ui(
        self,
    ):
        destination, candidate = self.prepare_candidate()
        previous = self.application.database_path
        entry = self.advanced_entry()
        owner = threading.get_ident()
        ui_threads = []
        heartbeat = []
        completed = self.desktop._finish_profile_switch

        def finish(*args):
            ui_threads.append(threading.get_ident())
            completed(*args)

        with (
            candidate.connection() as db,
            patch.object(self.desktop, "_finish_profile_switch", side_effect=finish),
        ):
            db.execute("BEGIN EXCLUSIVE")
            self.desktop.database_var.set(str(destination))
            entry.event_generate("<Return>")
            self.root.after(10, lambda: heartbeat.append(True))
            self.wait_for_ui(
                lambda: bool(heartbeat), "The UI timer froze on the candidate SQLite lock"
            )
            self.assertIsNotNone(self.desktop._profile_switch_update)
            self.assertTrue(entry.instate(["disabled"]))
            self.assertIn("Opening", self.desktop.profile_switch_status_var.get())
            self.assertEqual(self.application.database_path, previous)
            self.assertIn("unavailable", self.desktop.automatic_status_var.get())
            db.rollback()
            self.wait_for_ui(
                lambda: self.desktop._profile_switch_update is None,
                "The completed switch was not dispatched",
            )
        self.assertEqual(ui_threads, [owner])
        self.assertEqual(self.application.database_path, destination)
        self.assertEqual(self.desktop.poll_var.get(), "31")
        self.assertEqual(self.desktop.profile_switch_status_var.get(), "")
        self.assertFalse(entry.instate(["disabled"]))

    def test_failed_candidate_preserves_other_unsaved_settings_draft(self):
        entry = self.advanced_entry()
        previous = self.application.database_path
        destination = previous.parent / "invalid" / "workspace.sqlite3"
        destination.parent.mkdir()
        destination.write_bytes(b"invalid")
        self.desktop.poll_var.set("37")
        self.desktop.database_var.set(str(destination))
        with patch("mailarchive.presentation.desktop.messagebox.showerror") as error:
            entry.event_generate("<Return>")
            self.wait_for_ui(
                lambda: self.desktop._profile_switch_update is None,
                "The failed switch was not dispatched",
            )
        error.assert_called_once()
        self.assertEqual(self.desktop.database_var.get(), str(previous))
        self.assertEqual(self.desktop.poll_var.get(), "37")
        self.assertEqual(self.application.settings.default_poll_minutes, 5)
        self.assertFalse(entry.instate(["disabled"]))

    def test_main_tab_footers_wrap_keep_every_action_visible_and_focusable_at_scaled_fonts(self):
        root, desktop = self.root, self.desktop
        scaling = root.tk.call("tk", "scaling")
        self.addCleanup(root.tk.call, "tk", "scaling", scaling)
        for dpi in (96, 120, 144, 192):
            with self.subTest(dpi=dpi):
                root.tk.call("tk", "scaling", dpi / 72)
                for name in tkfont.names(root):
                    font = tkfont.nametofont(name, root)
                    font.configure(size=font.cget("size"))
                width = min(1024, root.winfo_screenwidth())
                height = min(720, root.winfo_screenheight())
                root.geometry(f"{width}x{height}+0+0")
                for tab in (desktop.accounts_tab, desktop.rules_tab, desktop.log_tab):
                    desktop.notebook.select(tab)
                    root.update()
                    actions = [
                        control
                        for container in tab.winfo_children()
                        for control in container.winfo_children()
                        if control.winfo_class() == "TButton"
                    ]
                    self.assertTrue(actions)
                    for control in actions:
                        assert_inside(self, control, tab)
                        self.assertGreaterEqual(control.winfo_height(), control.winfo_reqheight())
                        root.focus_force()
                        control.focus_set()
                        root.update()
                        self.assertIs(root.focus_get(), control)

    def test_quit_during_busy_profile_open_keeps_window_until_owned_io_finishes(self):
        destination, candidate = self.prepare_candidate()
        previous = self.application.database_path
        entry = self.advanced_entry()
        with (
            candidate.connection() as db,
            patch("mailarchive.presentation.desktop.messagebox.showerror") as error,
        ):
            db.execute("BEGIN EXCLUSIVE")
            self.desktop.database_var.set(str(destination))
            entry.event_generate("<Return>")
            self.root.update()
            self.desktop.quit()
            self.assertTrue(self.root.winfo_exists())
            self.assertTrue(self.desktop._closing)
            self.root.after(20, db.rollback)
            with self.tk_timeout(self.root.quit):
                self.root.mainloop()
        try:
            self.assertFalse(self.root.winfo_exists())
        except tk.TclError:
            pass
        self.assertEqual(self.application.database_path, previous)
        self.assertEqual(self.application._profiles.path, previous)
        self.assertFalse(
            any(owner.is_alive() for owner in self.application._profile_switch_threads)
        )
        error.assert_not_called()

    def test_open_activity_window_skips_old_profile_queries_during_busy_switch(self):
        destination, _candidate = self.prepare_candidate()
        self.desktop.show_archive_activity()
        activity = self.desktop.activity_dialog
        self.addCleanup(activity.destroy)
        entry = self.advanced_entry()
        heartbeat = []
        with self.application._context.execution.service.configuration.connection() as db:
            db.execute("BEGIN EXCLUSIVE")
            self.desktop.database_var.set(str(destination))
            entry.event_generate("<Return>")
            activity.after_cancel(activity._refresh_timer)
            activity._refresh_timer = activity.after(1, activity._tick)
            activity.refresh()
            activity.load_more()
            self.root.after(10, lambda: heartbeat.append(True))
            self.wait_for_ui(
                lambda: bool(heartbeat), "Activity reads blocked Tk while the profile worker waited"
            )
            self.assertIsNotNone(self.desktop._profile_switch_update)
            self.assertIn("temporarily unavailable", activity.detail_summary.get())
            self.assertTrue(activity.stop_button.instate(["disabled"]))
            db.rollback()
            self.wait_for_ui(
                lambda: self.desktop._profile_switch_update is None,
                "The profile switch did not finish",
            )
        self.assertEqual(activity._profile_path, destination)
        self.assertEqual(activity.current_items, ())
        self.assertEqual(activity.history_items, [])

    def test_timeout_keeps_all_profile_reads_off_tk_until_same_profile_recovery(self):
        destination, _candidate = self.prepare_candidate()
        app, desktop, root = self.application, self.desktop, self.root
        previous = app._context
        previous_revision = app.account_statuses.revision
        app._context.report(ServiceEvent(EventLevel.INFO, "Original profile log"))
        desktop.refresh_log()
        desktop.show_archive_activity()
        activity = desktop.activity_dialog
        self.addCleanup(activity.destroy)
        entry = self.advanced_entry()
        locked, release = threading.Event(), threading.Event()

        def writer():
            with closing(sqlite3.connect(previous.database_path, timeout=15)) as db:
                db.execute("BEGIN EXCLUSIVE")
                locked.set()
                release.wait(THREAD_TIMEOUT * 2)
                db.rollback()

        worker = threading.Thread(target=writer)
        worker.start()
        self.addCleanup(lambda: worker.join(THREAD_TIMEOUT))
        self.addCleanup(release.set)
        self.assertTrue(locked.wait(THREAD_TIMEOUT))
        ticks = []
        timer = None

        def heartbeat():
            nonlocal timer
            ticks.append(time.monotonic())
            timer = root.after(10, heartbeat)

        self.addCleanup(lambda: root.after_cancel(timer) if timer is not None else None)
        errors = []
        request = app.request_profile_switch
        read_names = (
            "status",
            "account_status",
            "activity_log_page",
            "current_jobs",
            "activity_page",
            "paused_scopes",
        )
        with ExitStack() as stack:
            reads = {
                name: stack.enter_context(patch.object(app, name, wraps=getattr(app, name)))
                for name in read_names
            }
            row_refresh = stack.enter_context(
                patch.object(desktop, "_refresh_account_rows", wraps=desktop._refresh_account_rows)
            )
            stack.enter_context(
                patch(
                    "mailarchive.presentation.desktop.messagebox.showerror",
                    side_effect=lambda *args, **kwargs: errors.append(args),
                )
            )
            stack.enter_context(
                patch.object(
                    app,
                    "request_profile_switch",
                    side_effect=lambda path, callback: request(path, callback, timeout=0.05),
                )
            )
            desktop.database_var.set(str(destination))
            entry.event_generate("<Return>")
            heartbeat()
            self.wait_for_ui(
                lambda: bool(errors) and desktop._profile_switch_update is None,
                "The switch timeout was not dispatched to Tk",
            )
            self.assertEqual(app.automatic_monitoring_state(), AutomaticMonitoringState.UNAVAILABLE)
            self.assertFalse(entry.instate(["disabled"]))
            self.assertTrue(desktop.check_button.instate(["disabled"]))
            self.assertEqual(app.database_path, previous.database_path)
            self.assertEqual(app.account_statuses.revision, previous_revision)
            baseline = len(ticks)
            desktop.on_service_event(ServiceEvent(EventLevel.ERROR, "Queued old-profile error"))
            desktop.on_run_progress(
                RunProgress(
                    "Old work stopped",
                    active=False,
                    origin="automatic",
                    state=ExecutionState.STOPPED,
                )
            )
            desktop.refresh_all()
            desktop.refresh_log()
            desktop._refresh_account_notice()
            desktop.clear_log()
            desktop.reset_paused_folder()
            activity.refresh()
            activity.load_more()
            self.wait_for_ui(
                lambda: len(ticks) >= baseline + 8,
                "Tk stopped responding after the profile timeout",
            )
            self.assertEqual(len(errors), 1)
            self.assertTrue(desktop.check_button.instate(["disabled"]))
            self.assertFalse(entry.instate(["disabled"]))
            self.assertEqual(app.automatic_monitoring_state(), AutomaticMonitoringState.UNAVAILABLE)
            self.assertFalse(release.is_set())
            self.assertTrue(all(read.call_count == 0 for read in reads.values()))
            unavailable_row_refreshes = row_refresh.call_count
            release.set()
            self.wait_for_ui(
                lambda: (
                    app.automatic_monitoring_state() != AutomaticMonitoringState.UNAVAILABLE
                    and desktop._monitoring_state != AutomaticMonitoringState.UNAVAILABLE
                ),
                "The same-profile recovery was not reflected by the UI",
            )
            self.assertEqual(app.database_path, previous.database_path)
            self.assertEqual(app.account_statuses.revision, previous_revision)
            self.assertIsNot(app._context.execution, previous.execution)
            self.assertTrue(app._context.execution._thread.is_alive())
            self.assertGreater(row_refresh.call_count, unavailable_row_refreshes)
            for name in ("status", "activity_log_page", "current_jobs", "activity_page"):
                self.assertGreater(reads[name].call_count, 0, name)
            self.assertEqual(desktop.profile_switch_status_var.get(), "")
            self.assertNotIn("unavailable", desktop.archive_summary.get())
            self.assertNotIn("unavailable", desktop.log_summary_var.get())
            self.assertTrue(
                any(
                    "Original profile log" in row
                    for row in (
                        desktop.log_tree.item(key, "values")
                        for key in desktop.log_tree.get_children()
                    )
                )
            )
            self.assertFalse(desktop.check_button.instate(["disabled"]))
            self.assertEqual(desktop.check_button.cget("text"), "Check mail now")
            with patch.object(app, "check_now", wraps=app.check_now) as check:
                desktop.check_button.invoke()
                check.assert_called_once_with()
            self.wait_for_ui(
                lambda: desktop.progress_var.get() == "No enabled mailboxes to check.",
                "The recovered Check mail now action did not reach its empty-profile preflight",
            )
            self.assertIsNone(desktop._check_id)
            self.assertFalse(desktop.check_button.instate(["disabled"]))
            self.assertEqual(desktop.check_button.cget("text"), "Check mail now")
            self.assertEqual(len(errors), 1)
        gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False)]
        self.assertLess(max(gaps), 0.5, "A Tk callback read locked profile SQL after the timeout")

    def test_normal_monitoring_state_changes_preserve_log_paging_policy(self):
        desktop, app = self.desktop, self.application
        desktop.refresh_log()
        self.assertTrue(desktop.log_previous_button.instate(["disabled"]))
        self.assertTrue(desktop.log_next_button.instate(["disabled"]))
        app.set_automatic_monitoring_paused(True)
        desktop._refresh_monitoring_controls()
        self.assertTrue(desktop.log_previous_button.instate(["disabled"]))
        self.assertTrue(desktop.log_next_button.instate(["disabled"]))
        app.set_automatic_monitoring_paused(False)
        desktop._refresh_monitoring_controls()
        self.assertTrue(desktop.log_previous_button.instate(["disabled"]))
        self.assertTrue(desktop.log_next_button.instate(["disabled"]))

    def test_stopping_background_work_keeps_its_label_across_monitoring_changes(self):
        desktop, app = self.desktop, self.application
        for origin in ("operation", "automatic"):
            with self.subTest(origin=origin):
                execution_id = f"stopping-{origin}"
                desktop.on_run_progress(
                    RunProgress(
                        "Stopping background work",
                        execution_id=execution_id,
                        origin=origin,
                        state=ExecutionState.STOPPING,
                        sequence=1,
                    )
                )
                self.wait_for_ui(
                    lambda: desktop.check_button.cget("text") == "Stopping",
                    "The typed stopping state was lost while rendering background work",
                )
                self.assertTrue(desktop.check_button.instate(["disabled"]))
                app.set_automatic_monitoring_paused(True)
                desktop._refresh_monitoring_controls()
                self.assertEqual(desktop.check_button.cget("text"), "Stopping")
                self.assertTrue(desktop.check_button.instate(["disabled"]))
                desktop.on_run_progress(
                    RunProgress(
                        "Background work stopped",
                        active=False,
                        execution_id=execution_id,
                        origin=origin,
                        state=ExecutionState.STOPPED,
                        sequence=2,
                    )
                )
                self.wait_for_ui(
                    lambda: desktop.check_button.cget("text") == "Check mail now",
                    "The stopped background work did not release the check action",
                )
                self.assertFalse(desktop.check_button.instate(["disabled"]))
                app.set_automatic_monitoring_paused(False)
                desktop._refresh_monitoring_controls()
