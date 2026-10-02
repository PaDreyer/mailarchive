"""One worker owns mail polling, manual operations, and output retries."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from mailarchive.application.events import RunProgress
from mailarchive.application.processing_ports import OperationPort
from mailarchive.application.service import (
    ArchiveRunBusyError,
    ArchiveService,
    EventLevel,
    ServiceEvent,
)
from mailarchive.domain.configuration import Settings

STARTUP_DELAY_SECONDS = 30
logger = logging.getLogger(__name__)


class ExecutionCoordinator:
    """Serializes all processing in one background worker.

    Mutating UI calls only enqueue work or persist a stop gate. The worker owns
    provider access and file publication, including bounded shutdown handling.
    """

    def __init__(
        self,
        service: ArchiveService,
        settings_provider: Callable[[], Settings],
        operations: OperationPort,
    ) -> None:
        self.service = service
        self.settings_provider = settings_provider
        self.operations = operations
        self._condition = threading.Condition()
        self._shutdown = False
        self._thread: threading.Thread | None = None
        self._manual: deque[str] = deque()
        self._retry: deque[str] = deque()
        self._settle: deque[str] = deque()
        self._force = False
        self._running = False
        self._active_operation: str | None = None
        self._last_run: dict[str, float] = {}

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            if self._shutdown:
                raise RuntimeError("The execution coordinator has shut down.")
            self._thread = threading.Thread(
                target=self._loop, name="MailArchive-Execution", daemon=False
            )
            self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> bool:
        if timeout < 0:
            raise ValueError("The shutdown timeout cannot be negative.")
        with self._condition:
            self._shutdown = True
            active = self._active_operation
            self._condition.notify_all()
            thread = self._thread
        if active:
            self.operations.request_stop_manual_operation(active)
        self.service.request_shutdown()
        if thread and thread.is_alive():
            thread.join(timeout)
        stopped = not (thread and thread.is_alive())
        if stopped:
            self.operations.interrupt_queued_manual_operations()
        return stopped

    def check_mail_now(self) -> bool:
        with self._condition:
            if self._shutdown or self._force or self._running:
                return False
            self._force = True
            self._condition.notify_all()
            return True

    def apply_to_past_mail(
        self,
        rule_id: str,
        start: datetime | None,
        end: datetime | None,
        timezone_name: str,
    ) -> str:
        settings = self.settings_provider()
        rule = next((item for item in settings.rules if item.id == rule_id), None)
        if rule is None or not rule.enabled:
            raise ValueError("Select an enabled rule for the past-mail run.")
        source_ids = {
            mailbox.id
            for account in settings.accounts
            if account.enabled and (rule.account_ids is None or account.id in rule.account_ids)
            for mailbox in account.mailboxes
            if mailbox.enabled
        }
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Mail processing is shutting down.")
            operation_id = self.service.prepare_range_operation(
                settings,
                source_ids,
                rule_id=rule_id,
                start=start,
                end=end,
                timezone_name=timezone_name,
            )
            self._manual.append(operation_id)
            self._condition.notify_all()
            return operation_id

    def stop_operation(self, operation_id: str) -> None:
        operation_id = operation_id.removeprefix("operation:")
        if not self.operations.request_stop_manual_operation(operation_id):
            return
        with self._condition:
            self._manual = deque(
                candidate for candidate in self._manual if candidate != operation_id
            )
            if operation_id not in self._settle:
                self._settle.append(operation_id)
            self._condition.notify_all()

    def retry_activity(self, key: str) -> bool:
        with self._condition:
            if self._shutdown:
                return False
            if key.startswith("operation:"):
                operation_id = key.removeprefix("operation:")
                operation = self.operations.manual_operation(operation_id)
                if operation is None or operation["status"] not in {
                    "failed",
                    "interrupted",
                    "waiting",
                }:
                    return False
                if operation_id not in self._manual:
                    self._manual.append(operation_id)
            elif key.startswith("mail:"):
                if not self.service.can_retry_mail(key.removeprefix("mail:")):
                    return False
                if key not in self._retry:
                    self._retry.append(key)
            else:
                return False
            self._condition.notify_all()
            return True

    def reset_schedule(self) -> None:
        """Call after a settings or profile change while the worker is idle."""
        with self._condition:
            if self._running or self._active_operation:
                raise ArchiveRunBusyError("MailArchive is processing another operation.")
            self._last_run.clear()
            self._force = False
            self._condition.notify_all()

    def is_idle(self) -> bool:
        with self._condition:
            return not self._running and self._active_operation is None

    def _loop(self) -> None:
        deadline = time.monotonic() + STARTUP_DELAY_SECONDS
        while True:
            with self._condition:
                if self._shutdown:
                    return
                settle = self._settle.popleft() if self._settle else None
                manual = self._manual.popleft() if not settle and self._manual else None
                retry = self._retry.popleft() if not settle and not manual and self._retry else None
                force = self._force if not settle and not manual and not retry else False
                if force:
                    self._force = False
                if (
                    settle is None
                    and manual is None
                    and retry is None
                    and not force
                    and time.monotonic() < deadline
                ):
                    self._condition.wait(timeout=min(15, deadline - time.monotonic()))
                    continue
                self._running = True
                self._active_operation = manual or settle
            check_completion = "Mail check failed."
            try:
                if settle:
                    self.operations.finalize_stop_manual_operation(settle)
                elif manual:
                    self.service.run_range_operation(manual)
                elif retry:
                    self._retry_one(retry)
                else:
                    check_completion = self._poll(force)
            except Exception as exc:
                logger.exception("Mail processing failed")
                try:
                    self.service.event_handler(
                        ServiceEvent(EventLevel.ERROR, f"Mail processing failed: {exc}")
                    )
                except Exception:
                    logger.exception("Could not report mail processing failure")
            finally:
                if force:
                    # Every accepted Check mail now request completes, including
                    # empty selections and failures before the service starts.
                    try:
                        self.service.progress_handler(RunProgress(check_completion, active=False))
                    except Exception:
                        logger.exception("Could not report mail check completion")
                with self._condition:
                    self._running = False
                    self._active_operation = None
                    deadline = time.monotonic() + 15
                    self._condition.notify_all()

    def _poll(self, force: bool) -> str:
        settings = self.settings_provider()
        current = time.monotonic()
        due = {
            account.id
            for account in settings.accounts
            if account.enabled
            and any(mailbox.enabled for mailbox in account.mailboxes)
            and (
                force
                or account.id not in self._last_run
                or current - self._last_run[account.id]
                >= (account.poll_minutes or settings.default_poll_minutes) * 60
            )
        }
        if not due and not self.service.has_automatic_work():
            return "No enabled mailboxes to check."
        results = self.service.run_once(settings, due, force_retry=force)
        finished = time.monotonic()
        for result in results:
            if result.account_id in due:
                self._last_run[result.account_id] = finished
        if not any(rule.enabled for rule in settings.rules):
            return "Mail check finished. No enabled rules are configured."
        return "Mail check finished."

    def _retry_one(self, key: str) -> None:
        if key.startswith("operation:"):
            self.service.retry_waiting_operation_outputs(key.removeprefix("operation:"))
        else:
            self.service.retry_activity(key)
