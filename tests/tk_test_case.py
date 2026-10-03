"""Finish Tk object ownership before another test can start a worker."""

import gc
import unittest
from contextlib import contextmanager

from tests.concurrency import THREAD_TIMEOUT

UI_POLL_INTERVAL_MS = 10


class TkTestCase(unittest.TestCase):
    def run(self, result=None):
        initial_attributes = set(vars(self))
        try:
            return super().run(result)
        finally:
            # addCleanup runs while fixture attributes still retain the widgets.
            # Release them after all cleanup callbacks and test frames have left.
            for name in set(vars(self)) - initial_attributes:
                delattr(self, name)
            gc.collect()

    @contextmanager
    def tk_timeout(self, abort):
        """Unblock a nested Tk loop on failure, never advance a successful test."""
        expired = False

        def expire():
            nonlocal expired
            expired = True
            abort()

        timer = self.root.after(THREAD_TIMEOUT * 1000, expire)
        try:
            yield
        finally:
            self.root.after_cancel(timer)
        self.assertFalse(expired, "The GUI action did not finish")

    def wait_for_ui(self, predicate, message):
        """Run the real event loop until the asserted UI state is observable."""
        timer = None
        errors = []

        def check():
            nonlocal timer
            try:
                if predicate():
                    self.root.quit()
                else:
                    timer = self.root.after(UI_POLL_INTERVAL_MS, check)
            except Exception as exc:
                errors.append(exc)
                self.root.quit()

        with self.tk_timeout(self.root.quit):
            timer = self.root.after_idle(check)
            try:
                self.root.mainloop()
            finally:
                self.root.after_cancel(timer)
        if errors:
            raise errors[0]
        self.assertTrue(predicate(), message)
