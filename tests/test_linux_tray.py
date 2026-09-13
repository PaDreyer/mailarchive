from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, call, patch

from PIL import Image

from mailarchive.linux_tray import LinuxTrayController, StatusNotifierItem, StatusNotifierMenu


class StatusNotifierItemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.post_ui = MagicMock()
        self.show = MagicMock()
        self.run_now = MagicMock()
        self.quit_app = MagicMock()
        self.image = MagicMock(return_value=Image.new("RGBA", (2, 1), (1, 2, 3, 4)))
        self.item = StatusNotifierItem(
            self.image, self.post_ui, self.show, self.run_now, self.quit_app
        )

    def test_icon_pixels_use_status_notifier_argb_order(self) -> None:
        self.assertEqual(self.item._icon_pixmap, [[2, 1, bytes((4, 1, 2, 3, 4, 1, 2, 3))]])
        self.assertEqual(self.item.IconAccessibleDesc, "MailArchive - ready")
        self.assertEqual(self.item.AttentionAccessibleDesc, "")

    def test_activation_callbacks_are_marshaled_to_the_ui_thread(self) -> None:
        self.item.Activate(0, 0)
        self.item.ContextMenu(0, 0)
        self.item.SecondaryActivate(0, 0)
        self.item.XAyatanaSecondaryActivate(0)

        self.assertEqual(
            self.post_ui.call_args_list,
            [call(self.show), call(self.show), call(self.run_now), call(self.run_now)],
        )

    def test_update_announces_title_icon_and_attention_status(self) -> None:
        with (
            patch.object(self.item, "emit_properties_changed") as properties_changed,
            patch.object(self.item, "NewTitle") as new_title,
            patch.object(self.item, "NewIcon") as new_icon,
            patch.object(self.item, "NewAttentionIcon") as new_attention_icon,
            patch.object(self.item, "NewStatus") as new_status,
        ):
            self.item.update("error", "MailArchive - problem detected")

        properties = properties_changed.call_args.args[0]
        self.assertEqual(properties["Title"], "MailArchive - problem detected")
        self.assertEqual(properties["Status"], "NeedsAttention")
        self.assertEqual(properties["IconPixmap"], [[2, 1, bytes((4, 1, 2, 3, 4, 1, 2, 3))]])
        self.assertEqual(
            properties["AttentionIconPixmap"], [[2, 1, bytes((4, 1, 2, 3, 4, 1, 2, 3))]]
        )
        self.assertEqual(properties["IconAccessibleDesc"], "MailArchive - problem detected")
        self.assertEqual(properties["AttentionAccessibleDesc"], "MailArchive - problem detected")
        new_title.assert_called_once_with()
        new_icon.assert_called_once_with()
        new_attention_icon.assert_called_once_with()
        new_status.assert_called_once_with("NeedsAttention")


class StatusNotifierMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.post_ui = MagicMock()
        self.show = MagicMock()
        self.run_now = MagicMock()
        self.quit_app = MagicMock()
        self.menu = StatusNotifierMenu(self.post_ui, self.show, self.run_now, self.quit_app)

    def test_root_layout_has_open_run_separator_and_quit(self) -> None:
        revision, root = self.menu.GetLayout.__wrapped__(self.menu, 0, -1, [])

        self.assertEqual(revision, 1)
        self.assertEqual(root[0], 0)
        self.assertEqual([child.value[0] for child in root[2]], [1, 2, 3, 4])
        self.assertEqual(root[2][2].value[1]["type"].value, "separator")

    def test_clicked_menu_item_is_marshaled_to_the_ui_thread(self) -> None:
        self.menu.Event(1, "clicked", None, 0)
        self.menu.Event(2, "clicked", None, 0)
        self.menu.Event(4, "clicked", None, 0)
        self.menu.Event(3, "clicked", None, 0)

        self.assertEqual(
            self.post_ui.call_args_list,
            [call(self.show), call(self.run_now), call(self.quit_app)],
        )


