"""Source failure scope is available without HTTP or IMAP coupling."""

import unittest

from mailarchive.application.source_port import (
    MailboxError,
    RemoteMessageError,
    ScanWideProviderError,
    is_scan_wide_error,
)
from mailarchive.infrastructure.providers.http import ProviderHttpError


class SourcePortTests(unittest.TestCase):
    def test_scan_wide_failures_stop_discovery(self) -> None:
        self.assertTrue(is_scan_wide_error(ScanWideProviderError("stream failed")))
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.assertTrue(is_scan_wide_error(ProviderHttpError(status, "unavailable")))

    def test_message_failures_allow_later_ids(self) -> None:
        for error in (
            MailboxError("folder unavailable"),
            RemoteMessageError("one malformed message"),
            ProviderHttpError(404, "missing"),
            ProviderHttpError(500, "temporary"),
            RuntimeError("local failure"),
        ):
            with self.subTest(error=error):
                self.assertFalse(is_scan_wide_error(error))


if __name__ == "__main__":
    unittest.main()
