from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mailarchive.models import Settings


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "MailArchive"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "mailarchive"


class ConfigStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.path = self.data_dir / "config.json"

    @property
    def default_state_database_path(self) -> Path:
        return self.data_dir / "archive-state.sqlite3"

    def state_database_path(self, settings: Settings) -> Path:
        if settings.state_database_path:
            return Path(settings.state_database_path).expanduser()
        return self.default_state_database_path

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings.defaults()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return Settings.from_dict(json.load(handle))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read settings from '{self.path}': {exc}") from exc

    def save(self, settings: Settings) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="config-", suffix=".tmp", dir=self.data_dir
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)
