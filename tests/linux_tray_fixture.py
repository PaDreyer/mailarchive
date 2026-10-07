"""Exercise tray recovery on a private D-Bus; never use the desktop session bus."""

# ruff: noqa: F722, F821

import asyncio
import queue
import threading
import time
from unittest.mock import patch

from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method, signal
from PIL import Image

from mailarchive.presentation.linux_tray import (
    STATUS_NOTIFIER_WATCHER,
    STATUS_NOTIFIER_WATCHER_PATH,
    LinuxTrayController,
)
from mailarchive.presentation.tray import TrayController
from tests.concurrency import THREAD_TIMEOUT


class Watcher(ServiceInterface):
    def __init__(self, host: bool) -> None:
        super().__init__(STATUS_NOTIFIER_WATCHER)
        self.host = host
        self.items: list[str] = []

    @method()
    def RegisterStatusNotifierItem(self, item: "s") -> "":
        self.items.append(item)

    @dbus_property(access=PropertyAccess.READ)
    def IsStatusNotifierHostRegistered(self) -> "b":
        return self.host

    @signal()
    def StatusNotifierHostRegistered(self) -> "":
        return None

    @signal()
    def StatusNotifierHostUnregistered(self) -> "":
        return None

    def set_host(self, host: bool) -> None:
        self.host = host
        if host:
            self.StatusNotifierHostRegistered()
        else:
            self.StatusNotifierHostUnregistered()


async def until(predicate, explanation: str) -> None:
    deadline = time.monotonic() + THREAD_TIMEOUT
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(explanation)
        await asyncio.sleep(0.01)


async def open_watcher(host: bool) -> tuple[MessageBus, Watcher]:
    bus = await MessageBus().connect()
    await bus.request_name(STATUS_NOTIFIER_WATCHER)
    watcher = Watcher(host)
    bus.export(STATUS_NOTIFIER_WATCHER_PATH, watcher)
    return bus, watcher


async def main() -> None:
    bus, watcher = await open_watcher(False)
    callbacks: queue.SimpleQueue = queue.SimpleQueue()
    owner_thread = threading.get_ident()
    restored_threads = []
    window = {"hidden": False}

    def restore() -> None:
        restored_threads.append(threading.get_ident())
        window["hidden"] = False

    controller = LinuxTrayController(
        lambda state: Image.new("RGBA", (2, 1)),
        callbacks.put,
        lambda: None,
        lambda: None,
        lambda: None,
        on_unavailable=restore,
    )
    with patch.object(TrayController, "_create_linux_tray", return_value=controller):
        tray = await asyncio.to_thread(
            TrayController, callbacks.put, lambda: None, lambda: None, lambda: None
        )
    try:
        assert watcher.items, "The controller did not register with the watcher"
        assert not tray.available and not tray.safe_to_hide
        assert not restored_threads, "An absent initial host must not force window activation"

        watcher.set_host(True)
        await until(lambda: tray.safe_to_hide, "A newly registered host was not discovered")
        window["hidden"] = True
        watcher.set_host(False)
        await until(
            lambda: not tray.safe_to_hide and not callbacks.empty(),
            "Host loss did not invalidate readiness and queue recovery",
        )
        assert window["hidden"] and not restored_threads
        callbacks.get_nowait()()
        assert not window["hidden"] and restored_threads == [owner_thread]

        watcher.set_host(True)
        await until(lambda: tray.available, "The returning host was not discovered")
        bus.disconnect()
        await bus.wait_for_disconnect()
        await until(
            lambda: not tray.available and not callbacks.empty(),
            "Watcher loss did not invalidate readiness and queue recovery",
        )
        window["hidden"] = True
        callbacks.get_nowait()()
        assert not window["hidden"]
        tray.set_state("error", "MailArchive - saved error state")
        tray.set_monitoring_paused(True)

        bus, watcher = await open_watcher(True)
        await until(lambda: tray.available and watcher.items, "The new watcher was not registered")
        assert controller._item.Title.endswith("automatic checks paused")
        assert controller._menu._monitoring_paused

        previous_bus = controller._bus
        controller._schedule(previous_bus.disconnect)
        await until(
            lambda: controller._bus is not previous_bus and tray.available,
            "The controller did not reconnect after a bus connection loss",
        )
        assert len(watcher.items) == 2
        while not callbacks.empty():
            callbacks.get_nowait()()
        assert all(identifier == owner_thread for identifier in restored_threads)

        # A recovery already queued before shutdown must never reopen the window afterward.
        controller._schedule(lambda: controller._set_available(False))
        await until(lambda: not callbacks.empty(), "No recovery callback was queued")
        await asyncio.to_thread(tray.stop)
        window["hidden"] = True
        callbacks.get_nowait()()
        assert window["hidden"]
        assert not controller._thread.is_alive() and not tray.available
    finally:
        await asyncio.to_thread(tray.stop)
        bus.disconnect()
        await bus.wait_for_disconnect()


if __name__ == "__main__":
    asyncio.run(main())
