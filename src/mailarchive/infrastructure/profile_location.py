"""Settings facade over the single local profile database."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mailarchive.domain.configuration import Settings
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_ownership import exclusive_profile_directory


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "MailArchive"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "mailarchive"


class ConfigStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.location_file = self.data_dir / "database-location.json"
        self.path = self._load_database_path()

    def _load_database_path(self) -> Path:
        if not self.location_file.exists():
            return self.default_state_database_path.resolve()
        try:
            location = json.loads(self.location_file.read_text(encoding="utf-8"))
            if (
                not isinstance(location, dict)
                or location.get("version") != 1
                or not isinstance(location.get("path"), str)
                or not Path(location["path"]).is_absolute()
            ):
                raise ValueError("Invalid database location")
            return Path(location["path"]).resolve()
        except (OSError, ValueError) as exc:
            raise RuntimeError("Could not read the MailArchive database location.") from exc

    def _save_database_path(self, path: Path) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".database-location-", dir=self.data_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "path": str(path)}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.location_file)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def default_state_database_path(self) -> Path:
        return self.data_dir / "workspace.sqlite3"

    def state_database_path(self, _settings: Settings | None = None) -> Path:
        if self.location_file.exists() and not self.path.exists():
            raise RuntimeError(f"The configured database is missing: {self.path}")
        return self.path

    def load(self) -> Settings:
        return ProfileDatabase(self.state_database_path()).configuration.load_settings()

    def save(self, settings: Settings) -> None:
        ProfileDatabase(self.state_database_path(settings)).configuration.save_settings(settings)

    def select_database(self, destination: Path) -> tuple[ProfileDatabase, Settings]:
        """Open an existing profile or create a fresh one at the selected path."""
        existed = destination.expanduser().resolve().exists()
        replacement, settings = self.prepare_database(destination)
        try:
            self.activate_database(replacement.database_path)
        except Exception:
            if not existed:
                self._remove_empty_profile(replacement.database_path)
            raise
        return replacement, settings

    def activate_database(self, destination: Path) -> None:
        """Publish a validated selection only after its old worker has stopped."""
        destination = destination.expanduser().resolve()
        self._save_database_path(destination)
        self.path = destination

    @staticmethod
    def _remove_empty_profile(destination: Path) -> None:
        if not destination.exists():
            return
        with exclusive_profile_directory(destination):
            destination.unlink()
            work = destination.parent / "work"
            if work.is_dir():
                try:
                    work.rmdir()
                except OSError:
                    pass

    def prepare_database(self, destination: Path) -> tuple[ProfileDatabase, Settings]:
        """Validate a candidate without changing the selected profile location."""
        destination = destination.expanduser().resolve()
        if destination == self.path:
            raise ValueError("The selected database is already open.")
        if destination.parent == self.path.parent:
            raise ValueError("Choose a different folder for each database's work files.")
        existed = destination.exists()
        if not existed:
            work = destination.parent / "work"
            if work.is_symlink() or (work.exists() and (not work.is_dir() or any(work.iterdir()))):
                raise ValueError("The new database folder already contains mail work files.")
        try:
            replacement = ProfileDatabase(destination, recover=True)
            settings = replacement.configuration.load_settings()
        except Exception:
            if not existed:
                self._remove_empty_profile(destination)
            raise
        return replacement, settings
