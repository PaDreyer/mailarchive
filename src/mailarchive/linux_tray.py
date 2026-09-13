# ruff: noqa: F722, F821

import asyncio
import os
import threading
from collections.abc import Callable
from typing import Any

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, MessageType, PropertyAccess
from dbus_next.message import Message
from dbus_next.service import ServiceInterface, dbus_property, method, signal

STATUS_NOTIFIER_ITEM = "org.kde.StatusNotifierItem"
STATUS_NOTIFIER_ITEM_PATH = "/StatusNotifierItem"
STATUS_NOTIFIER_MENU_PATH = "/Menu"
STATUS_NOTIFIER_WATCHER = "org.kde.StatusNotifierWatcher"
STATUS_NOTIFIER_WATCHER_PATH = "/StatusNotifierWatcher"
NOTIFICATIONS = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"


def _status_for(state: str) -> str:
    return "NeedsAttention" if state == "error" else "Active"


def _argb_pixels(image: Any) -> bytes:
    rgba = image.convert("RGBA")
    pixels = rgba.tobytes()
    argb = bytearray(len(pixels))
    argb[0::4] = pixels[3::4]
    argb[1::4] = pixels[0::4]
    argb[2::4] = pixels[1::4]
    argb[3::4] = pixels[2::4]
    return bytes(argb)


class StatusNotifierItem(ServiceInterface):
    def __init__(
        self,
        image_for_state: Callable[[str], Any],
        post_ui: Callable[[Callable[[], None]], None],
        show: Callable[[], None],
        run_now: Callable[[], None],
        quit_app: Callable[[], None],
    ) -> None:
        super().__init__(STATUS_NOTIFIER_ITEM)
        self._image_for_state = image_for_state
        self._post_ui = post_ui
        self._show = show
        self._run_now = run_now
        self._quit_app = quit_app
        self._state = "ok"
        self._title = "MailArchive - ready"
        self._icon_pixmap = self._make_pixmap(self._state)

    def _make_pixmap(self, state: str) -> list[list[Any]]:
        image = self._image_for_state(state)
        return [[image.width, image.height, _argb_pixels(image)]]

    def update(self, state: str, title: str) -> None:
        previous_status = _status_for(self._state)
        self._state = state
        self._title = title
        self._icon_pixmap = self._make_pixmap(state)
        current_status = _status_for(state)
        self.emit_properties_changed(
            {
                "Title": self._title,
                "IconPixmap": self._icon_pixmap,
                "AttentionIconPixmap": self.AttentionIconPixmap,
                "IconAccessibleDesc": self.IconAccessibleDesc,
                "AttentionAccessibleDesc": self.AttentionAccessibleDesc,
                "Status": current_status,
            }
        )
        self.NewTitle()
        self.NewIcon()
        self.NewAttentionIcon()
        if current_status != previous_status:
            self.NewStatus(current_status)

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "MailArchive"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return self._title

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return _status_for(self._state)

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return STATUS_NOTIFIER_MENU_PATH

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return self._icon_pixmap

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return self._icon_pixmap if self._state == "error" else []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconAccessibleDesc(self) -> "s":
        return self._title

    @dbus_property(access=PropertyAccess.READ)
    def AttentionAccessibleDesc(self) -> "s":
        return self._title if self._state == "error" else ""

    @method()
    def Activate(self, _x: "i", _y: "i") -> "":
        self._post_ui(self._show)

    @method()
    def ContextMenu(self, _x: "i", _y: "i") -> "":
        self._post_ui(self._show)

    @method()
    def SecondaryActivate(self, _x: "i", _y: "i") -> "":
        self._post_ui(self._run_now)

    @method()
    def XAyatanaSecondaryActivate(self, _timestamp: "u") -> "":
        self._post_ui(self._run_now)

    @method()
    def Scroll(self, _delta: "i", _orientation: "s") -> "":
        return None

    @signal()
    def NewIcon(self) -> "":
        return None

    @signal()
    def NewAttentionIcon(self) -> "":
        return None

    @signal()
    def NewOverlayIcon(self) -> "":
        return None

    @signal()
    def NewTitle(self) -> "":
        return None

    @signal()
    def NewStatus(self, status: "s") -> "s":
        return status


