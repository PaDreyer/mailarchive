import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from mailarchive.updates import LATEST_RELEASE_API, Release, UpdateError, check_for_update


class UpdateTests(unittest.TestCase):
    def check(self, payload, current="0.1.0"):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with patch("mailarchive.updates.urlopen", return_value=response) as open_url:
            result = check_for_update(current)
        self.assertEqual(open_url.call_args.args[0].full_url, LATEST_RELEASE_API)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)
        return result

    def test_newer_version_uses_numeric_comparison_and_fixed_repository_url(self) -> None:
        result = self.check(
            {"tag_name": "v0.10.0", "html_url": "https://untrusted.example"}, "0.9.0"
        )
        self.assertEqual(result, Release("0.10.0"))
        self.assertEqual(result.url, "https://github.com/PaDreyer/mailarchive/releases/tag/v0.10.0")

    def test_equal_and_older_versions_do_not_offer_a_downgrade(self) -> None:
        for version in ("v0.1.0", "v0.0.9"):
            with self.subTest(version=version):
                self.assertIsNone(self.check({"tag_name": version}))

    def test_drafts_and_prereleases_are_ignored(self) -> None:
        for flag in ("draft", "prerelease"):
            with self.subTest(flag=flag):
                self.assertIsNone(self.check({"tag_name": "v1.0.0-beta.1", flag: True}))

    def test_missing_or_invalid_release_data_reports_error(self) -> None:
        for value in (
            [],
            {},
            {"tag_name": 123},
            {"tag_name": "1.0.0"},
            {"tag_name": "vv1.0.0"},
            {"tag_name": "v1.0.0/../../bad"},
            {"tag_name": "v01.0.0"},
        ):
            with self.subTest(value=value), self.assertRaises(UpdateError):
                self.check(value)

    def test_http_404_means_no_release_and_other_http_errors_are_reported(self) -> None:
        for status in (404, 403, 500):
            with (
                self.subTest(status=status),
                patch(
                    "mailarchive.updates.urlopen",
                    side_effect=HTTPError(LATEST_RELEASE_API, status, "failed", None, None),
                ),
            ):
                if status == 404:
                    self.assertIsNone(check_for_update())
                else:
                    with self.assertRaisesRegex(UpdateError, f"HTTP {status}"):
                        check_for_update()

    def test_offline_or_timeout_reports_error(self) -> None:
        for error in (URLError("offline"), TimeoutError("timed out")):
            with self.subTest(error=error), patch("mailarchive.updates.urlopen", side_effect=error):
                with self.assertRaises(UpdateError):
                    check_for_update()

    def test_invalid_json_and_oversized_response_report_error(self) -> None:
        for body in (b"not json", b"x" * (1024 * 1024 + 1)):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            with patch("mailarchive.updates.urlopen", return_value=response):
                with self.assertRaises(UpdateError):
                    check_for_update()
