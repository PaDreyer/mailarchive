from __future__ import annotations

import ctypes
import logging
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from mailarchive import APP_NAME
from mailarchive.infrastructure.desktop_entry import autostart_entry, is_managed_entry
from mailarchive.infrastructure.linux_integration import IntegrationPaths, managed_appimage

logger = logging.getLogger(__name__)


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
        self._activation_path: Path | None = None
        self._activation_socket: socket.socket | None = None
        self._activation_identity: tuple[int, int] | None = None
        self._activation_thread: threading.Thread | None = None
        self._activation_lock = threading.Lock()
        self._activation_handler: Callable[[], None] | None = None
        self._activation_pending = False
        self._activation_closed = threading.Event()
        self.already_running = False
        if os.name != "nt":
            import fcntl

            configured_runtime = os.environ.get("XDG_RUNTIME_DIR")
            if configured_runtime:
                runtime_dir = Path(configured_runtime)
            else:
                runtime_dir = Path(tempfile.gettempdir()) / f"mailarchive-{os.getuid()}"
                runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = runtime_dir.lstat()
            if (
                not runtime_dir.is_absolute()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise RuntimeError("Unsafe runtime directory for the single-instance lock.")
            lock_path = runtime_dir / f"{name}-{os.getuid()}.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                os.close(descriptor)
                raise RuntimeError("Unsafe single-instance lock file.")
            self._lock_file = os.fdopen(descriptor, "a+")
            self._activation_path = runtime_dir / f"{name}-{os.getuid()}.sock"
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.already_running = True
            if not self.already_running:
                self._start_activation_listener()
            return
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_bool
        self.handle = self._kernel32.CreateMutexW(None, False, f"Local\\{name}")
        self.already_running = ctypes.get_last_error() == 183

    def activate(self) -> bool:
        """Ask the owning process to show its UI; no desktop-session bus is used."""
        if os.name == "nt":
            activate_existing_window()
            return True
        if self._activation_path is None or self._activation_closed.is_set():
            return False
        deadline = time.monotonic() + 0.5
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(0.1)
            while True:
                try:
                    client.sendto(b"activate", str(self._activation_path))
                    return True
                except OSError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.01)

    def set_activation_handler(self, handler: Callable[[], None]) -> None:
        """Attach a queue-only callback after the desktop has finished starting."""
        with self._activation_lock:
            if self._activation_closed.is_set():
                return
            self._activation_handler = handler
            if self._activation_pending:
                self._activation_pending = False
                handler()

    def _start_activation_listener(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            if self._activation_path.exists() or self._activation_path.is_symlink():
                info = self._activation_path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError("The activation path is not an owned socket.")
                self._activation_path.unlink()
            listener.bind(str(self._activation_path))
            info = self._activation_path.lstat()
            self._activation_identity = info.st_dev, info.st_ino
            self._activation_path.chmod(0o600)
            listener.settimeout(0.1)
            self._activation_socket = listener
            self._activation_thread = threading.Thread(
                target=self._listen_for_activation, name="MailArchive-Activation", daemon=True
            )
            self._activation_thread.start()
        except (OSError, RuntimeError):
            listener.close()
            logger.warning("Could not start the local activation listener.", exc_info=True)
            self._close_activation_listener()

    def _listen_for_activation(self) -> None:
        while not self._activation_closed.is_set():
            try:
                message = self._activation_socket.recv(16)
            except TimeoutError:
                continue
            except OSError:
                return
            if message != b"activate":
                continue
            with self._activation_lock:
                if self._activation_closed.is_set():
                    return
                if self._activation_handler is None:
                    self._activation_pending = True
                else:
                    self._activation_handler()

    def _close_activation_listener(self) -> None:
        with self._activation_lock:
            self._activation_closed.set()
            self._activation_handler = None
            self._activation_pending = False
        if self._activation_socket is not None:
            self._activation_socket.close()
        if self._activation_thread is not None and self._activation_thread.ident is not None:
            self._activation_thread.join(timeout=1)
        if self._activation_identity is not None:
            try:
                info = self._activation_path.lstat()
                if (info.st_dev, info.st_ino) == self._activation_identity:
                    self._activation_path.unlink()
            except FileNotFoundError:
                pass
            self._activation_identity = None

    def close(self) -> None:
        try:
            self._close_activation_listener()
        finally:
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None
            if self.handle and self._kernel32:
                self._kernel32.CloseHandle(self.handle)
                self.handle = None
