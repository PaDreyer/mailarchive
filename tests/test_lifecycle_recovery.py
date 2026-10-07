"""Storage failures cannot prevent owned shutdown or strand profile execution."""

import threading
import time
import unittest
from unittest.mock import patch

from mailarchive.application.errors import ExecutionShutdownError, ShutdownCleanupError
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.domain.source_identity import MessageScope
from mailarchive.infrastructure.profile_database import ProfileDatabase
from tests import test_execution_outcomes as execution_fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import FakeSource, Registry, raw_mail


class SingleRangeSource(FakeSource):
    def search_messages(self, target, should_fetch, _start, _end, *, range_sync, cancellation):
        namespace = self._namespace_for(target)
        scope = MessageScope(namespace, namespace)
        range_sync.start(namespace)

        def messages():
            if should_fetch(scope, "1"):
                yield self.messages["1"]
            range_sync.finish()

        return scope, messages()


class LifecycleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = execution_fixture.ExecutionOutcomeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app = self.case.app
        self.execution = self.app._context.execution
        self.state = self.execution.service
        self.path = self.app.database_path

    def wait_for(self, predicate):
        deadline = time.monotonic() + THREAD_TIMEOUT
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Lifecycle state did not settle")
            threading.Event().wait(0.01)

    def hide_profile(self):
        parent = self.path.parent
        hidden = parent.with_name(parent.name + "-unavailable")
        parent.rename(hidden)
        self.addCleanup(lambda: hidden.rename(parent) if hidden.exists() else None)
        return hidden

    def test_idle_close_finishes_despite_missing_profile_and_preserves_restart_state(self):
        original = self.app.settings
        hidden = self.hide_profile()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertTrue(self.app._background._closed)
        self.assertFalse(self.execution._thread.is_alive())
        self.assertFalse(self.execution._shutdown_thread.is_alive())
        self.assertIn("queued operations", self.app.shutdown_errors[0])
        self.assertFalse(self.path.exists())
        hidden.rename(self.path.parent)
        recovered = ProfileDatabase(self.path, recover=True)
        self.assertEqual(recovered.configuration.load_settings(), original)
        with recovered.connection() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_busy_database_shutdown_obeys_budget_and_tracks_cleanup_io(self):
        operation = self.app.apply_rule_to_past_mail(self.case.rule.id, None, None, "UTC")
        # Stop before dispatch is unnecessary for this check; the lock blocks shutdown settlement.
        with self.state.operations.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            self.assertFalse(self.app.close(timeout=0.05))
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertTrue(self.app._background._closed)
            self.assertTrue(self.execution._shutdown_thread.is_alive())
            db.rollback()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertFalse(self.execution._shutdown_thread.is_alive())
        recovered = ProfileDatabase(self.path, recover=True)
        self.assertIn(
            recovered.operations.manual_operation(operation)["status"],
            {"interrupted", "failed", "stopped"},
        )

    def test_active_manual_close_signals_stream_and_tasks_before_failed_stop_persistence(self):
        entered, release, released = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        source = SingleRangeSource(self.case.source.messages)
        message = source.messages["1"]
        message.raw = None

        def chunks():
            entered.set()
            release.wait(THREAD_TIMEOUT)
            yield raw_mail()

        message.raw_chunks = chunks
        message.release = released.set
        self.state.source_registry = Registry(source)
        operation = self.app.apply_rule_to_past_mail(self.case.rule.id, None, None, "UTC")
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        hidden = self.hide_profile()
        self.assertFalse(self.app.close(timeout=0))
        self.assertTrue(self.state._shutdown_requested.is_set())
        self.assertTrue(self.app._background._closed)
        self.assertTrue(self.execution._active.stop.is_set())
        release.set()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertTrue(released.is_set())
        self.assertGreaterEqual(len(self.app.shutdown_errors), 1)
        hidden.rename(self.path.parent)
        recovered = ProfileDatabase(self.path, recover=True)
        self.assertNotEqual(recovered.operations.manual_operation(operation)["status"], "completed")
        with recovered.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_switch_reports_unavailable_then_reopens_original_without_recreating_it(self):
        self.assertTrue(self.case.check())
        original = self.app.settings
        hidden = self.hide_profile()
        destination = self.case.root / "replacement" / "workspace.sqlite3"
        with self.assertRaises(ExecutionShutdownError):
            self.app.switch_profile(destination, timeout=THREAD_TIMEOUT)
        self.assertFalse(self.execution._thread.is_alive())
        self.assertEqual(
            self.app.automatic_monitoring_state(), AutomaticMonitoringState.UNAVAILABLE
        )
        self.assertFalse(self.path.exists())
        self.assertFalse(destination.exists())
        self.assertFalse(self.path.parent.exists())
        hidden.rename(self.path.parent)
        self.wait_for(lambda: self.app._profile_recovery is None)
        self.assertIsNot(self.app._context.execution, self.execution)
        self.assertTrue(self.app._context.execution._thread.is_alive())
        self.assertEqual(self.app.settings, original)
        self.assertIsInstance(self.app.check_now(), str)
        self.wait_for(self.app._context.execution.is_idle)

    def test_close_during_profile_recovery_never_recreates_or_restarts_original(self):
        hidden = self.hide_profile()
        with self.assertRaises(ExecutionShutdownError):
            self.app.switch_profile(self.case.root / "replacement" / "workspace.sqlite3")
        self.wait_for(lambda: self.app.close(timeout=0.05))
        hidden.rename(self.path.parent)
        self.assertFalse(self.app._recovery_thread.is_alive())
        self.assertFalse(self.execution._thread.is_alive())

    def test_unrecoverable_previous_profile_can_be_retried_or_replaced_explicitly(self):
        original_open = self.app._profiles.open
        destination = self.case.root / "replacement" / "workspace.sqlite3"

        def broken(path, *args):
            if path == destination:
                raise RuntimeError("Candidate rejected")
            raise RuntimeError("Previous profile temporarily rejected")

        with patch.object(self.app._profiles, "open", side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, "Candidate rejected"):
                self.app.switch_profile(destination)
            self.assertEqual(
                self.app.automatic_monitoring_state(), AutomaticMonitoringState.UNAVAILABLE
            )
            with self.assertRaisesRegex(RuntimeError, "previous profile is unavailable"):
                self.app.check_now()
        with self.assertRaisesRegex(RuntimeError, "previous profile is unavailable"):
            self.app.check_now()
        self.wait_for(lambda: self.app._profile_recovery is None)
        self.assertIsInstance(self.app.check_now(), str)
        self.assertIsNot(self.app._context.execution, self.execution)
        self.assertEqual(self.app._profiles.open, original_open)

    def test_background_timeout_keeps_close_false_after_execution_stops(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.app.submit_background(
            lambda: (entered.set(), release.wait(THREAD_TIMEOUT)), lambda _: None
        )
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        hidden = self.hide_profile()
        self.assertFalse(self.app.close(timeout=0.05))
        self.assertTrue(self.app._background._closed)
        self.assertFalse(self.execution._thread.is_alive())
        release.set()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        hidden.rename(self.path.parent)

    def test_account_editor_close_failure_does_not_skip_other_owned_cleanup(self):
        editor = self.app.account_editor(self.app.settings.accounts[0].id)
        with patch.object(editor, "close", side_effect=RuntimeError("Editor close failed")):
            self.wait_for(lambda: self.app.close(timeout=0.05))
        editor.close()
        self.assertTrue(self.app._background._closed)
        self.assertFalse(self.execution._thread.is_alive())
        self.assertIn("Editor close failed", self.app.shutdown_errors[0])

    def test_close_waits_for_background_completion_callbacks_and_actual_pool_threads(self):
        entered, release, work_release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(work_release.set)

        class CallbackQueue:
            def put(self, callback):
                entered.set()
                release.wait(THREAD_TIMEOUT)

        self.app._background._callbacks = CallbackQueue()
        self.app.submit_background(lambda: work_release.wait(THREAD_TIMEOUT), lambda _: None)
        work_release.set()
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.assertTrue(self.app._background.wait(0))
        self.assertFalse(self.app.close(timeout=0.05))
        release.set()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertFalse(any(thread.is_alive() for thread in self.app._background._pool._threads))

    def test_close_during_candidate_open_keeps_io_owned_and_rolls_back_locator(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        destination = self.case.root / "replacement" / "workspace.sqlite3"
        opened, errors = [], []
        original_open = self.app._profiles.open

        def controlled_open(path, *args):
            if path == destination:
                entered.set()
                release.wait(THREAD_TIMEOUT)
            context = original_open(path, *args)
            opened.append(context)
            return context

        def switch():
            try:
                self.app.switch_profile(destination)
            except Exception as exc:
                errors.append(exc)

        with patch.object(self.app._profiles, "open", side_effect=controlled_open):
            caller = threading.Thread(target=switch)
            caller.start()
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                self.assertFalse(self.app.close(timeout=0.05))
                release.set()
                caller.join(THREAD_TIMEOUT)
                self.assertFalse(caller.is_alive())
                self.wait_for(lambda: self.app.close(timeout=0.05))
            finally:
                release.set()
                caller.join(THREAD_TIMEOUT)
        self.assertIn("closing", str(errors[0]))
        self.assertEqual(self.app._profiles.configuration.path, self.path)
        self.assertEqual(self.app.database_path, self.path)
        self.assertIsNone(opened[0].execution._thread)
        self.assertFalse(opened[0].execution._shutdown_thread.is_alive())

    def test_close_during_recovery_open_is_bounded_and_cleans_late_context(self):
        hidden = self.hide_profile()
        with self.assertRaises(ExecutionShutdownError):
            self.app.switch_profile(self.case.root / "replacement" / "workspace.sqlite3")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        opened = []
        original_open = self.app._profiles.open

        def controlled_open(path, *args):
            entered.set()
            release.wait(THREAD_TIMEOUT)
            context = original_open(path, *args)
            opened.append(context)
            return context

        with patch.object(self.app._profiles, "open", side_effect=controlled_open):
            hidden.rename(self.path.parent)
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            started = time.monotonic()
            self.assertFalse(self.app.close(timeout=0.05))
            self.assertLess(time.monotonic() - started, 0.3)
            release.set()
            self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertIsNone(opened[0].execution._thread)
        self.assertFalse(opened[0].execution._shutdown_thread.is_alive())
        self.assertFalse(self.app._recovery_thread.is_alive())

    def test_schedule_write_holding_execution_condition_does_not_delay_stop_signals(self):
        entered = threading.Event()
        storage = self.execution._schedule._storage
        save = storage.save

        def scheduled_write(*args):
            entered.set()
            return save(*args)

        with (
            self.state.operations.connection() as db,
            patch.object(storage, "save", side_effect=scheduled_write),
        ):
            db.execute("BEGIN IMMEDIATE")
            writer = threading.Thread(
                target=lambda: self.execution._record_account_check(self.case.account.id)
            )
            writer.start()
            try:
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                started = time.monotonic()
                self.assertFalse(self.app.close(timeout=0.05))
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(self.state._shutdown_requested.is_set())
            finally:
                db.rollback()
                writer.join(THREAD_TIMEOUT)
        self.assertFalse(writer.is_alive())
        self.wait_for(lambda: self.app.close(timeout=0.05))

    def test_failed_candidate_cleanup_finishes_before_original_profile_reopens(self):
        destination = self.case.root / "replacement" / "workspace.sqlite3"
        original_settings = self.app.settings
        original_open = self.app._profiles.open
        opened, locks = [], []

        def partly_started(path, *args):
            context = original_open(path, *args)
            if path == destination:
                opened.append(context)
                connection = context.execution.service.operations.connection()
                db = connection.__enter__()
                db.execute("BEGIN IMMEDIATE")
                locks.append((db, connection))
                start = context.execution.start

                def start_then_fail():
                    start()
                    raise RuntimeError("Partial worker start failed")

                context.execution.start = start_then_fail
            return context

        with patch.object(self.app._profiles, "open", side_effect=partly_started):
            try:
                with self.assertRaisesRegex(RuntimeError, "Partial worker start failed"):
                    self.app.switch_profile(destination, timeout=0.1)
                self.assertTrue(opened[0].execution._shutdown_thread.is_alive())
                self.assertEqual(
                    self.app.automatic_monitoring_state(), AutomaticMonitoringState.UNAVAILABLE
                )
                self.assertEqual(
                    ProfileDatabase(self.path).configuration.load_settings(), original_settings
                )
                with self.assertRaisesRegex(RuntimeError, "changing profiles"):
                    self.app.check_now()
            finally:
                for db, connection in locks:
                    db.rollback()
                    connection.__exit__(None, None, None)
            self.wait_for(lambda: self.app._profile_recovery is None)
        self.assertFalse(opened[0].execution._thread.is_alive())
        self.assertFalse(opened[0].execution._shutdown_thread.is_alive())
        self.assertIsNot(self.app._context.execution, self.execution)
        self.assertEqual(self.app.database_path, self.path)
        self.assertEqual(self.app._profiles.configuration.path, self.path)
        self.assertEqual(self.app.settings, original_settings)
        self.assertTrue(self.app._context.execution._thread.is_alive())

    def test_unexpected_pool_shutdown_failure_still_stops_execution_and_is_not_hidden(self):
        tasks = self.app._background
        with patch.object(
            tasks._pool, "shutdown", side_effect=RuntimeError("Unexpected pool failure")
        ):
            with self.assertRaisesRegex(ShutdownCleanupError, "Unexpected pool failure"):
                self.app.close(timeout=THREAD_TIMEOUT)
        # The injected pool failure deliberately left its workers running; release them explicitly.
        tasks._pool.shutdown(wait=True, cancel_futures=True)
        tasks._shutdown_error = None
        self.assertFalse(self.execution._thread.is_alive())
        self.assertFalse(self.execution._shutdown_thread.is_alive())

    def test_saved_user_stop_keeps_accepted_work_paused_after_failed_settlement_and_restart(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.state.source_registry = Registry(SingleRangeSource(self.case.source.messages))
        files = self.state.engine.output_files
        publish = files.publish

        def publishing(path, content):
            entered.set()
            release.wait(THREAD_TIMEOUT)
            publish(path, content)

        with patch.object(files, "publish", side_effect=publishing):
            operation = self.app.apply_rule_to_past_mail(self.case.rule.id, None, None, "UTC")
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.app.stop_operation(operation)
            self.assertEqual(
                self.state.operations.manual_operation(operation)["status"], "stopping"
            )
            hidden = self.hide_profile()
            self.assertFalse(self.app.close(timeout=0))
            release.set()
            self.wait_for(lambda: self.app.close(timeout=0.05))
        hidden.rename(self.path.parent)
        recovered = ProfileDatabase(self.path, recover=True)
        self.assertEqual(recovered.operations.manual_operation(operation)["status"], "stopped")
        self.assertFalse(recovered.operations.can_retry_manual_operation(operation))
        with recovered.connection() as db:
            plans = db.execute("SELECT status, raw_path FROM plan").fetchall()
            self.assertEqual([plan["status"] for plan in plans], ["paused"])
            self.assertTrue(recovered.spool.read(plans[0]["raw_path"], 1024 * 1024))
            self.assertEqual(db.execute("SELECT count(*) FROM receipt").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_manual_recovery_retry_keeps_busy_sqlite_io_outside_command_and_close_locks(self):
        destination = self.case.root / "replacement" / "workspace.sqlite3"
        with patch.object(self.app._profiles, "open", side_effect=RuntimeError("Profile rejected")):
            with self.assertRaisesRegex(RuntimeError, "Profile rejected"):
                self.app.switch_profile(destination)
        entered = threading.Event()
        recover = ProfileDatabase._recover_owned_work

        def native_recovery(state):
            entered.set()
            return recover(state)

        with (
            self.state.operations.connection() as db,
            patch.object(ProfileDatabase, "_recover_owned_work", native_recovery),
        ):
            db.execute("BEGIN IMMEDIATE")
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "previous profile is unavailable"):
                    self.app.check_now()
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(entered.wait(THREAD_TIMEOUT))
                started = time.monotonic()
                self.assertFalse(self.app.close(timeout=0))
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertTrue(self.app._recovery_thread.is_alive())
                self.assertTrue(self.app._background._closed)
            finally:
                db.rollback()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertFalse(self.app._recovery_thread.is_alive())
        self.assertFalse(self.app._profile_recovery.stopping.execution._shutdown_thread.is_alive())
        self.assertEqual(
            ProfileDatabase(self.path, recover=True).configuration.load_settings(),
            self.app.settings,
        )

    def test_stale_restore_completion_cannot_finish_new_candidate_sqlite_io(self):
        rejected = self.case.root / "rejected" / "workspace.sqlite3"
        destination = self.case.root / "replacement" / "workspace.sqlite3"
        replacement = ProfileDatabase(destination)
        original_open = self.app._profiles.open
        old_finished, release_old, candidate_entered = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        self.addCleanup(release_old.set)
        failures = []

        def opened(path, *args):
            if path in (rejected, self.path):
                raise RuntimeError("Old profile restore rejected")
            return original_open(path, *args)

        recover = ProfileDatabase._recover_owned_work

        def native_candidate_io(state):
            if state.database_path == destination:
                candidate_entered.set()
            return recover(state)

        def switch():
            try:
                self.app.switch_profile(destination)
            except Exception as exc:
                failures.append(exc)

        with patch.object(self.app._profiles, "open", side_effect=opened):
            with self.assertRaisesRegex(RuntimeError, "Old profile restore rejected"):
                self.app.switch_profile(rejected)
            old_worker = self.app._recovery_thread
            create_transition = self.app._new_profile_transition

            class DelayedCompletion:
                def __init__(self, event):
                    self.event = event

                def set(self):
                    old_finished.set()
                    release_old.wait(THREAD_TIMEOUT)
                    self.event.set()

            def transition():
                event = create_transition()
                return (
                    DelayedCompletion(event) if threading.current_thread() is old_worker else event
                )

            with patch.object(self.app, "_new_profile_transition", side_effect=transition):
                with self.assertRaisesRegex(RuntimeError, "previous profile is unavailable"):
                    self.app.check_now()
                self.assertTrue(old_finished.wait(THREAD_TIMEOUT))
                with (
                    replacement.connection() as db,
                    patch.object(ProfileDatabase, "_recover_owned_work", native_candidate_io),
                ):
                    db.execute("BEGIN IMMEDIATE")
                    caller = threading.Thread(target=switch)
                    caller.start()
                    try:
                        self.assertTrue(candidate_entered.wait(THREAD_TIMEOUT))
                        release_old.set()
                        old_worker.join(THREAD_TIMEOUT)
                        self.assertFalse(old_worker.is_alive())
                        self.assertTrue(caller.is_alive())
                        self.assertFalse(self.app.close(timeout=0.05))
                        self.assertFalse(self.app._profile_transition_done.is_set())
                    finally:
                        release_old.set()
                        db.rollback()
                        caller.join(THREAD_TIMEOUT)
                    self.assertFalse(caller.is_alive())
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertIn("closing", str(failures[0]))
        self.assertEqual(self.app.database_path, self.path)
        self.assertEqual(self.app._profiles.configuration.path, self.path)

    def test_close_tracks_older_recovery_owner_after_latest_reference_is_replaced(self):
        rejected = self.case.root / "rejected" / "workspace.sqlite3"
        old_finished, release_old = threading.Event(), threading.Event()
        self.addCleanup(release_old.set)
        with patch.object(self.app._profiles, "open", side_effect=RuntimeError("Restore rejected")):
            with self.assertRaisesRegex(RuntimeError, "Restore rejected"):
                self.app.switch_profile(rejected)
            old_worker = self.app._recovery_thread
            create_transition = self.app._new_profile_transition

            class DelayedCompletion:
                def __init__(self, event):
                    self.event = event

                def set(self):
                    old_finished.set()
                    release_old.wait(THREAD_TIMEOUT)
                    self.event.set()

            def transition():
                event = create_transition()
                return (
                    DelayedCompletion(event) if threading.current_thread() is old_worker else event
                )

            with patch.object(self.app, "_new_profile_transition", side_effect=transition):
                with self.assertRaisesRegex(RuntimeError, "previous profile is unavailable"):
                    self.app.check_now()
                self.assertTrue(old_finished.wait(THREAD_TIMEOUT))
                with self.assertRaisesRegex(RuntimeError, "Restore rejected"):
                    self.app.switch_profile(
                        self.case.root / "second-rejected" / "workspace.sqlite3"
                    )
                latest = self.app._recovery_thread
                self.assertIsNot(latest, old_worker)
                self.assertFalse(self.app.close(timeout=0.05))
                self.wait_for(lambda: not latest.is_alive())
                self.assertTrue(old_worker.is_alive())
                self.assertFalse(self.app.close(timeout=0))
                release_old.set()
                self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertFalse(old_worker.is_alive())
        self.assertFalse(latest.is_alive())
