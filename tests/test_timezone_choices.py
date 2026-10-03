from __future__ import annotations

import unittest
from unittest.mock import patch

from mailarchive.presentation.timezone_choices import local_timezone_name, timezone_choices


class TimezoneChoicesTests(unittest.TestCase):
    def test_includes_installed_zones_utc_and_saved_choice(self) -> None:
        with patch(
            "mailarchive.presentation.timezone_choices.available_timezones",
            return_value={"Europe/Berlin", "America/New_York"},
        ):
            choices = timezone_choices("Etc/Custom")

        self.assertEqual(choices, ("America/New_York", "Etc/Custom", "Europe/Berlin", "UTC"))

    def test_uses_valid_system_zone_instead_of_archive_timezone(self) -> None:
        for name in ("Europe/Berlin", "America/New_York", "Asia/Kolkata", "UTC"):
            with (
                self.subTest(name=name),
                patch(
                    "mailarchive.presentation.timezone_choices.get_localzone_name",
                    return_value=name,
                ),
            ):
                self.assertEqual(local_timezone_name("Asia/Tokyo"), name)

    def test_missing_system_zone_uses_archive_timezone(self) -> None:
        for name in (None, ""):
            with (
                self.subTest(name=name),
                patch(
                    "mailarchive.presentation.timezone_choices.get_localzone_name",
                    return_value=name,
                ),
            ):
                self.assertEqual(local_timezone_name("Asia/Tokyo"), "Asia/Tokyo")

    def test_unknown_system_zone_uses_archive_timezone(self) -> None:
        with (
            patch(
                "mailarchive.presentation.timezone_choices.get_localzone_name",
                return_value="Unknown/Timezone",
            ),
            self.assertLogs("mailarchive.presentation.timezone_choices", level="WARNING"),
        ):
            self.assertEqual(local_timezone_name("Asia/Tokyo"), "Asia/Tokyo")

    def test_system_lookup_failure_uses_archive_timezone(self) -> None:
        for error in (OSError("Access denied"), LookupError("No timezone"), ValueError("Conflict")):
            with (
                self.subTest(error=error),
                patch(
                    "mailarchive.presentation.timezone_choices.get_localzone_name",
                    side_effect=error,
                ),
                self.assertLogs("mailarchive.presentation.timezone_choices", level="WARNING"),
            ):
                self.assertEqual(local_timezone_name("Asia/Tokyo"), "Asia/Tokyo")


if __name__ == "__main__":
    unittest.main()
