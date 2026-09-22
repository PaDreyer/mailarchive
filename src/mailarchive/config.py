"""Settings facade over the single local profile database."""

from __future__ import annotations

import os
from pathlib import Path

from mailarchive.models import Settings
from mailarchive.workspace import WorkspaceStore


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "MailArchive"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "mailarchive"


class ConfigStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.path = self.data_dir / "workspace.sqlite3"

    @property
    def default_state_database_path(self) -> Path:
        return self.path

    def state_database_path(self, _settings: Settings) -> Path:
        return self.path

    def load(self) -> Settings:
        return WorkspaceStore(self.path).load_settings()

    def save(self, settings: Settings) -> None:
        WorkspaceStore(self.path).save_settings(settings)
