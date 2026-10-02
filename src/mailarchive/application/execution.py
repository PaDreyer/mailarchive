"""One worker owns mail polling, manual operations, and output retries."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation, ProcessingStopped
from mailarchive.application.events import ExecutionState, RunProgress
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


@dataclass(slots=True)
class _Execution:
    origin: str
    id: str = field(default_factory=lambda: str(uuid4()))
    state: ExecutionState = ExecutionState.QUEUED
    stop: threading.Event = field(default_factory=threading.Event)
    sources: dict[str, tuple[str, int]] = field(default_factory=dict)
    announced: bool = False


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
        *,
        progress_handler: Callable[[RunProgress], None] | None = None,
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
        self._check: _Execution | None = None
        self._running = False
        self._active_operation: str | None = None
        self._last_run: dict[str, float] = {}
        self._deferred_sources: dict[str, float] = {}
        self._active: _Execution | None = None
        self._sequence = 0
        self._progress_handler = progress_handler or (lambda progress: None)
        service.progress_handler = self._service_progress

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
            if self._check is not None:
                self.stop_check(self._check.id)
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

    def check_mail_now(self) -> str | None:
        with self._condition:
            if self._shutdown or self._check is not None or self._running:
                return None
        # Freeze source ownership before accepting the command, also for a stop
        # while it is still queued. This only reads local metadata.
        sources = self.service.automatic_source_intervals(self.settings_provider())
        with self._condition:
            if self._shutdown or self._check is not None or self._running:
                return None
            request = _Execution("check", sources=sources)
            self._check = request
            self._publish(request, "Waiting for the mail check to start.")
            self._condition.notify_all()
            return request.id

    def stop_check(self, check_id: str) -> bool:
        with self._condition:
            request = self._check
            if request is None or request.id != check_id:
                return False
            if request.state == ExecutionState.STOPPING:
                return True
            request.stop.set()
            if request.state == ExecutionState.QUEUED:
                self._defer_check(request)
                self._check = None
                request.state = ExecutionState.STOPPED
                self._publish(request, "Mail check stopped.")
                self._report_stopped_check()
            else:
                request.state = ExecutionState.STOPPING
                self._publish(request, "Stopping the mail check.")
            self._condition.notify_all()
            return True

    def _publish(self, request: _Execution, message: str) -> None:
        """Called under the condition so observers see transitions in order."""
        self._sequence += 1
        request.announced = True
        try:
            self._progress_handler(
                RunProgress(
                    message,
                    active=request.state.active,
                    execution_id=request.id,
                    origin=request.origin,
                    state=request.state,
                    sequence=self._sequence,
                )
            )
        except Exception:
            logger.exception("Could not report processing progress")

    def _service_progress(self, message: str) -> None:
        with self._condition:
            request = self._active
            if request is not None and not request.stop.is_set():
                self._publish(request, message)

    def _defer_check(self, request: _Execution) -> None:
        finished = time.monotonic()
        for source_id, (account_id, seconds) in request.sources.items():
            self._deferred_sources[source_id] = finished + seconds
            self._last_run[account_id] = finished

    def _report_stopped_check(self) -> None:
        try:
            self.service.event_handler(ServiceEvent(EventLevel.INFO, "Mail check stopped."))
        except Exception:
            logger.exception("Could not report mail check stop")

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
            if self._running or self._active_operation or self._check is not None:
                raise ArchiveRunBusyError("MailArchive is processing another operation.")
            self._last_run.clear()
            self._condition.notify_all()

    def is_idle(self) -> bool:
        with self._condition:
            return not self._running and self._active_operation is None and self._check is None

    def _loop(self) -> None:
        deadline = time.monotonic() + STARTUP_DELAY_SECONDS
        while True:
            with self._condition:
                if self._shutdown:
                    return
                settle = self._settle.popleft() if self._settle else None
                manual = self._manual.popleft() if not settle and self._manual else None
                retry = self._retry.popleft() if not settle and not manual and self._retry else None
                check = self._check if not settle and not manual and not retry else None
                if not (settle or manual or retry or check) and time.monotonic() < deadline:
                    self._condition.wait(timeout=min(15, deadline - time.monotonic()))
                    continue
                request = check or _Execution(
                    "operation" if settle or manual else "retry" if retry else "automatic"
                )
                self._running = True
                self._active_operation = manual or settle
                self._active = request
                request.state = ExecutionState.RUNNING
                if request.origin != "automatic":
                    self._publish(request, "Checking mail." if check else "Processing mail.")
            self._execute(request, settle=settle, manual=manual, retry=retry)
            deadline = time.monotonic() + 15

    def _execute(
        self, request: _Execution, *, settle: str | None, manual: str | None, retry: str | None
    ) -> None:
        completion = "Mail processing finished."
        outcome = ExecutionState.COMPLETED
        try:
            if settle:
                self.operations.finalize_stop_manual_operation(settle)
            elif manual:
                self.service.run_range_operation(manual)
            elif retry:
                self._retry_one(retry)
            else:
                completion = self._poll(request.origin == "check", request=request)
        except ProcessingStopped:
            outcome = ExecutionState.STOPPED
        except Exception as exc:
            if request.stop.is_set():
                outcome = ExecutionState.STOPPED
            else:
                outcome = ExecutionState.FAILED
                completion = (
                    "Mail check failed." if request.origin == "check" else "Mail processing failed."
                )
                self._report_failure(exc)
        finally:
            with self._condition:
                if request.stop.is_set():
                    outcome = ExecutionState.STOPPED
                if outcome == ExecutionState.STOPPED:
                    completion = (
                        "Mail check stopped."
                        if request.origin == "check"
                        else "Mail processing stopped."
                    )
                    if request.origin == "check":
                        self._defer_check(request)
                        self._report_stopped_check()
                request.state = outcome
                self._running = False
                self._active_operation = None
                self._active = None
                if self._check is request:
                    self._check = None
                if request.announced:
                    self._publish(request, completion)
                self._condition.notify_all()

    def _report_failure(self, error: Exception) -> None:
        logger.exception("Mail processing failed")
        try:
            self.service.event_handler(
                ServiceEvent(EventLevel.ERROR, f"Mail processing failed: {error}")
            )
        except Exception:
            logger.exception("Could not report mail processing failure")

    def _poll(self, force: bool, *, request: _Execution | None = None) -> str:
        settings = self.settings_provider()
        current = time.monotonic()
        self._deferred_sources = {
            source: deadline
            for source, deadline in self._deferred_sources.items()
            if deadline > current
        }
        if force:
            excluded = frozenset()
            if request is not None:
                request.sources.update(self.service.automatic_source_intervals(settings))
                for source in request.sources:
                    self._deferred_sources.pop(source, None)
        else:
            excluded = frozenset(self._deferred_sources)
        cancellation = Cancellation(request.stop.is_set) if request is not None else NO_CANCELLATION
        cancellation.checkpoint()
        due = {
            account.id
            for account in settings.accounts
            if account.enabled
            and any(mailbox.enabled and mailbox.id not in excluded for mailbox in account.mailboxes)
            and (
                force
                or account.id not in self._last_run
                or current - self._last_run[account.id]
                >= (account.poll_minutes or settings.default_poll_minutes) * 60
            )
        }
        if not due and not self.service.has_automatic_work(excluded_source_ids=excluded):
            return "No enabled mailboxes to check."
        results = self.service.run_once(
            settings,
            due,
            force_retry=force,
            cancellation=cancellation,
            excluded_source_ids=excluded,
        )
        cancellation.checkpoint()
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
