"""Shared destination selection with native dialogs and a folder-creating fallback."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from queue import Empty
from string import Formatter
from tkinter import filedialog, messagebox, simpledialog, ttk

from mailarchive.domain.archive_paths import destination_path


def _initial_directory(destination: str) -> Path:
    try:
        destination_path(destination)
        literals = []
        for literal, field, _spec, _conversion in Formatter().parse(destination):
            literals.append(literal)
            if field is not None:
                break
        path = Path("".join(literals))
        for candidate in (path, *path.parents):
            if candidate.is_absolute() and candidate.is_dir():
                return candidate
    except (ValueError, OSError):
        pass
    return Path.home()


def _center(dialog: tk.Toplevel, parent: tk.Misc) -> None:
    parent.update_idletasks()
    dialog.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_reqheight()) // 2
    dialog.geometry(f"+{x}+{y}")
    dialog.deiconify()
    dialog.grab_set()


def _exists(widget: tk.Misc | None) -> bool:
    try:
        return widget is not None and bool(widget.winfo_exists())
    except tk.TclError:
        return False


def choose_destination_folder(parent: tk.Misc, current_destination: str) -> str | None:
    """Return a concrete folder escaped for destination templates, or None on cancel."""
    previous_grab, previous_focus = parent.grab_current(), parent.focus_get()
    initial = _initial_directory(current_destination)
    try:
        if sys.platform == "linux":
            selected = _choose_linux_folder(parent, initial)
        else:
            selected = filedialog.askdirectory(
                parent=parent, initialdir=str(initial), title="Choose folder", mustexist=True
            )
        if not selected or not _exists(parent):
            return None
        return str(selected).replace("{", "{{").replace("}", "}}")
    finally:
        if _exists(previous_grab):
            previous_grab.grab_set()
        if _exists(previous_focus):
            previous_focus.focus_set()


def _choose_linux_folder(parent: tk.Misc, initial: Path) -> Path | None:
    try:
        wait = _PortalWaitDialog(parent, initial)
    except ImportError:
        result = RuntimeError("The desktop portal library is unavailable.")
    else:
        try:
            parent.wait_window(wait)
        except tk.TclError:
            return None
        result = wait.result
    if not _exists(parent):
        return None
    if isinstance(result, Exception):
        fallback = FolderPickerDialog(parent, initial)
        try:
            parent.wait_window(fallback)
        except tk.TclError:
            return None
        return fallback.result
    return result


class _PortalWaitDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, initial: Path) -> None:
        from mailarchive.presentation.linux_folder_picker import PortalFolderRequest

        super().__init__(parent)
        self.withdraw()
        self.title("Choose folder")
        self.transient(parent)
        self.resizable(False, False)
        self.result: Path | None | Exception = None
        self._timer: str | None = None
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Choose a folder in the system dialog.").pack(pady=(0, 12))
        self._progress = ttk.Progressbar(frame, mode="indeterminate")
        self._progress.pack(fill="x", pady=(0, 12))
        self._progress.start()
        ttk.Button(frame, text="Cancel", command=self.destroy).pack(anchor="e")
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        _center(self, parent)
        self.update_idletasks()
        identifier = ""
        if self.tk.call("tk", "windowingsystem") == "x11":
            identifier = f"x11:{int(str(self.tk.call('wm', 'frame', self)), 0):x}"
        self._request = PortalFolderRequest(identifier, initial)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._request.start()
        self._timer = self.after(25, self._poll)

    def destroy(self) -> None:
        if _exists(self):
            self._progress.stop()
        super().destroy()

    def _poll(self) -> None:
        self._timer = None
        try:
            self.result = self._request.results.get_nowait()
        except Empty:
            self._timer = self.after(25, self._poll)
            return
        self.destroy()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self._request.cancel()
            if self._timer is not None:
                self.after_cancel(self._timer)
                self._timer = None


class FolderPickerDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, initial: Path) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Choose folder")
        self.transient(parent)
        self.resizable(True, True)
        self.result: Path | None = None
        self._directory = initial
        self._folders: list[Path] = []
        self.path_var = tk.StringVar(master=self, value=str(initial))
        self.hidden_var = tk.BooleanVar(master=self, value=False)
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Directory").grid(row=0, column=0, sticky="w", pady=(0, 6))
        path_entry = ttk.Entry(frame, textvariable=self.path_var, width=60)
        path_entry.grid(row=1, column=0, sticky="ew")
        path_entry.bind("<Return>", lambda _event: self._go())
        ttk.Button(frame, text="Go", command=self._go).grid(row=1, column=1, padx=(6, 0))
        navigation = ttk.Frame(frame)
        navigation.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(navigation, text="Up", command=self._up).pack(side="left")
        ttk.Button(navigation, text="New folder", command=self._new_folder).pack(
            side="left", padx=6
        )
        ttk.Checkbutton(
            navigation,
            text="Show hidden folders",
            variable=self.hidden_var,
            command=self._refresh,
        ).pack(side="right")
        self.folder_list = tk.Listbox(frame, height=12, exportselection=False, activestyle="dotbox")
        self.folder_list.grid(row=3, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.folder_list.yview)
        scroll.grid(row=3, column=1, sticky="ns")
        self.folder_list.configure(yscrollcommand=scroll.set)
        self.folder_list.bind("<Double-Button-1>", lambda _event: self._open_selected())
        self.folder_list.bind("<Return>", lambda _event: self._open_selected())
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="Choose folder", command=self._choose).pack(side="left")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        self.bind("<Escape>", lambda _event: self.destroy())
        self._navigate(initial)
        _center(self, parent)
        path_entry.focus_set()

    def _navigate(self, path: Path) -> bool:
        try:
            if not path.is_dir():
                raise ValueError("Choose an existing directory.")
            folders = []
            for item in path.iterdir():
                try:
                    if item.is_dir() and (self.hidden_var.get() or not item.name.startswith(".")):
                        folders.append(item)
                except OSError:
                    continue  # An entry can disappear while the directory is being read.
            folders.sort(key=lambda item: item.name.casefold())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot open folder", str(exc), parent=self)
            self.path_var.set(str(self._directory))
            return False
        self._directory = path
        self.path_var.set(str(path))
        self._folders = folders
        self.folder_list.delete(0, "end")
        for item in folders:
            self.folder_list.insert("end", item.name)
        return True

    def _typed_path(self) -> Path:
        path = Path(self.path_var.get()).expanduser()
        if not path.is_absolute():
            path = self._directory / path
        return Path(os.path.abspath(path))

    def _go(self) -> None:
        try:
            self._navigate(self._typed_path())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot open folder", str(exc), parent=self)

    def _up(self) -> None:
        self._navigate(self._directory.parent)

    def _refresh(self) -> None:
        self._navigate(self._directory)

    def _selection(self) -> Path:
        selection = self.folder_list.curselection()
        return self._folders[selection[0]] if selection else self._directory

    def _open_selected(self) -> None:
        self._navigate(self._selection())

    def _new_folder(self) -> None:
        name = simpledialog.askstring("New folder", "Folder name:", parent=self)
        self.grab_set()
        if name is None:
            return
        try:
            if (
                not name
                or name != name.strip()
                or name in {".", ".."}
                or any(character in name for character in "/\\\0")
            ):
                raise ValueError(
                    "Enter a single folder name without separators or outer whitespace."
                )
            folder = self._directory / name
            folder.mkdir()
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot create folder", str(exc), parent=self)
            return
        self._navigate(folder)

    def _choose(self) -> None:
        try:
            path = (
                self._typed_path()
                if self.path_var.get() != str(self._directory)
                else self._selection()
            )
            if not path.is_dir():
                raise ValueError("Choose an existing directory.")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Cannot choose folder", str(exc), parent=self)
            return
        self.result = path
        self.destroy()
