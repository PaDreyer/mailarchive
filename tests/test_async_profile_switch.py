"""Profile I/O is owned outside the UI callback pool, including real SQLite waits."""

import threading
import time
import unittest
from unittest.mock import patch

from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.domain.configuration import Settings
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from tests import test_execution_outcomes as execution_fixture
from tests.concurrency import THREAD_TIMEOUT
from tests.test_restart_core import Registry


class AsyncProfileSwitchTests(unittest.TestCase):
    def setUp(self):
        self.case = execution_fixture.ExecutionOutcomeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.app = self.case.app
        self.previous = self.app._context
        self.destination = self.case.root / "other" / "workspace.sqlite3"
        store = ConfigStore(self.destination.parent)
        store.save(
            Settings(
                default_poll_minutes=31, automatic_monitoring_paused=True, start_at_login=False
            )
        )
        self.candidate = ProfileDatabase(self.destination)
        self.callbacks = []
        self.owner = threading.get_ident()

    def wait_for(self, predicate):
        deadline = time.monotonic() + THREAD_TIMEOUT
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Profile switch did not settle")
            threading.Event().wait(0.01)

    def callback(self, result):
        self.callbacks.append((threading.get_ident(), result))

    def dispatch_result(self):
        self.wait_for(
            lambda: not any(owner.is_alive() for owner in self.app._profile_switch_threads)
        )
        self.assertEqual(self.callbacks, [])
        self.app.dispatch_callbacks()
        self.assertEqual(len(self.callbacks), 1)
        self.assertEqual(self.callbacks[0][0], self.owner)
        return self.callbacks[0][1]

    def test_busy_candidate_returns_promptly_and_preserves_old_profile_until_opened(self):
        with self.candidate.connection() as db:
            db.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            self.assertTrue(self.app.request_profile_switch(self.destination, self.callback))
            self.assertLess(time.monotonic() - started, 0.3)
            self.wait_for(
                lambda: (
                    self.previous.execution._shutdown_thread is not None
                    and not self.previous.execution._shutdown_thread.is_alive()
                )
            )
            self.assertTrue(any(owner.is_alive() for owner in self.app._profile_switch_threads))
            self.assertEqual(self.app.database_path, self.previous.database_path)
            self.assertEqual(self.app._profiles.path, self.previous.database_path)
            self.assertEqual(
                self.app.automatic_monitoring_state(), AutomaticMonitoringState.UNAVAILABLE
            )
            with self.assertRaisesRegex(RuntimeError, "changing profiles"):
                self.app.request_profile_switch(self.destination, self.callback)
            db.rollback()
        result = self.dispatch_result()
        self.assertIsNone(result.error)
        self.assertEqual(result.value.default_poll_minutes, 31)
        self.assertEqual(self.app.database_path, self.destination)
        self.assertTrue(self.app._context.execution._thread.is_alive())
        self.assertFalse(self.previous.execution._thread.is_alive())
        self.assertEqual(self.app._profiles.path, self.destination)
        self.assertEqual(
            ProfileDatabase(self.previous.database_path).configuration.load_settings().rules,
            self.previous.settings.rules,
        )

    def test_close_zero_tracks_blocked_open_and_discards_late_callback_and_publication(self):
        with self.candidate.connection() as db:
            db.execute("BEGIN EXCLUSIVE")
            self.app.request_profile_switch(self.destination, self.callback)
            self.wait_for(
                lambda: (
                    self.previous.execution._shutdown_thread is not None
                    and not self.previous.execution._shutdown_thread.is_alive()
                )
            )
            started = time.monotonic()
            self.assertFalse(self.app.close(timeout=0))
            self.assertLess(time.monotonic() - started, 0.3)
            db.rollback()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.app.dispatch_callbacks()
        self.assertEqual(self.callbacks, [])
        self.assertEqual(self.app.database_path, self.previous.database_path)
        self.assertEqual(self.app._profiles.path, self.previous.database_path)
        self.assertFalse(any(owner.is_alive() for owner in self.app._profile_switch_threads))
        self.assertEqual(
            ProfileDatabase(self.destination).configuration.load_settings().default_poll_minutes, 31
        )

    def test_fatal_candidate_error_returns_original_profile_with_fresh_worker(self):
        self.destination.write_bytes(b"invalid profile")
        self.app.request_profile_switch(self.destination, self.callback)
        result = self.dispatch_result()
        self.assertIsNotNone(result.error)
        self.assertEqual(self.app.database_path, self.previous.database_path)
        self.assertIsNot(self.app._context.execution, self.previous.execution)
        self.assertTrue(self.app._context.execution._thread.is_alive())
        self.assertEqual(self.app.settings, self.previous.settings)
        self.app._context.execution.service.source_registry = Registry(self.case.source)
        self.assertIsInstance(self.app.check_now(), str)
        self.wait_for(self.app._context.execution.is_idle)

    def test_switch_drains_other_background_work_without_waiting_for_itself(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.app.submit_background(
            lambda: (entered.set(), release.wait(THREAD_TIMEOUT)), lambda result: None
        )
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.app.request_profile_switch(self.destination, self.callback)
        self.assertEqual(self.app.database_path, self.previous.database_path)
        release.set()
        self.assertIsNone(self.dispatch_result().error)

    def test_close_during_candidate_start_cancels_owned_worker_before_publication(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.app._profiles.open
        contexts = []

        def opened(path, *args):
            context = original(path, *args)
            if path == self.destination:
                contexts.append(context)
                start = context.execution.start

                def blocked_start():
                    entered.set()
                    release.wait(THREAD_TIMEOUT)
                    start()

                context.execution.start = blocked_start
            return context

        with patch.object(self.app._profiles, "open", side_effect=opened):
            self.app.request_profile_switch(self.destination, self.callback)
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.assertFalse(self.app.close(timeout=0))
            self.assertTrue(contexts[0].execution._shutdown)
            release.set()
            self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertEqual(self.app.database_path, self.previous.database_path)
        self.assertEqual(self.app._profiles.path, self.previous.database_path)
        self.app.dispatch_callbacks()
        self.assertEqual(self.callbacks, [])

    def test_close_during_startup_configuration_restores_previous_startup_and_location(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        startup_calls = []
        self.candidate.configuration.save_settings(
            Settings(start_at_login=True, automatic_monitoring_paused=True)
        )

        def startup(enabled):
            startup_calls.append(enabled)
            if enabled:
                entered.set()
                release.wait(THREAD_TIMEOUT)

        self.app._configure_startup = startup
        self.app.request_profile_switch(self.destination, self.callback)
        self.assertTrue(entered.wait(THREAD_TIMEOUT))
        self.assertEqual(self.app.database_path, self.previous.database_path)
        self.assertFalse(self.app.close(timeout=0))
        release.set()
        self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertEqual(startup_calls, [True, False])
        self.assertEqual(self.app._profiles.path, self.previous.database_path)
        self.app.dispatch_callbacks()
        self.assertEqual(self.callbacks, [])

    def test_close_during_locator_write_rolls_back_without_late_context_publication(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.app._profiles.activate

        def activate(path):
            original(path)
            if path == self.destination:
                entered.set()
                release.wait(THREAD_TIMEOUT)

        with patch.object(self.app._profiles, "activate", side_effect=activate):
            self.app.request_profile_switch(self.destination, self.callback)
            self.assertTrue(entered.wait(THREAD_TIMEOUT))
            self.assertEqual(self.app.database_path, self.previous.database_path)
            self.assertFalse(self.app.close(timeout=0))
            release.set()
            self.wait_for(lambda: self.app.close(timeout=0.05))
        self.assertEqual(self.app._profiles.path, self.previous.database_path)
        self.assertEqual(self.app.database_path, self.previous.database_path)
        self.app.dispatch_callbacks()
        self.assertEqual(self.callbacks, [])
