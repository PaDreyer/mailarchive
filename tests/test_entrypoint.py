import runpy
import unittest
from unittest import mock


class EntrypointTests(unittest.TestCase):
    def test_module_entrypoint_starts_application(self) -> None:
        with mock.patch("mailarchive.app.main") as main:
            runpy.run_module("mailarchive.__main__", run_name="__main__")

        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
