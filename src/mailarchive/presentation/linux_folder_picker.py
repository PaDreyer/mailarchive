"""A cancellable desktop-portal folder request, independent of Tk's UI thread."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from queue import Queue
from urllib.parse import unquote_to_bytes, urlsplit
from uuid import uuid4

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import MessageType
from dbus_next.message import Message

_PORTAL = "org.freedesktop.portal.Desktop"
_DESKTOP_PATH = "/org/freedesktop/portal/desktop"
_FILE_CHOOSER = "org.freedesktop.portal.FileChooser"
_REQUEST = "org.freedesktop.portal.Request"
_STARTUP_TIMEOUT = 3.0


class PortalUnavailable(RuntimeError):
    """The native chooser cannot complete this request; use the app chooser."""


def _selected_directory(results: dict[str, Variant]) -> Path:
    uris = results.get("uris")
    if uris is None or uris.signature != "as" or len(uris.value) != 1:
        raise PortalUnavailable("The folder chooser returned no single local directory.")
    uri = urlsplit(uris.value[0])
    if uri.scheme != "file" or uri.netloc not in {"", "localhost"} or uri.query or uri.fragment:
        raise PortalUnavailable("The folder chooser returned a non-local directory.")
    path = Path(os.fsdecode(unquote_to_bytes(uri.path)))
    if not path.is_absolute() or not path.is_dir():
        raise PortalUnavailable("The selected directory is no longer available.")
    return path


class PortalFolderRequest:
    def __init__(self, parent_window: str, initial_directory: Path) -> None:
        self.results: Queue[Path | None | Exception] = Queue()
        self._parent_window = parent_window
        self._initial_directory = initial_directory
        self._cancelled = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._bus: MessageBus | None = None
        self._handle: str | None = None
        self._owner: str | None = None
        self._response: asyncio.Future | None = None
        self._early_responses: dict[str, list] = {}
        self._opened = False
        self._responded = False
        self._thread = threading.Thread(
            target=self._run, name="MailArchive-FolderChooser", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker already closed its loop.

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._task = loop.create_task(self._choose())
        if self._cancelled.is_set():
            self._task.cancel()
        try:
            result = loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            result = None
        except Exception as exc:
            result = PortalUnavailable(str(exc))
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None
            self._task = None
            self._bus = None
        self.results.put(result)

    async def _call(self, message: Message) -> Message:
        reply = await self._bus.call(message)
        if reply is None or reply.message_type == MessageType.ERROR:
            raise PortalUnavailable("The desktop folder chooser is unavailable.")
        return reply

    async def _open(self) -> None:
        self._bus = MessageBus()
        await self._bus.connect()
        reply = await self._call(
            Message(
                destination=_PORTAL,
                path=_DESKTOP_PATH,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=[_FILE_CHOOSER, "version"],
            )
        )
        if reply.body[0].value < 3:
            raise PortalUnavailable("The desktop portal does not support folder selection.")
        self._owner = reply.sender
        token = f"mailarchive_{uuid4().hex}"
        sender = self._bus.unique_name[1:].replace(".", "_")
        self._handle = f"{_DESKTOP_PATH}/request/{sender}/{token}"
        self._response = asyncio.get_running_loop().create_future()
        self._bus.add_message_handler(self._receive_response)
        # Subscribe before OpenFile: a fast backend may respond before its method reply.
        for match in (
            f"type='signal',sender='{_PORTAL}',interface='{_REQUEST}',"
            f"member='Response',path_namespace='{_DESKTOP_PATH}/request'",
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            f"member='NameOwnerChanged',arg0='{_PORTAL}'",
        ):
            await self._call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[match],
                )
            )
        reply = await self._call(
            Message(
                destination=_PORTAL,
                path=_DESKTOP_PATH,
                interface=_FILE_CHOOSER,
                member="OpenFile",
                signature="ssa{sv}",
                body=[
                    self._parent_window,
                    "Choose folder",
                    {
                        "handle_token": Variant("s", token),
                        "directory": Variant("b", True),
                        "multiple": Variant("b", False),
                        "modal": Variant("b", True),
                        "accept_label": Variant("s", "Choose folder"),
                        "current_folder": Variant(
                            "ay", os.fsencode(self._initial_directory) + b"\0"
                        ),
                    },
                ],
            )
        )
        self._handle = reply.body[0]
        if reply.sender != self._owner:
            raise PortalUnavailable("The desktop portal restarted.")
        self._opened = True
        early = self._early_responses.pop(self._handle, None)
        if early is not None and not self._response.done():
            self._response.set_result(early)
        self._early_responses.clear()

    def _receive_response(self, message: Message) -> None:
        if (
            message.message_type == MessageType.SIGNAL
            and message.sender == "org.freedesktop.DBus"
            and message.interface == "org.freedesktop.DBus"
            and message.member == "NameOwnerChanged"
            and message.signature == "sss"
            and message.body[0] == _PORTAL
            and message.body[1] == self._owner
            and message.body[2] != self._owner
        ):
            if not self._response.done():
                self._response.set_exception(PortalUnavailable("The desktop portal stopped."))
            return
        if (
            message.message_type != MessageType.SIGNAL
            or message.sender != self._owner
            or message.interface != _REQUEST
            or message.member != "Response"
            or message.signature != "ua{sv}"
        ):
            return
        if not self._opened:
            self._early_responses[message.path] = message.body
        elif message.path == self._handle and not self._response.done():
            self._response.set_result(message.body)

    async def _choose(self) -> Path | None:
        disconnected = None
        try:
            opening = asyncio.create_task(self._open())
            try:
                # wait_for can swallow external cancellation on Python 3.10.
                done, _ = await asyncio.wait((opening,), timeout=_STARTUP_TIMEOUT)
                if not done:
                    raise asyncio.TimeoutError
                opening.result()
            finally:
                if not opening.done():
                    opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            disconnected = asyncio.create_task(self._bus.wait_for_disconnect())
            done, _ = await asyncio.wait(
                (self._response, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if self._response not in done:
                raise PortalUnavailable("The desktop portal disconnected.")
            response, results = self._response.result()
            self._responded = True
            if response == 1:
                return None
            if response != 0:
                raise PortalUnavailable("The desktop folder chooser failed.")
            return _selected_directory(results)
        finally:
            await self._cleanup(disconnected)

    async def _cleanup(self, disconnected: asyncio.Task | None) -> None:
        if self._bus is None:
            return
        if self._handle and not self._responded:
            try:
                await asyncio.wait_for(
                    self._bus.call(
                        Message(
                            destination=_PORTAL,
                            path=self._handle,
                            interface=_REQUEST,
                            member="Close",
                        )
                    ),
                    timeout=_STARTUP_TIMEOUT,
                )
            except Exception:
                pass  # Unavailable backends may have already removed the request.
        self._bus.remove_message_handler(self._receive_response)
        if self._response is not None and not self._response.cancelled():
            if self._response.done():
                self._response.exception()
            else:
                self._response.cancel()
        self._bus.disconnect()
        if disconnected is None:
            disconnected = asyncio.create_task(self._bus.wait_for_disconnect())
        try:
            await asyncio.wait_for(disconnected, timeout=_STARTUP_TIMEOUT)
        except Exception:
            pass
