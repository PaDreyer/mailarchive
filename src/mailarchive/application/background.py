"""Owned background tasks with completion callbacks dispatched by the UI loop."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BackgroundResult(Generic[T]):
    value: T | None = None
    error: Exception | None = None


class BackgroundTasks:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="MailArchive-Task")
        self._futures: set[Future] = set()
        self._lock = threading.Lock()
        self._callbacks: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._closed = False
        self._shutdown_thread: threading.Thread | None = None
        self._shutdown_error: Exception | None = None

    def submit(
        self, work: Callable[[], T], callback: Callable[[BackgroundResult[T]], None]
    ) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("MailArchive is closing.")
            future = self._pool.submit(work)
            self._futures.add(future)

        def completed(done: Future[T]) -> None:
            if not done.cancelled():
                try:
                    result = BackgroundResult(value=done.result())
                except Exception as exc:
                    result = BackgroundResult(error=exc)
                self._callbacks.put(lambda: callback(result))
            with self._lock:
                self._futures.discard(done)

        future.add_done_callback(completed)

    def post(self, callback: Callable[[], None]) -> None:
        """Queue an owned worker's completion without putting it in this pool."""
        with self._lock:
            if not self._closed:
                self._callbacks.put(callback)

    def dispatch(self) -> None:
        while True:
            try:
                callback = self._callbacks.get_nowait()
            except queue.Empty:
                return
            callback()

    def wait(self, timeout: float) -> bool:
        with self._lock:
            pending = tuple(self._futures)
        return not pending or not wait(pending, timeout=timeout).not_done

    def close(self, timeout: float) -> bool:
        if timeout < 0:
            raise ValueError("The shutdown timeout cannot be negative.")
        with self._lock:
            self._closed = True
            if self._shutdown_thread is None:
                self._shutdown_thread = threading.Thread(
                    target=self._finish_shutdown,
                    name="MailArchive-TaskShutdown",
                    daemon=False,
                )
                self._shutdown_thread.start()
            shutdown = self._shutdown_thread
        shutdown.join(timeout)
        if self._shutdown_error is not None:
            raise RuntimeError(
                f"Could not stop background tasks: {self._shutdown_error}"
            ) from self._shutdown_error
        return not shutdown.is_alive()

    def _finish_shutdown(self) -> None:
        try:
            self._pool.shutdown(wait=True, cancel_futures=True)
        except Exception as exc:
            self._shutdown_error = exc
