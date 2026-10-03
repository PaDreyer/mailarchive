from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if sys.platform != "linux":
    raise unittest.SkipTest("Linux folder chooser requires Linux-only dependencies")

from dbus_next import Variant
from dbus_next.constants import MessageType
from dbus_next.message import Message

from mailarchive.presentation.linux_folder_picker import (
    PortalFolderRequest,
    PortalUnavailable,
    _selected_directory,
)


class FakeBus:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.unique_name = ":1.17"
        self.messages = []
        self.handlers = []
        self.disconnected = asyncio.get_running_loop().create_future()
        self.opened = asyncio.Event()
        self.response = 0
        self.version = 3
        self.stall = False
        self.drop_connection = False
        self.portal_stopped = False
        self.different_handle = False
        self.foreign_sender = False

    async def connect(self):
        return self

    async def call(self, message):
        self.messages.append(message)
        body = []
        if message.member == "Get":
            body = [Variant("u", self.version)]
        elif message.member == "OpenFile":
            options = message.body[2]
            sender = "legacy" if self.different_handle else "1_17"
            handle = (
                f"/org/freedesktop/portal/desktop/request/{sender}/{options['handle_token'].value}"
            )
            self.opened.set()
            if self.stall:
                await asyncio.Future()
            elif self.drop_connection:
                self.disconnect()
            elif self.portal_stopped:
                signal = Message(
                    path="/org/freedesktop/DBus",
                    sender="org.freedesktop.DBus",
                    interface="org.freedesktop.DBus",
                    member="NameOwnerChanged",
                    signature="sss",
                    message_type=MessageType.SIGNAL,
                    body=["org.freedesktop.portal.Desktop", ":1.9", ""],
                )
                for handler in self.handlers:
                    handler(signal)
            else:
                signal = Message(
                    path=handle,
                    sender=":1.99" if self.foreign_sender else ":1.9",
                    interface="org.freedesktop.portal.Request",
                    member="Response",
                    signature="ua{sv}",
                    message_type=MessageType.SIGNAL,
                    body=[self.response, {"uris": Variant("as", [self.uri])}],
                )
                for handler in self.handlers:
                    handler(signal)
            body = [handle]
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, sender=":1.9", body=body)

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)

    def disconnect(self):
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    async def wait_for_disconnect(self):
        await self.disconnected


class LinuxFolderRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    async def test_success_catches_response_before_method_reply_and_uses_folder_options(
        self,
    ) -> None:
        for different_handle in (False, True):
            with self.subTest(different_handle=different_handle):
                bus = FakeBus(self.folder.as_uri())
                bus.different_handle = different_handle
                request = PortalFolderRequest("x11:1234", self.folder)
                with patch(
                    "mailarchive.presentation.linux_folder_picker.MessageBus", return_value=bus
                ):
                    self.assertEqual(await request._choose(), self.folder)
                call = next(message for message in bus.messages if message.member == "OpenFile")
                self.assertEqual(call.body[:2], ["x11:1234", "Choose folder"])
                options = call.body[2]
                self.assertTrue(options["directory"].value)
                self.assertTrue(options["modal"].value)
                self.assertFalse(options["multiple"].value)
                self.assertEqual(options["current_folder"].value, bytes(self.folder) + b"\0")
                self.assertEqual(
                    [message.member for message in bus.messages],
                    ["Get", "AddMatch", "AddMatch", "OpenFile"],
                )
                self.assertTrue(bus.disconnected.done())
                self.assertFalse(bus.handlers)

    async def test_user_cancel_is_not_a_backend_failure(self) -> None:
        bus = FakeBus(self.folder.as_uri())
        bus.response = 1
        with patch("mailarchive.presentation.linux_folder_picker.MessageBus", return_value=bus):
            self.assertIsNone(await PortalFolderRequest("", self.folder)._choose())
        self.assertFalse(any(message.member == "Close" for message in bus.messages))

    async def test_old_portal_error_and_connection_loss_are_fallback_results(self) -> None:
        for failure in ("old", "error", "disconnect", "portal_stopped"):
            with self.subTest(failure=failure):
                bus = FakeBus(self.folder.as_uri())
                if failure == "old":
                    bus.version = 2
                elif failure == "error":
                    bus.response = 2
                elif failure == "disconnect":
                    bus.drop_connection = True
                else:
                    bus.portal_stopped = True
                with (
                    patch(
                        "mailarchive.presentation.linux_folder_picker.MessageBus", return_value=bus
                    ),
                    self.assertRaises(PortalUnavailable),
                ):
                    await PortalFolderRequest("", self.folder)._choose()
                self.assertTrue(bus.disconnected.done())
                if failure in {"disconnect", "portal_stopped"}:
                    self.assertTrue(any(message.member == "Close" for message in bus.messages))

    async def test_startup_timeout_closes_request_before_disconnecting(self) -> None:
        bus = FakeBus(self.folder.as_uri())
        bus.stall = True
        with (
            patch("mailarchive.presentation.linux_folder_picker.MessageBus", return_value=bus),
            patch("mailarchive.presentation.linux_folder_picker._STARTUP_TIMEOUT", 0.02),
            self.assertRaises(asyncio.TimeoutError),
        ):
            await PortalFolderRequest("", self.folder)._choose()
        self.assertEqual(bus.messages[-1].member, "Close")
        self.assertTrue(bus.disconnected.done())

    async def test_parent_cancellation_closes_active_request(self) -> None:
        bus = FakeBus(self.folder.as_uri())
        bus.foreign_sender = True
        request = PortalFolderRequest("", self.folder)
        with patch("mailarchive.presentation.linux_folder_picker.MessageBus", return_value=bus):
            task = asyncio.create_task(request._choose())
            await bus.opened.wait()
            # A signal from an unrelated bus owner must not finish the selection.
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(bus.messages[-1].member, "Close")
        self.assertTrue(bus.disconnected.done())

    async def test_user_interaction_outlives_startup_timeout(self) -> None:
        bus = FakeBus(self.folder.as_uri())
        bus.foreign_sender = True
        request = PortalFolderRequest("", self.folder)
        with (
            patch("mailarchive.presentation.linux_folder_picker.MessageBus", return_value=bus),
            patch("mailarchive.presentation.linux_folder_picker._STARTUP_TIMEOUT", 0.01),
        ):
            task = asyncio.create_task(request._choose())
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            request._response.set_result([1, {}])
            self.assertIsNone(await task)


