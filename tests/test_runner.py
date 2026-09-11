import unittest

from mailarchive.models import Account, Settings
from mailarchive.runner import polling_interval_minutes


class RunnerTests(unittest.TestCase):
    def test_account_uses_global_polling_interval_by_default(self) -> None:
        settings = Settings.defaults()
        settings.default_poll_minutes = 12
        account = Account(label="Default", poll_minutes=None)

        self.assertEqual(polling_interval_minutes(account, settings), 12)

    def test_account_polling_override_takes_precedence(self) -> None:
        settings = Settings.defaults()
        settings.default_poll_minutes = 12
        account = Account(label="Override", poll_minutes=3)

        self.assertEqual(polling_interval_minutes(account, settings), 3)


if __name__ == "__main__":
    unittest.main()
