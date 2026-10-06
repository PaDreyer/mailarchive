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

from mailarchive.application.account_status import AccountAction, AccountBlocker, AccountState
from mailarchive.application.cancellation import NO_CANCELLATION, Cancellation, ProcessingStopped
from mailarchive.application.events import ExecutionState, RunProgress
from mailarchive.application.polling import (
    STARTUP_DELAY_SECONDS,
    AutomaticMonitoringState,
    PollingSchedule,
    PollingSchedulePort,
)
from mailarchive.application.processing_ports import OperationPort, Record
from mailarchive.application.service import (
    ArchiveService,
    EventLevel,
    ServiceEvent,
)
from mailarchive.domain.configuration import Account, Settings
from mailarchive.domain.rules import has_enabled_rule_for_account

NO_RULES_NOTICE = "No mail checked. Create or enable a rule for an enabled email account."
logger = logging.getLogger(__name__)
_OPERATION_OUTCOMES = {
    "failed": ExecutionState.FAILED,
    "stopped": ExecutionState.STOPPED,
    "stopping": ExecutionState.STOPPED,
    "interrupted": ExecutionState.STOPPED,
}


def _operation_outcome(operation: Record | None) -> ExecutionState:
    if operation is None:
        return ExecutionState.COMPLETED
    return _OPERATION_OUTCOMES.get(
        operation["status"],
        ExecutionState.FAILED if operation["error"] else ExecutionState.COMPLETED,
    )


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
        polling_schedule: PollingSchedulePort | None = None,
        automatic_monitoring_paused: bool = False,
        utc_now: Callable[[], datetime] | None = None,
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
        self._schedule = PollingSchedule(polling_schedule, utc_now=utc_now)
        self._automatic_paused = automatic_monitoring_paused
        self._automatic_wakeup = False
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
            self._schedule.initialize(account.id for account in self.settings_provider().accounts)
            self._thread = threading.Thread(
                target=self._loop, name="MailArchive-Execution", daemon=False
            )
            self._thread.start()

    def automatic_monitoring_state(self) -> AutomaticMonitoringState:
        with self._condition:
            if not self._automatic_paused:
                return AutomaticMonitoringState.ACTIVE
            if self._active is not None and self._active.origin == "automatic":
                return AutomaticMonitoringState.PAUSING
            return AutomaticMonitoringState.PAUSED

    def settings_changed(self, settings: Settings) -> None:
        """Apply only a successfully persisted settings change."""
        with self._condition:
            was_paused = self._automatic_paused
            self._automatic_paused = settings.automatic_monitoring_paused
            if self._automatic_paused:
                request = self._active
                if (
                    request is not None
                    and request.origin == "automatic"
                    and not request.stop.is_set()
                ):
                    request.stop.set()
                    request.state = ExecutionState.STOPPING
                    self._publish(request, "Pausing automatic checks.")
            elif was_paused:
                self._automatic_wakeup = True
            self._condition.notify_all()

    def _record_account_check(self, account_id: str) -> None:
        with self._condition:
            self._schedule.record_completed(account_id)

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
        settings = self.settings_provider()
        accounts = self._eligible_accounts(settings)
        if not accounts:
            message = self._no_accounts_notice(settings)
            self.service.event_handler(ServiceEvent(EventLevel.INFO, message))
            return None
        # Freeze source ownership before accepting the command, also for a stop
        # while it is still queued. This only reads local metadata.
        sources = self.service.automatic_source_intervals(settings)
        with self._condition:
            if self._shutdown or self._check is not None or self._running:
                return None
            request = _Execution("check", sources=sources)
            self._check = request
            self._publish(request, "Waiting for the mail check to start.")
            self._condition.notify_all()
            return request.id

    def _eligible_accounts(self, settings: Settings, *, inspect: bool = False) -> list[Account]:
        return [
            account
            for account in settings.accounts
            if self.service.account_status(account, settings, inspect=inspect).allows(
                AccountAction.CHECK_MAIL
            )
        ]

    def _no_accounts_notice(self, settings: Settings) -> str:
        statuses = [self.service.account_status(account, settings) for account in settings.accounts]
        if any(not status.allows(AccountAction.RETRY_REMOTE) for status in statuses):
            return "No mail checked. Complete account authorization in Accounts first."
        if any(
            AccountBlocker.NO_ACTIVE_RULE in status.blockers
            and not (status.blockers & {AccountBlocker.PAUSED, AccountBlocker.NO_ACTIVE_MAILBOXES})
            for status in statuses
        ):
            return NO_RULES_NOTICE
        return "No enabled mailboxes to check."

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
        settings = self.settings_provider()
        for source_id, (account_id, seconds) in request.sources.items():
            self._deferred_sources[source_id] = finished + seconds
            if has_enabled_rule_for_account(settings.rules, account_id):
                self._schedule.defer_after_stop(account_id)

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
                if not (settle or manual or retry or check):
                    if self._automatic_paused:
                        self._condition.wait()
                        continue
                    if not self._automatic_wakeup and time.monotonic() < deadline:
                        self._condition.wait(timeout=min(15, deadline - time.monotonic()))
                        continue
                    self._automatic_wakeup = False
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
                outcome = ExecutionState.STOPPED
            elif manual:
                self.service.run_range_operation(manual)
                outcome = _operation_outcome(self.operations.manual_operation(manual))
                if outcome == ExecutionState.FAILED:
                    completion = "Mail processing failed."
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
        if not force and (settings.automatic_monitoring_paused or self._automatic_paused):
            return "Automatic checks paused."
        with self._condition:
            self._schedule.initialize(account.id for account in settings.accounts)
        eligible = self._eligible_accounts(settings, inspect=True)
        if force and not eligible:
            return self._no_accounts_notice(settings)
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
        with self._condition:
            due = {
                account.id
                for account in eligible
                if any(
                    mailbox.enabled and mailbox.id not in excluded for mailbox in account.mailboxes
                )
                and (
                    force
                    or self._schedule.is_due(
                        account.id, (account.poll_minutes or settings.default_poll_minutes) * 60
                    )
                )
            }
        if not due and not self.service.has_automatic_work(settings, excluded_source_ids=excluded):
            return "No enabled mailboxes to check."
        self.service.run_once(
            settings,
            due,
            force_retry=force,
            cancellation=cancellation,
            excluded_source_ids=excluded,
            on_account_finished=self._record_account_check,
        )
        cancellation.checkpoint()
        skipped_statuses = [
            self.service.account_status(account, settings) for account in settings.accounts
        ]
        skipped_statuses = [
            status
            for status in skipped_statuses
            if not status.allows(AccountAction.CHECK_MAIL)
            and not status.blockers & {AccountBlocker.PAUSED, AccountBlocker.NO_ACTIVE_MAILBOXES}
        ]
        skipped = len(skipped_statuses)
        if force and skipped:
            noun = "account" if skipped == 1 else "accounts"
            reason = (
                "without an active rule"
                if all(status.state == AccountState.WAITING_FOR_RULE for status in skipped_statuses)
                else "requiring attention"
            )
            return f"Mail check finished. Skipped {skipped} {noun} {reason}."
        return "Mail check finished."

    def _retry_one(self, key: str) -> None:
        if key.startswith("operation:"):
            self.service.retry_waiting_operation_outputs(key.removeprefix("operation:"))
        else:
            self.service.retry_activity(key)
