"""Tk finalizers must finish before later background garbage collection."""

import gc
import threading
import tkinter as tk
import unittest
import weakref
from tkinter import ttk
from unittest.mock import patch

from tests.concurrency import THREAD_TIMEOUT
from tests.tk_test_case import TkTestCase


class TkLifecycleTests(unittest.TestCase):
    def test_fixture_resources_are_released_on_the_owner_thread_for_every_outcome(self):
        automatic_gc = gc.isenabled()
        gc.disable()
        try:
            for outcome in ("success", "failure", "error", "skip"):
                with self.subTest(outcome=outcome):
                    self._check_fixture_outcome(outcome)
        finally:
            gc.collect()
            if automatic_gc:
                gc.enable()

    def _check_fixture_outcome(self, outcome):
        owner_thread = threading.get_ident()
        original_del = tk.Variable.__del__
        references = []
        finalizer_threads = []

        def finalize(variable):
            finalizer_threads.append(threading.get_ident())
            original_del(variable)

        class Fixture(TkTestCase):
            def setUp(self):
                try:
                    self.root = tk.Tk()
                except tk.TclError as exc:
                    self.skipTest(f"Tk display unavailable: {exc}")
                self.addCleanup(self.root.destroy)
                self.root.withdraw()
                self.variable = tk.StringVar(master=self.root, value="test")
                self.entry = ttk.Entry(self.root, textvariable=self.variable)
                self.callback_errors = []
                self.root.report_callback_exception = lambda *error: self.callback_errors.append(
                    error
                )
                references.extend(
                    weakref.ref(item) for item in (self.root, self.variable, self.entry)
                )

            def run_fixture(self):
                self.assertEqual(self.variable.get(), "test")
                if outcome == "failure":
                    self.fail("expected fixture failure")
                if outcome == "error":
                    raise RuntimeError("expected fixture error")
                if outcome == "skip":
                    self.skipTest("expected fixture skip")

        # Retain the completed case, as unittest does for failures.
        fixture = Fixture("run_fixture")
        result = unittest.TestResult()
        with patch.object(tk.Variable, "__del__", finalize):
            fixture.run(result)
            if not references:
                self.skipTest(result.skipped[0][1])
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(bool(result.failures), outcome == "failure")
            self.assertEqual(bool(result.errors), outcome == "error")
            self.assertEqual(bool(result.skipped), outcome == "skip")
            self.assertTrue(all(reference() is None for reference in references))
            self.assertEqual(finalizer_threads, [owner_thread])
            worker = threading.Thread(target=gc.collect)
            worker.start()
            worker.join(timeout=THREAD_TIMEOUT)
            self.assertFalse(worker.is_alive())
            self.assertEqual(finalizer_threads, [owner_thread])