class LinuxTrayControllerTests(unittest.TestCase):
    def test_state_updates_are_scheduled_on_the_dbus_thread(self) -> None:
        controller = LinuxTrayController(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        controller._loop = MagicMock()
        controller._item = MagicMock()
        controller._available = True

        controller.set_state("warning", "MailArchive - attention")

        controller._loop.call_soon_threadsafe.assert_called_once_with(
            controller._item.update, "warning", "MailArchive - attention"
        )

    def test_notification_is_scheduled_on_the_dbus_thread(self) -> None:
        controller = LinuxTrayController(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()
        )
        controller._loop = MagicMock()
        controller._bus = MagicMock()
        controller._available = True

        controller.notify("Mailbox unavailable")

        controller._loop.call_soon_threadsafe.assert_called_once_with(
            controller._schedule_notification, "Mailbox unavailable"
        )

    @unittest.skipUnless(shutil.which("dbus-run-session"), "dbus-run-session is not installed")
    def test_registers_with_a_status_notifier_host_over_dbus(self) -> None:
        script = textwrap.dedent(
            """
            import asyncio

            from dbus_next.aio import MessageBus
            from dbus_next.constants import BusType, MessageType
            from dbus_next.message import Message
            from dbus_next.service import ServiceInterface, method
            from PIL import Image

            from mailarchive.linux_tray import (
                STATUS_NOTIFIER_ITEM,
                STATUS_NOTIFIER_ITEM_PATH,
                STATUS_NOTIFIER_MENU_PATH,
                STATUS_NOTIFIER_WATCHER,
                STATUS_NOTIFIER_WATCHER_PATH,
                LinuxTrayController,
            )


            class Watcher(ServiceInterface):
                def __init__(self):
                    super().__init__(STATUS_NOTIFIER_WATCHER)
                    self.items = []

                @method()
                def RegisterStatusNotifierItem(self, item: "s") -> "":
                    self.items.append(item)


            async def main():
                watcher_bus = await MessageBus(bus_type=BusType.SESSION).connect()
                await watcher_bus.request_name(STATUS_NOTIFIER_WATCHER)
                watcher = Watcher()
                watcher_bus.export(STATUS_NOTIFIER_WATCHER_PATH, watcher)
                controller = LinuxTrayController(
                    lambda state: Image.new("RGBA", (2, 1), (1, 2, 3, 4)),
                    lambda callback: callback(),
                    lambda: None,
                    lambda: None,
                    lambda: None,
                )
                client = None
                try:
                    assert await asyncio.to_thread(controller.start)
                    assert len(watcher.items) == 1
                    service_name = watcher.items[0]
                    client = await MessageBus(bus_type=BusType.SESSION).connect()
                    properties = await client.call(
                        Message(
                            destination=service_name,
                            path=STATUS_NOTIFIER_ITEM_PATH,
                            interface="org.freedesktop.DBus.Properties",
                            member="GetAll",
                            signature="s",
                            body=[STATUS_NOTIFIER_ITEM],
                        )
                    )
                    assert properties.message_type != MessageType.ERROR
                    assert properties.body[0]["Id"].value == "MailArchive"
                    assert properties.body[0]["Menu"].value == STATUS_NOTIFIER_MENU_PATH
                    assert properties.body[0]["IconAccessibleDesc"].value == "MailArchive - ready"
                    accessibility = await client.call(
                        Message(
                            destination=service_name,
                            path=STATUS_NOTIFIER_ITEM_PATH,
                            interface="org.freedesktop.DBus.Properties",
                            member="Get",
                            signature="ss",
                            body=[STATUS_NOTIFIER_ITEM, "IconAccessibleDesc"],
                        )
                    )
                    assert accessibility.message_type != MessageType.ERROR
                    assert accessibility.body[0].value == "MailArchive - ready"
                    activation_token = await client.call(
                        Message(
                            destination=service_name,
                            path=STATUS_NOTIFIER_ITEM_PATH,
                            interface=STATUS_NOTIFIER_ITEM,
                            member="ProvideXdgActivationToken",
                            signature="s",
                            body=["test-token"],
                        )
                    )
                    assert activation_token.message_type == MessageType.ERROR
                    layout = await client.call(
                        Message(
                            destination=service_name,
                            path=STATUS_NOTIFIER_MENU_PATH,
                            interface="com.canonical.dbusmenu",
                            member="GetLayout",
                            signature="iias",
                            body=[0, -1, []],
                        )
                    )
                    assert layout.message_type != MessageType.ERROR
                finally:
                    await asyncio.to_thread(controller.stop)
                    if client is not None:
                        client.disconnect()
                    watcher_bus.disconnect()


            asyncio.run(main())
            """
        )
        completed = subprocess.run(
            ["dbus-run-session", "--", sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode == 127 and "Failed to bind socket" in completed.stderr:
            self.skipTest("the test sandbox does not permit a private D-Bus socket")
        self.assertEqual(completed.returncode, 0, completed.stderr)
