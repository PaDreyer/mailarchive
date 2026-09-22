from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from mailarchive.models import Account, Settings
from mailarchive.service import ArchiveRunBusyError, ArchiveService, EventLevel, ServiceEvent

STARTUP_DELAY_SECONDS = 30
logger = logging.getLogger(__name__)


def polling_interval_minutes(account: Account, settings: Settings) -> int:
    return account.poll_minutes or settings.default_poll_minutes


class BackgroundRunner:
    def __init__(self, service: ArchiveService, settings_provider: Callable[[], Settings]) -> None:
        self.service = service
        self.settings_provider = settings_provider
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._force = False
        self._request_lock = threading.Lock()
        self._running = False
        self._last_run: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="MailArchive-Polling", daemon=True)
        self._thread.start()

    def run_now(self) -> bool:
        with self._request_lock:
            if self._running or self._force:
                return False
            self._force = True
            self._wake.set()
            return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        # Give startup connections time to settle; manual runs and shutdown wake this wait.
        self._wake.wait(timeout=STARTUP_DELAY_SECONDS)
        self._wake.clear()
        while not self._stop.is_set():
            try:
                self._run_due_accounts()
            except Exception as exc:
                self._report_failure(exc)
            finally:
                with self._request_lock:
                    self._running = False
            self._wake.wait(timeout=15)
            self._wake.clear()

    def _run_due_accounts(self) -> None:
        settings = self.settings_provider()
        now = time.monotonic()
        work_due = self.service.has_automatic_work() is True
        with self._request_lock:
            force = self._force
            self._force = False
            due = {
                account.id
                for account in settings.accounts
                if account.enabled
                and (
                    force
                    or account.id not in self._last_run
                    or now - self._last_run[account.id]
                    >= polling_interval_minutes(account, settings) * 60
                )
            }
            self._running = bool(due or force or work_due)
        if self._running:
            try:
                results = (
                    self.service.run_once(settings, due, force_retry=True)
                    if force
                    else self.service.run_once(settings, due)
                )
            except ArchiveRunBusyError:
                if force:
                    with self._request_lock:
                        self._force = True
                return
            completed_at = time.monotonic()
            for result in results:
                if result.account_id in due:
                    self._last_run[result.account_id] = completed_at

    def _report_failure(self, error: Exception) -> None:
        logger.exception("Archive run failed.")
        try:
            self.service.event_handler(
                ServiceEvent(EventLevel.ERROR, f"Archive run failed: {error}")
            )
        except Exception:
            # Even a failing UI/log callback must not terminate the polling worker.
            logger.exception("Could not report the archive run failure.")
