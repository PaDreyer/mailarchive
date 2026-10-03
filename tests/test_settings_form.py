from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mailarchive.domain.configuration import Settings
from mailarchive.presentation.settings_form import SettingsFormValues, prepare_settings_update


def form_values(root: Path, **overrides: object) -> SettingsFormValues:
    values = SettingsFormValues(
        state_database_path=str(root / "state.sqlite3"),
        default_poll_minutes="10",
        start_at_login=True,
        minimize_to_tray=False,
        warn_on_error=False,
    )
    return replace(values, **overrides)


class SettingsFormTests(unittest.TestCase):
    def test_prepares_update_without_mutating_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            current = Settings(default_poll_minutes=5, start_at_login=False)
            update = prepare_settings_update(
                current, form_values(root), current_database_path=root / "old.sqlite3"
            )
            self.assertEqual(update.database_path, root / "state.sqlite3")
        self.assertEqual(update.settings.default_poll_minutes, 10)
        self.assertTrue(update.settings.start_at_login)
        self.assertTrue(update.database_changed)
        self.assertTrue(update.startup_changed)
        self.assertEqual(current.default_poll_minutes, 5)
        self.assertFalse(current.start_at_login)

    def test_database_path_is_selection_not_settings_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = root / "state.sqlite3"
            update = prepare_settings_update(
                Settings(), form_values(root), current_database_path=database
            )
        self.assertFalse(update.database_changed)
        self.assertFalse(hasattr(update.settings, "state_database_path"))

    def test_rejects_bad_database_path_and_poll_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for path, message in ((str(root), "not a folder"), ("relative.sqlite3", "absolute")):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                    prepare_settings_update(
                        Settings(),
                        form_values(root, state_database_path=path),
                        current_database_path=root / "state.sqlite3",
                    )
            with self.assertRaisesRegex(ValueError, "whole number"):
                prepare_settings_update(
                    Settings(),
                    form_values(root, default_poll_minutes="often"),
                    current_database_path=root / "state.sqlite3",
                )


if __name__ == "__main__":
    unittest.main()
