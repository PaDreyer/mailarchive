from __future__ import annotations

import unittest
from unittest.mock import patch

from mailarchive.provider_config import (
    MICROSOFT_CLIENT_ID_ENV,
    ProviderConfigurationError,
    main,
    require_bundled_microsoft_public_client_id,
    require_microsoft_public_client_id,
)


class ProviderConfigurationTests(unittest.TestCase):
    def test_development_override_supplies_public_client_id(self) -> None:
        client_id = "11111111-2222-4333-8444-555555555555"

        with patch.dict("os.environ", {MICROSOFT_CLIENT_ID_ENV: client_id}):
            self.assertEqual(require_microsoft_public_client_id(), client_id)

    def test_invalid_or_missing_client_id_is_rejected(self) -> None:
        with (
            patch.dict("os.environ", {MICROSOFT_CLIENT_ID_ENV: "not-a-client-id"}),
            self.assertRaisesRegex(ProviderConfigurationError, "not configured"),
        ):
            require_microsoft_public_client_id()

    def test_release_gate_requires_a_nonzero_bundled_uuid(self) -> None:
        with (
            patch(
                "mailarchive.provider_config.BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID",
                "00000000-0000-0000-0000-000000000000",
            ),
            self.assertRaisesRegex(ProviderConfigurationError, "not configured"),
        ):
            require_bundled_microsoft_public_client_id()

    def test_release_gate_command_exits_with_a_clean_configuration_error(self) -> None:
        with (
            patch(
                "mailarchive.provider_config.require_bundled_microsoft_public_client_id",
                side_effect=ProviderConfigurationError("Microsoft sign-in is not configured."),
            ),
            self.assertRaisesRegex(SystemExit, "Microsoft sign-in is not configured"),
        ):
            main()


if __name__ == "__main__":
    unittest.main()