class LinuxFolderWorkerTests(unittest.TestCase):
    def test_worker_reports_unavailability_and_cancel_before_start(self) -> None:
        request = PortalFolderRequest("", Path.home())
        with patch(
            "mailarchive.presentation.linux_folder_picker.MessageBus",
            side_effect=OSError("No session bus"),
        ):
            request.start()
            result = request.results.get(timeout=3)
        self.assertIsInstance(result, PortalUnavailable)
        request._thread.join(timeout=1)
        self.assertFalse(request._thread.is_alive())
        cancelled = PortalFolderRequest("", Path.home())
        cancelled.cancel()
        cancelled.start()
        self.assertIsNone(cancelled.results.get(timeout=3))
        cancelled._thread.join(timeout=1)
        self.assertFalse(cancelled._thread.is_alive())

    def test_uri_decoding_requires_one_existing_local_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "Belege ä # % {literal}"
            directory.mkdir()
            self.assertEqual(
                _selected_directory({"uris": Variant("as", [directory.as_uri()])}), directory
            )
            for uris in (
                [],
                [directory.as_uri(), directory.as_uri()],
                ["https://example.com"],
                ["file://server/share"],
                ["file:relative"],
                [(Path(temporary) / "missing").as_uri()],
            ):
                with self.subTest(uris=uris), self.assertRaises(PortalUnavailable):
                    _selected_directory({"uris": Variant("as", uris)})
            with self.assertRaises(PortalUnavailable):
                _selected_directory({})

    @unittest.skipUnless(shutil.which("dbus-run-session"), "dbus-run-session unavailable")
    def test_real_dbus_roundtrip_and_cancellation(self) -> None:
        script = textwrap.dedent("""
            import asyncio
            import tempfile
            from pathlib import Path
            from dbus_next import Variant
            from dbus_next.aio import MessageBus
            from dbus_next.constants import PropertyAccess
            from dbus_next.service import ServiceInterface, dbus_property, method, signal
            from mailarchive.presentation.linux_folder_picker import PortalFolderRequest

            class Request(ServiceInterface):
                def __init__(self):
                    super().__init__('org.freedesktop.portal.Request')
                @signal()
                def Response(self, response, results) -> 'ua{sv}':
                    return [response, results]
                @method()
                def Close(self):
                    pass

            class Chooser(ServiceInterface):
                def __init__(self, bus, directory):
                    super().__init__('org.freedesktop.portal.FileChooser')
                    self.bus, self.directory = bus, directory
                    self.response = 0
                @dbus_property(access=PropertyAccess.READ)
                def version(self) -> 'u':
                    return 3
                @method()
                def OpenFile(self, parent: 's', title: 's', options: 'a{sv}') -> 'o':
                    assert options['directory'].value is True
                    handle = '/org/freedesktop/portal/desktop/request/mock/' + options['handle_token'].value
                    request = Request()
                    self.bus.export(handle, request)
                    asyncio.get_running_loop().call_soon(request.Response, self.response, {'uris': Variant('as', [self.directory.as_uri()])})
                    return handle

            async def main():
                with tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary)
                    bus = await MessageBus().connect()
                    chooser = Chooser(bus, directory)
                    bus.export('/org/freedesktop/portal/desktop', chooser)
                    await bus.request_name('org.freedesktop.portal.Desktop')
                    assert await PortalFolderRequest('', directory)._choose() == directory
                    chooser.response = 1
                    assert await PortalFolderRequest('', directory)._choose() is None
                    bus.disconnect()
                    await bus.wait_for_disconnect()
            asyncio.run(main())
        """)
        completed = subprocess.run(
            ["dbus-run-session", "--", sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
