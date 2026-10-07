from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


class TrayController:
    def __init__(
        self,
        post_ui: Callable[[Callable[[], None]], None],
        show: Callable[[], None],
        run_now: Callable[[], None],
        quit_app: Callable[[], None],
        toggle_monitoring: Callable[[], None] | None = None,
        restore_on_tray_loss: Callable[[], None] | None = None,
    ) -> None:
        self.post_ui = post_ui
        self.show_callback = show
        self.run_callback = run_now
        self.quit_callback = quit_app
        self.monitoring_callback = toggle_monitoring
        self.restore_callback = restore_on_tray_loss or show
        self._monitoring_paused = False
        self._title = "MailArchive - ready"
        self.icon: Any = None
        self._linux_tray: Any = None
        self.available = False
        self.safe_to_hide = False
        self._state = "ok"
        if os.name != "nt":
            self._start_linux_tray()
            return
        self._start_windows_tray()

    @property
    def available(self) -> bool:
        linux = getattr(self, "_linux_tray", None)
        return linux.available if linux is not None else self._available

    @available.setter
    def available(self, value: bool) -> None:
        self._available = value

    @property
    def safe_to_hide(self) -> bool:
        linux = getattr(self, "_linux_tray", None)
        return linux.available if linux is not None else self._safe_to_hide

    @safe_to_hide.setter
    def safe_to_hide(self, value: bool) -> None:
        self._safe_to_hide = value

    def _start_linux_tray(self) -> None:
        try:
            self._linux_tray = self._create_linux_tray()
            self.available = self._linux_tray.start()
            self.safe_to_hide = self.available
        except Exception:
            self._linux_tray = None
            self.available = False
            self.safe_to_hide = False

    def _create_linux_tray(self) -> Any:
        from mailarchive.presentation.linux_tray import LinuxTrayController

        return LinuxTrayController(
            self._image,
            self.post_ui,
            self.show_callback,
            self.run_callback,
            self.quit_callback,
            self.monitoring_callback,
            on_unavailable=self.restore_callback,
        )

    def _start_windows_tray(self) -> None:
        try:
            import pystray

            self.pystray = pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open MailArchive", self._show, default=True),
                pystray.MenuItem("Check mail now", self._run),
                pystray.MenuItem(
                    lambda item: (
                        "Resume automatic checks"
                        if self._monitoring_paused
                        else "Pause automatic checks"
                    ),
                    self._toggle_monitoring,
                    visible=self.monitoring_callback is not None,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )
            self.icon = pystray.Icon("MailArchive", self._image("ok"), "MailArchive - ready", menu)
            self.icon.run_detached()
            self.available = True
            self.safe_to_hide = True
        except Exception:
            self.icon = None
            self.available = False
            self.safe_to_hide = False

    @staticmethod
    def _image(state: str) -> Any:
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = {
            "ok": "#18794e",
            "busy": "#2563eb",
            "warning": "#b7791f",
            "error": "#c53030",
            "paused": "#697386",
        }[state]
        draw.rounded_rectangle((5, 10, 59, 52), radius=8, fill=color)
        draw.line((8, 14, 32, 34, 56, 14), fill="white", width=5)
        return image

    def _show(self, *_: Any) -> None:
        self.post_ui(self.show_callback)

    def _run(self, *_: Any) -> None:
        self.post_ui(self.run_callback)

    def _quit(self, *_: Any) -> None:
        self.post_ui(self.quit_callback)

    def _toggle_monitoring(self, *_: Any) -> None:
        if self.monitoring_callback is not None:
            self.post_ui(self.monitoring_callback)

    def set_monitoring_paused(self, paused: bool) -> None:
        self._monitoring_paused = paused
        if self._linux_tray is not None:
            self._linux_tray.set_monitoring_paused(paused)
        elif self.icon is not None:
            self.icon.update_menu()
        self.set_state(self._state, self._title)

    def set_state(self, state: str, title: str) -> None:
        self._state = state
        self._title = title
        if self._monitoring_paused:
            if state == "ok":
                state = "paused"
                title = "MailArchive - automatic checks paused"
            else:
                title += " - automatic checks paused"
        if self._linux_tray is not None:
            self._linux_tray.set_state(state, title)
            return
        if not self.available or not self.icon:
            return
        self.icon.icon = self._image(state)
        self.icon.title = title

    def notify(self, message: str) -> None:
        if not self.available:
            return
        if self._linux_tray is not None:
            self._linux_tray.notify(message)
            return
        if self.icon:
            try:
                self.icon.notify(message, "MailArchive - problem detected")
            except Exception:
                pass

    def stop(self) -> None:
        if self._linux_tray is not None:
            self._linux_tray.stop()
        elif self.available and self.icon:
            self.icon.stop()