class StatusNotifierMenu(ServiceInterface):
    _item_actions = {
        1: "show",
        2: "run",
        4: "quit",
    }

    def __init__(
        self,
        post_ui: Callable[[Callable[[], None]], None],
        show: Callable[[], None],
        run_now: Callable[[], None],
        quit_app: Callable[[], None],
    ) -> None:
        super().__init__("com.canonical.dbusmenu")
        self._post_ui = post_ui
        self._callbacks = {
            "show": show,
            "run": run_now,
            "quit": quit_app,
        }

    @staticmethod
    def _properties(item_id: int) -> dict[str, Variant]:
        if item_id == 3:
            return {
                "type": Variant("s", "separator"),
                "visible": Variant("b", True),
            }
        labels = {
            1: "Open MailArchive",
            2: "Archive now",
            4: "Quit",
        }
        return {
            "label": Variant("s", labels[item_id]),
            "enabled": Variant("b", True),
            "visible": Variant("b", True),
        }

    @classmethod
    def _layout(cls, item_id: int, depth: int) -> list[Any]:
        if item_id != 0:
            return [item_id, cls._properties(item_id), []]
        children = []
        if depth != 0:
            children = [
                Variant("(ia{sv}av)", cls._layout(child_id, 0)) for child_id in (1, 2, 3, 4)
            ]
        return [0, {}, children]

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", _properties: "as") -> "u(ia{sv}av)":
        if parent_id not in (0, 1, 2, 3, 4):
            return [1, [parent_id, {}, []]]
        return [1, self._layout(parent_id, recursion_depth)]

    @method()
    def GetGroupProperties(self, item_ids: "ai", _properties: "as") -> "a(ia{sv})":
        return [
            [item_id, self._properties(item_id)]
            for item_id in item_ids
            if item_id in self._item_actions or item_id == 3
        ]

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        return self._properties(item_id).get(name, Variant("s", ""))

    @method()
    def Event(self, item_id: "i", event_id: "s", _data: "v", _timestamp: "u") -> "":
        action = self._item_actions.get(item_id)
        if action is not None and event_id == "clicked":
            self._post_ui(self._callbacks[action])

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for event in events:
            self.Event(*event)
        return []

    @method()
    def AboutToShow(self, _item_id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, _item_ids: "ai") -> "aiai":
        return [[], []]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []


class LinuxTrayController:
    def __init__(
        self,
        image_for_state: Callable[[str], Any],
        post_ui: Callable[[Callable[[], None]], None],
        show: Callable[[], None],
        run_now: Callable[[], None],
        quit_app: Callable[[], None],
    ) -> None:
        self._image_for_state = image_for_state
        self._post_ui = post_ui
        self._show = show
        self._run_now = run_now
        self._quit_app = quit_app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: MessageBus | None = None
        self._item: StatusNotifierItem | None = None
        self._ready = threading.Event()
        self._available = False
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> bool:
        self._thread = threading.Thread(
            target=self._run,
            name="MailArchive-StatusNotifier",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=2)
        return self._available

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._connect())
            if self._available:
                loop.run_forever()
        except Exception:
            self._available = False
        finally:
            self._ready.set()
            if self._bus is not None:
                self._bus.disconnect()
            loop.close()

    async def _connect(self) -> None:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        service_name = f"{STATUS_NOTIFIER_ITEM}.MailArchive_{os.getpid()}"
        await bus.request_name(service_name)
        item = StatusNotifierItem(
            self._image_for_state,
            self._post_ui,
            self._show,
            self._run_now,
            self._quit_app,
        )
        menu = StatusNotifierMenu(self._post_ui, self._show, self._run_now, self._quit_app)
        bus.export(STATUS_NOTIFIER_ITEM_PATH, item)
        bus.export(STATUS_NOTIFIER_MENU_PATH, menu)
        reply = await bus.call(
            Message(
                destination=STATUS_NOTIFIER_WATCHER,
                path=STATUS_NOTIFIER_WATCHER_PATH,
                interface=STATUS_NOTIFIER_WATCHER,
                member="RegisterStatusNotifierItem",
                signature="s",
                body=[service_name],
            )
        )
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else "No StatusNotifier host is available."
            bus.disconnect()
            raise RuntimeError(detail)
        self._bus = bus
        self._item = item
        self._available = True
        self._ready.set()

    def set_state(self, state: str, title: str) -> None:
        if self._loop is None or self._item is None or not self._available:
            return
        self._loop.call_soon_threadsafe(self._item.update, state, title)

    def notify(self, message: str) -> None:
        if self._loop is None or self._bus is None or not self._available:
            return
        self._loop.call_soon_threadsafe(self._schedule_notification, message)

    def _schedule_notification(self, message: str) -> None:
        asyncio.create_task(self._send_notification(message))

    async def _send_notification(self, message: str) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.call(
                Message(
                    destination=NOTIFICATIONS,
                    path=NOTIFICATIONS_PATH,
                    interface=NOTIFICATIONS,
                    member="Notify",
                    signature="susssasa{sv}i",
                    body=[
                        "MailArchive",
                        0,
                        "",
                        "MailArchive - problem detected",
                        message,
                        [],
                        {},
                        -1,
                    ],
                )
            )
        except Exception:
            pass

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
