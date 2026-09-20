"""User-scoped AppImage installation, independent of Tk and archive settings."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from mailarchive import APP_WINDOW_CLASS, __version__
from mailarchive.desktop_entry import (
    MANAGED_KEY,
    autostart_entry,
    desktop_exec,
    desktop_value,
    is_managed_entry,
)


class IntegrationError(RuntimeError):
    pass


def _xdg_home(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable, "")
    path = Path(value) if value else fallback
    return path if path.is_absolute() else fallback


@dataclass(frozen=True)
class IntegrationPaths:
    data_home: Path
    config_home: Path
    home: Path

    @classmethod
    def defaults(cls) -> IntegrationPaths:
        home = Path.home()
        return cls(
            _xdg_home("XDG_DATA_HOME", home / ".local" / "share"),
            _xdg_home("XDG_CONFIG_HOME", home / ".config"),
            home,
        )

    @property
    def application(self) -> Path:
        return self.data_home / "mailarchive" / "application" / "MailArchive.AppImage"

    @property
    def icon(self) -> Path:
        return self.application.parent / "mailarchive.svg"

    @property
    def receipt(self) -> Path:
        return self.data_home / "mailarchive" / "desktop-integration.json"

    @property
    def menu(self) -> Path:
        return self.data_home / "applications" / "mailarchive.desktop"

    @property
    def autostart(self) -> Path:
        return self.config_home / "autostart" / "mailarchive.desktop"

    def desktop_directory(self) -> Path | None:
        """Read XDG's user-directory configuration without evaluating shell code.

        A desktop pointing at HOME is explicitly disabled. Never create a new
        English 'Desktop' directory on a localized or headless installation.
        """
        directory = self.home / "Desktop"
        config = self.config_home / "user-dirs.dirs"
        if config.exists():
            for line in config.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.strip().partition("=")
                if separator and key == "XDG_DESKTOP_DIR":
                    try:
                        values = shlex.split(value, comments=True)
                    except ValueError:
                        return None
                    if len(values) != 1:
                        return None
                    value = values[0]
                    for prefix in ("$HOME", "${HOME}"):
                        if value == prefix or value.startswith(prefix + "/"):
                            value = str(self.home) + value[len(prefix) :]
                            break
                    directory = Path(value)
                    break
        if (
            not directory.is_absolute()
            or directory.resolve() == self.home.resolve()
            or not directory.is_dir()
        ):
            return None
        return directory


@dataclass(frozen=True)
class IntegrationOptions:
    menu_entry: bool = True
    desktop_shortcut: bool = False


@dataclass(frozen=True)
class IntegrationState:
    schema_version: int = 1
    prompt_seen: bool = False
    installed_version: str = ""
    menu_entry: bool = False
    desktop_path: str = ""

    @property
    def options(self) -> IntegrationOptions:
        return IntegrationOptions(self.menu_entry, bool(self.desktop_path))


@dataclass(frozen=True)
class IntegrationResult:
    state: IntegrationState
    warnings: tuple[str, ...] = ()


def _snapshot(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise IntegrationError(f"Refusing to change a symlink or non-regular file: {path}")
    return info


@dataclass
class _Change:
    target: Path
    staged: Path | None
    original: os.stat_result | None
    backup: Path | None = None
    committed: bool = False


class _FileTransaction:
    """Stage all files first; atomically replace each and roll back on failure.

    Backups are renamed, not copied, so even a large running AppImage can be
    replaced without truncating its inode or consuming a second backup copy.
    """

    def __init__(self) -> None:
        self.changes: list[_Change] = []
        self.succeeded = False

    def write(self, target: Path, content: bytes, *, mode: int = 0o644) -> None:
        staged = self._stage(target)
        with staged.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)

    def copy(self, source: Path, target: Path, *, mode: int) -> None:
        staged = self._stage(target)
        with source.open("rb") as original, staged.open("wb") as handle:
            before = os.fstat(original.fileno())
            shutil.copyfileobj(original, handle)
            if self._identity(before) != self._identity(os.fstat(original.fileno())):
                raise IntegrationError(f"Application source changed while copying: {source}")
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)

    def _stage(self, target: Path) -> Path:
        original = _snapshot(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".mailarchive-", dir=target.parent)
        os.close(descriptor)
        staged = Path(name)
        self.changes.append(_Change(target, staged, original))
        return staged

    def remove(self, target: Path) -> None:
        self.changes.append(_Change(target, None, _snapshot(target)))

    def commit(self) -> None:
        try:
            for change in self.changes:
                if self._identity(_snapshot(change.target)) != self._identity(change.original):
                    raise IntegrationError(f"File changed during desktop setup: {change.target}")
                if change.original is not None:
                    descriptor, name = tempfile.mkstemp(
                        prefix=".mailarchive-backup-", dir=change.target.parent
                    )
                    os.close(descriptor)
                    backup = Path(name)
                    try:
                        os.replace(change.target, backup)
                    except BaseException:
                        backup.unlink(missing_ok=True)
                        raise
                    change.backup = backup
                if change.staged is not None:
                    os.replace(change.staged, change.target)
                change.committed = True
            self.succeeded = True
        except BaseException:
            self._rollback()
            raise

    @staticmethod
    def _identity(info: os.stat_result | None) -> tuple[int, ...] | None:
        if info is None:
            return None
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
        )

    def _rollback(self) -> None:
        errors = []
        for change in reversed(self.changes):
            try:
                if change.backup is not None:
                    os.replace(change.backup, change.target)
                    change.backup = None
                elif change.committed:
                    change.target.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{change.target}: {exc}; backup: {change.backup}")
        if errors:
            raise IntegrationError("Could not fully undo desktop setup: " + "; ".join(errors))

    def close(self) -> None:
        for change in self.changes:
            if change.staged is not None:
                change.staged.unlink(missing_ok=True)
            # On a failed rollback, preserve backups for manual recovery.
            if self.succeeded and change.backup is not None:
                change.backup.unlink(missing_ok=True)

    def __enter__(self) -> _FileTransaction:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AppImageIntegration:
    def __init__(
        self, source: Path, icon_source: Path | None, paths: IntegrationPaths | None = None
    ) -> None:
        self.source = source
        self.icon_source = icon_source
        self.paths = paths or IntegrationPaths.defaults()

    @classmethod
    def for_current_process(cls) -> AppImageIntegration | None:
        if sys.platform != "linux" or not os.environ.get("APPIMAGE"):
            return None
        app_dir = os.environ.get("APPDIR", "")
        return cls(
            Path(os.environ["APPIMAGE"]),
            Path(app_dir) / "mailarchive.svg" if app_dir else None,
        )

    def load_state(self) -> IntegrationState:
        if _snapshot(self.paths.receipt) is None:
            return IntegrationState()
        try:
            payload = json.loads(self.paths.receipt.read_text(encoding="utf-8"))
            state = IntegrationState(**payload)
            if (
                type(state.schema_version) is not int
                or state.schema_version != 1
                or type(state.prompt_seen) is not bool
                or type(state.menu_entry) is not bool
                or not isinstance(state.installed_version, str)
                or not isinstance(state.desktop_path, str)
            ):
                raise ValueError("Unsupported desktop integration receipt.")
            if state.desktop_path:
                path = Path(state.desktop_path)
                if not path.is_absolute() or path.name != "MailArchive.desktop":
                    raise ValueError("Invalid desktop shortcut path in receipt.")
            return state
        except (TypeError, ValueError) as exc:
            raise IntegrationError(
                f"Could not read desktop integration receipt '{self.paths.receipt}': {exc}"
            ) from exc

    def mark_prompt_seen(self) -> None:
        state = replace(self.load_state(), prompt_seen=True)
        with _FileTransaction() as transaction:
            self._write_state(transaction, state)
            transaction.commit()

    def apply(self, options: IntegrationOptions, *, start_at_login: bool) -> IntegrationResult:
        state = self.load_state()
        desktop = self._desktop_target(options)
        launcher = self._launcher()
        self._check_launchers(state, options, desktop)
        install = options.menu_entry or options.desktop_shortcut
        if install:
            source, icon_source = self._validate_sources(state)
        with _FileTransaction() as transaction:
            if install:
                if source.resolve() != self.paths.application.resolve():
                    transaction.copy(source, self.paths.application, mode=0o755)
                transaction.copy(icon_source, self.paths.icon, mode=0o644)
            self._stage_launchers(transaction, state, options, desktop, launcher)
            installed_version = __version__ if install else state.installed_version
            if (
                installed_version
                and start_at_login
                and (install or self.paths.application.is_file())
            ):
                self._require_owned(self.paths.autostart)
                transaction.write(
                    self.paths.autostart,
                    autostart_entry([str(self.paths.application), "--minimized"]).encode(),
                )
            updated = IntegrationState(
                prompt_seen=True,
                installed_version=installed_version,
                menu_entry=options.menu_entry,
                desktop_path=str(desktop) if desktop else "",
            )
            self._write_state(transaction, updated)
            transaction.commit()
        warnings = self._trust_desktop(desktop) if desktop else ()
        return IntegrationResult(updated, warnings)

    def _desktop_target(self, options: IntegrationOptions) -> Path | None:
        if not options.desktop_shortcut:
            return None
        directory = self.paths.desktop_directory()
        if directory is None:
            raise IntegrationError("No usable desktop folder is configured for this user.")
        return directory / "MailArchive.desktop"

    def _launcher(self) -> bytes:
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=MailArchive\n"
            "Comment=Archive emails and attachments locally\n"
            f"Exec={desktop_exec([str(self.paths.application)])}\n"
            f"Icon={desktop_value(str(self.paths.icon))}\n"
            "Terminal=false\n"
            "Categories=Office;Utility;\n"
            "StartupNotify=false\n"
            f"StartupWMClass={APP_WINDOW_CLASS}\n"
            f"{MANAGED_KEY}\n"
        ).encode()

    def _require_owned(self, path: Path) -> None:
        if _snapshot(path) is not None:
            if not is_managed_entry(path.read_text(encoding="utf-8")):
                raise IntegrationError(f"An existing file is not managed by MailArchive: {path}")

    def _check_launchers(
        self, state: IntegrationState, options: IntegrationOptions, desktop: Path | None
    ) -> None:
        if state.menu_entry or options.menu_entry:
            self._require_owned(self.paths.menu)
        if state.desktop_path:
            self._require_owned(Path(state.desktop_path))
        if desktop:
            self._require_owned(desktop)

    def _validate_sources(self, state: IntegrationState) -> tuple[Path, Path]:
        source = self.source
        if not source.is_file() and state.installed_version == __version__:
            source = self.paths.application
        if not source.is_absolute() or not source.is_file():
            raise IntegrationError("The running AppImage could not be found on disk.")
        with source.open("rb") as handle:
            header = handle.read(11)
        if header[:4] != b"\x7fELF" or header[8:11] not in (b"AI\x01", b"AI\x02"):
            raise IntegrationError("The application source is not an AppImage.")
        icon_source = self.icon_source
        if (
            icon_source is None or not icon_source.is_file()
        ) and state.installed_version == __version__:
            icon_source = self.paths.icon
        if icon_source is None or not icon_source.is_absolute() or not icon_source.is_file():
            raise IntegrationError("The application icon is missing from the AppImage.")
        for path in (self.paths.application, self.paths.icon):
            if _snapshot(path) is not None and not state.installed_version:
                raise IntegrationError(f"Refusing to replace an unregistered installation: {path}")
        if source.resolve() == self.paths.application.resolve() and not os.access(
            self.paths.application, os.X_OK
        ):
            raise IntegrationError("The installed AppImage is not executable.")
        return source, icon_source

    def _stage_launchers(
        self,
        transaction: _FileTransaction,
        state: IntegrationState,
        options: IntegrationOptions,
        desktop: Path | None,
        launcher: bytes,
    ) -> None:
        if options.menu_entry:
            self._require_owned(self.paths.menu)
            transaction.write(self.paths.menu, launcher)
        elif state.menu_entry:
            self._require_owned(self.paths.menu)
            transaction.remove(self.paths.menu)
        if state.desktop_path and Path(state.desktop_path) != desktop:
            self._require_owned(Path(state.desktop_path))
            transaction.remove(Path(state.desktop_path))
        if desktop:
            self._require_owned(desktop)
            transaction.write(desktop, launcher, mode=0o755)

    def _write_state(self, transaction: _FileTransaction, state: IntegrationState) -> None:
        content = json.dumps(asdict(state), indent=2) + "\n"
        transaction.write(self.paths.receipt, content.encode("utf-8"), mode=0o600)

    def _trust_desktop(self, path: Path) -> tuple[str, ...]:
        try:
            subprocess.run(
                ["gio", "set", str(path), "metadata::trusted", "true"],
                check=True,
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return (
                "Your desktop may require right-clicking the shortcut and choosing Allow Launching.",
            )
        return ()


def managed_appimage() -> Path | None:
    """Use the stable installation for autostart, including from a new download."""
    try:
        paths = IntegrationPaths.defaults()
        integration = AppImageIntegration(paths.application, paths.icon, paths)
        state = integration.load_state()
    except (OSError, RuntimeError):
        return None
    try:
        if (
            state.installed_version
            and _snapshot(paths.application) is not None
            and os.access(paths.application, os.X_OK)
        ):
            return paths.application
    except (OSError, IntegrationError):
        return None
    return None
