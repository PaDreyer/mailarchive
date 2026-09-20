"""Native window identity, independent of how the application was launched."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

from mailarchive import APP_NAME, APP_WINDOW_CLASS


def create_root() -> tk.Tk:
    # The class must be set at creation time so remapping after tray use keeps
    # matching the desktop entry's StartupWMClass instead of falling back to Tk.
    root = tk.Tk(className=APP_WINDOW_CLASS)
    root.title(APP_NAME)
    root.iconname(APP_NAME)
    with Image.open(Path(__file__).with_name("assets") / "mailarchive.ico") as icon:
        # Tk takes a copy; -default also supplies the icon to future dialogs.
        root.iconphoto(True, ImageTk.PhotoImage(icon, master=root))
    return root
