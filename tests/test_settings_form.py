from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mailarchive.models import Settings
from mailarchive.settings_form import SettingsFormValues, prepare_settings_update


def form_values(root: Path, **overrides: object) -> SettingsFormValues:
    values = SettingsFormValues(
        archive_root=str(root / "archive"),
        state_database_path=str(root / "state.sqlite3"),
        default_poll_minutes="10",
        start_at_login=True,
        minimize_to_tray=False,
        warn_on_error=False,
    )
    return replace(values, **overrides)


class SettingsFormTests(unittest.TestCase):
    def test_prepares_normalized_update_without_mutating_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_database = root / "old.sqlite3"
            default_database = root / "default.sqlite3"
            current = Settings(
                archive_root=str(root / "old-archive"),
                default_poll_minutes=5,
                start_at_login=False,
            )

            update = prepare_settings_update(
                current,
                form_values(root),
                current_database_path=current_database,
                default_database_path=default_database,
            )

        self.assertEqual(update.settings.archive_root, str(root / "old-archive"))
        self.assertEqual(update.settings.default_poll_minutes, 10)
        self.assertTrue(update.settings.start_at_login)
        self.assertFalse(update.settings.minimize_to_tray)
        self.assertFalse(update.database_changed)
        self.assertTrue(update.startup_changed)
        self.assertEqual(current.default_poll_minutes, 5)
        self.assertFalse(current.start_at_login)

    def test_default_database_path_is_not_serialized_as_an_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_database = root / "default.sqlite3"

            update = prepare_settings_update(
                Settings(archive_root=str(root / "old-archive")),
                form_values(root, state_database_path=str(default_database)),
                current_database_path=default_database,
                default_database_path=default_database,
            )

        self.assertEqual(update.settings.state_database_path, "")
        self.assertFalse(update.database_changed)

    def test_global_archive_path_is_no_longer_a_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_file = root / "archive-file"
            archive_file.write_text("not a directory")
            current = Settings(archive_root=str(root / "old-archive"))

            for archive_root in (" ", str(archive_file)):
                with self.subTest(archive_root=archive_root):
                    update = prepare_settings_update(
                        current,
                        form_values(root, archive_root=archive_root),
                        current_database_path=root / "state.sqlite3",
                        default_database_path=root / "default.sqlite3",
                    )
                    self.assertEqual(update.settings.archive_root, current.archive_root)

    def test_ignores_old_database_path_and_rejects_non_numeric_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = Settings(archive_root=str(root / "old-archive"))

            update = prepare_settings_update(
                current,
                form_values(root, state_database_path=str(root)),
                current_database_path=root / "state.sqlite3",
                default_database_path=root / "default.sqlite3",
            )
            self.assertFalse(update.database_changed)
            with self.assertRaisesRegex(ValueError, "whole number"):
                prepare_settings_update(
                    current,
                    form_values(root, default_poll_minutes="often"),
                    current_database_path=root / "state.sqlite3",
                    default_database_path=root / "default.sqlite3",
                )


if __name__ == "__main__":
    unittest.main()
