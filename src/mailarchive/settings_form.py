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
    archive_timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    """A validated settings change and its selected database path."""

    settings: Settings
    database_path: Path
    database_changed: bool
    startup_changed: bool


def prepare_settings_update(
    current: Settings,
    values: SettingsFormValues,
    *,
    current_database_path: Path,
) -> SettingsUpdate:
    """Normalize and validate settings before the application performs side effects."""
    database_value = values.state_database_path.strip()
    if not database_value:
        raise ValueError("Enter a database file path.")
    database_path = Path(database_value).expanduser()
    if not database_path.is_absolute():
        raise ValueError("Enter an absolute database file path.")
    database_path = database_path.resolve()
    if database_path.is_dir():
        raise ValueError("Enter a database file path, not a folder.")

    try:
        default_poll_minutes = int(values.default_poll_minutes)
    except ValueError as exc:
        raise ValueError("Enter a whole number for the default polling interval.") from exc

    candidate = replace(
        current,
        default_poll_minutes=default_poll_minutes,
        start_at_login=values.start_at_login,
        minimize_to_tray=values.minimize_to_tray,
        warn_on_error=values.warn_on_error,
        archive_timezone=values.archive_timezone.strip(),
    )
    candidate.validate()

    previous_database_path = current_database_path.expanduser().resolve()
    return SettingsUpdate(
        settings=candidate,
        database_path=database_path,
        database_changed=database_path != previous_database_path,
        startup_changed=candidate.start_at_login != current.start_at_login,
    )
