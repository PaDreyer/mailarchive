from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from mailarchive.platform_integration import tray_backend_is_available


class TrayController:
    def __init__(
        self,
        post_ui: Callable[[Callable[[], None]], None],
        show: Callable[[], None],
        run_now: Callable[[], None],
        quit_app: Callable[[], None],
    ) -> None:
        self.post_ui = post_ui
        self.show_callback = show
        self.run_callback = run_now
        self.quit_callback = quit_app
        self.icon: Any = None
        self.available = False
        self.safe_to_hide = False
        self._state = "ok"
        try:
            import pystray

            self.pystray = pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open MailArchive", self._show, default=True),
                pystray.MenuItem("Archive now", self._run),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )
            self.icon = pystray.Icon("MailArchive", self._image("ok"), "MailArchive - ready", menu)
            if not tray_backend_is_available(self.icon):
                self.icon = None
                return
            backend = type(self.icon).__module__.casefold()
            if os.name == "nt":
                self.icon.run_detached()
            else:
                threading.Thread(
                    target=self.icon.run,
                    name="MailArchive-Tray",
                    daemon=True,
                ).start()
            self.available = True
            self.safe_to_hide = (
                os.name == "nt" or "appindicator" in backend or backend.endswith("._xorg")
            )
        except Exception:
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

    def set_state(self, state: str, title: str) -> None:
        if not self.available or not self.icon:
            return
        self._state = state
        self.icon.icon = self._image(state)
        self.icon.title = title

    def notify(self, message: str) -> None:
        if self.available and self.icon:
            try:
                self.icon.notify(message, "MailArchive - problem detected")
            except Exception:
                pass

    def stop(self) -> None:
        if self.available and self.icon:
            self.icon.stop()
