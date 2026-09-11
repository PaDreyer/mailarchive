from __future__ import annotations

import threading
import time
from collections.abc import Callable

from mailarchive.models import Account, Settings
from mailarchive.service import ArchiveService


def polling_interval_minutes(account: Account, settings: Settings) -> int:
    return account.poll_minutes or settings.default_poll_minutes


class BackgroundRunner:
    def __init__(
        self, service: ArchiveService, settings_provider: Callable[[], Settings]
    ) -> None:
        self.service = service
        self.settings_provider = settings_provider
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._force = False
        self._last_run: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="MailArchive-Polling", daemon=True)
        self._thread.start()

    def run_now(self) -> None:
        self._force = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self.settings_provider()
            now = time.monotonic()
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
            if due:
                self.service.run_once(settings, due)
                completed_at = time.monotonic()
                for account_id in due:
                    self._last_run[account_id] = completed_at
            self._wake.wait(timeout=15)
            self._wake.clear()
