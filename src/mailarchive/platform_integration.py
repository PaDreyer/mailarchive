from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path

from mailarchive import APP_NAME
from mailarchive.desktop_entry import autostart_entry, is_managed_entry
from mailarchive.linux_integration import IntegrationPaths, managed_appimage


def activate_existing_window() -> None:
    if os.name != "nt":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    window = user32.FindWindowW(None, APP_NAME)
    if window:
        user32.ShowWindow(window, 9)
        user32.SetForegroundWindow(window)


def application_command() -> list[str]:
    if os.name != "nt" and os.environ.get("APPIMAGE"):
        installed = managed_appimage()
        return [str(installed) if installed else os.environ["APPIMAGE"], "--minimized"]
    executable = str(sys.executable)
    if getattr(sys, "frozen", False):
        return [executable, "--minimized"]
    return [executable, "-m", "mailarchive", "--minimized"]


def _windows_command_line(arguments: list[str]) -> str:
    return " ".join(f'"{argument}"' if " " in argument else argument for argument in arguments)


def linux_autostart_path() -> Path:
    return IntegrationPaths.defaults().autostart


def _set_linux_autostart(enabled: bool, path: Path, arguments: list[str]) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace a symlink at the autostart path: {path}")
    if path.exists() and not is_managed_entry(
        path.read_text(encoding="utf-8"), legacy_autostart=True
    ):
        raise RuntimeError(f"The existing autostart file is not managed by MailArchive: {path}")
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    content = autostart_entry(arguments)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="mailarchive-", suffix=".desktop", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def set_start_at_login(enabled: bool) -> None:
    if os.name != "nt":
        _set_linux_autostart(enabled, linux_autostart_path(), application_command())
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(
                key, APP_NAME, 0, winreg.REG_SZ, _windows_command_line(application_command())
            )
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


class SingleInstance:
    def __init__(self, name: str = "MailArchive-7BB03E55") -> None:
        self.handle: int | None = None
        self._lock_file = None
        self._kernel32 = None
        self.already_running = False
        if os.name != "nt":
            import fcntl

            configured_runtime = os.environ.get("XDG_RUNTIME_DIR")
            if configured_runtime:
                runtime_dir = Path(configured_runtime)
            else:
                runtime_dir = Path(tempfile.gettempdir()) / f"mailarchive-{os.getuid()}"
                runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                if runtime_dir.stat().st_uid != os.getuid():
                    raise RuntimeError("Unsafe runtime directory for the single-instance lock.")
                runtime_dir.chmod(0o700)
            lock_path = runtime_dir / f"{name}-{os.getuid()}.lock"
            self._lock_file = lock_path.open("a+")
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.already_running = True
            return
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_bool
        self.handle = self._kernel32.CreateMutexW(None, False, f"Local\\{name}")
        self.already_running = ctypes.get_last_error() == 183

    def close(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
        if self.handle and self._kernel32:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None
