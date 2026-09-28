from __future__ import annotations

import unittest
from unittest.mock import patch

from mailarchive.timezone_choices import timezone_choices


class TimezoneChoicesTests(unittest.TestCase):
    def test_includes_installed_zones_utc_and_saved_choice(self) -> None:
        with patch(
            "mailarchive.timezone_choices.available_timezones",
            return_value={"Europe/Berlin", "America/New_York"},
        ):
            choices = timezone_choices("Etc/Custom")

        self.assertEqual(choices, ("America/New_York", "Etc/Custom", "Europe/Berlin", "UTC"))


if __name__ == "__main__":
    unittest.main()
