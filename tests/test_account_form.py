from __future__ import annotations

import unittest

from mailarchive.account_form import COMMON_ACCOUNT_FIELDS, visible_account_fields
from mailarchive.models import AuthMode, MailProvider


class AccountFormTests(unittest.TestCase):
    def test_imap_shows_only_imap_connection_fields(self) -> None:
        self.assertEqual(
            visible_account_fields(MailProvider.GENERIC_IMAP, AuthMode.PASSWORD),
            COMMON_ACCOUNT_FIELDS | {"host", "port", "secret"},
        )

    def test_google_user_sign_in_shows_oauth_client_credentials(self) -> None:
        self.assertEqual(
            visible_account_fields(MailProvider.GMAIL_API, AuthMode.OAUTH_USER),
            COMMON_ACCOUNT_FIELDS | {"client_id", "secret"},
        )

    def test_google_domain_wide_delegation_shows_only_service_account(self) -> None:
        self.assertEqual(
            visible_account_fields(
                MailProvider.GMAIL_API,
                AuthMode.OAUTH_APPLICATION,
            ),
            COMMON_ACCOUNT_FIELDS | {"service_account_file"},
        )

    def test_microsoft_user_access_shows_public_client_fields(self) -> None:
        self.assertEqual(
            visible_account_fields(
                MailProvider.MICROSOFT_GRAPH,
                AuthMode.OAUTH_USER,
            ),
            COMMON_ACCOUNT_FIELDS | {"client_id", "tenant_id"},
        )

    def test_microsoft_application_access_adds_client_secret(self) -> None:
        self.assertEqual(
            visible_account_fields(
                MailProvider.MICROSOFT_GRAPH,
                AuthMode.OAUTH_APPLICATION,
            ),
            COMMON_ACCOUNT_FIELDS | {"client_id", "tenant_id", "secret"},
        )


if __name__ == "__main__":
    unittest.main()
