from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from mailarchive.models import Settings


@dataclass(frozen=True, slots=True)
class SettingsFormValues:
    """UI-independent values collected by the settings editor."""

    archive_root: str
    state_database_path: str
    default_poll_minutes: str
    start_at_login: bool
    minimize_to_tray: bool
    warn_on_error: bool


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    """A validated settings change and the filesystem transitions it needs."""

    settings: Settings
    archive_root: Path
    database_path: Path
    previous_database_path: Path
    database_changed: bool
    startup_changed: bool


def prepare_settings_update(
    current: Settings,
    values: SettingsFormValues,
    *,
    current_database_path: Path,
    default_database_path: Path,
) -> SettingsUpdate:
    """Normalize and validate settings before the application performs side effects."""
    archive_value = values.archive_root.strip()
    if not archive_value:
        raise ValueError("Choose an archive folder.")
    archive_root = Path(archive_value).expanduser()
    if archive_root.exists() and not archive_root.is_dir():
        raise ValueError("The archive path must point to a folder.")
    archive_root = archive_root.resolve()

    database_value = values.state_database_path.strip()
    if not database_value:
        raise ValueError("Choose a file for the archive processing database.")
    database_path = Path(database_value).expanduser()
    if database_path.exists() and database_path.is_dir():
        raise ValueError("The archive processing database path must point to a file, not a folder.")
    database_path = database_path.resolve()

    try:
        default_poll_minutes = int(values.default_poll_minutes)
    except ValueError as exc:
        raise ValueError("Enter a whole number for the default polling interval.") from exc

    default_database_path = default_database_path.expanduser().resolve()
    candidate = replace(
        current,
        archive_root=str(archive_root),
        default_poll_minutes=default_poll_minutes,
        start_at_login=values.start_at_login,
        minimize_to_tray=values.minimize_to_tray,
        warn_on_error=values.warn_on_error,
        state_database_path=("" if database_path == default_database_path else str(database_path)),
    )
    candidate.validate()

    previous_database_path = current_database_path.expanduser().resolve()
    return SettingsUpdate(
        settings=candidate,
        archive_root=archive_root,
        database_path=database_path,
        previous_database_path=previous_database_path,
        database_changed=database_path != previous_database_path,
        startup_changed=candidate.start_at_login != current.start_at_login,
    )
