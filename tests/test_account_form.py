from __future__ import annotations

import unittest

from mailarchive.account_form import (
    COMMON_ACCOUNT_FIELDS,
    AccountFormValues,
    build_account_submission,
    visible_account_fields,
)
from mailarchive.models import Account, AuthMode, MailProvider


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

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported mail provider"):
            visible_account_fields(object(), AuthMode.PASSWORD)  # type: ignore[arg-type]

    def test_builds_normalized_imap_account_and_password_update(self) -> None:
        submission = build_account_submission(
            AccountFormValues(
                label="  Work  ",
                provider=MailProvider.GENERIC_IMAP,
                auth_mode=AuthMode.PASSWORD,
                username=" mail@example.com ",
                host=" imap.example.com ",
                port="993",
                secret="password",
                poll_minutes="15",
            )
        )

        self.assertEqual(submission.account.label, "Work")
        self.assertEqual(submission.account.username, "mail@example.com")
        self.assertEqual(submission.account.host, "imap.example.com")
        self.assertEqual(submission.account.poll_minutes, 15)
        self.assertEqual(submission.credential_updates, {"password": "password"})
        self.assertFalse(submission.replace_credentials)

    def test_unchanged_imap_binding_can_keep_existing_password(self) -> None:
        existing = Account(
            id="account-1",
            label="Old label",
            host="imap.example.com",
            username="mail@example.com",
        )

        submission = build_account_submission(
            AccountFormValues(
                label="New label",
                provider=MailProvider.GENERIC_IMAP,
                auth_mode=AuthMode.PASSWORD,
                username="MAIL@example.com",
                host="IMAP.example.com",
            ),
            existing=existing,
        )

        self.assertEqual(submission.account.id, existing.id)
        self.assertEqual(submission.credential_updates, {})
        self.assertFalse(submission.replace_credentials)

    def test_changed_imap_binding_requires_a_new_password(self) -> None:
        existing = Account(
            id="account-1",
            label="Work",
            host="imap.old.example",
            username="mail@example.com",
        )

        with self.assertRaisesRegex(ValueError, "password"):
            build_account_submission(
                AccountFormValues(
                    label="Work",
                    provider=MailProvider.GENERIC_IMAP,
                    auth_mode=AuthMode.PASSWORD,
                    username="mail@example.com",
                    host="imap.new.example",
                ),
                existing=existing,
            )

    def test_changed_microsoft_application_requires_a_new_secret(self) -> None:
        existing = Account(
            id="account-1",
            label="Microsoft",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_APPLICATION,
            username="mail@example.com",
            client_id="old-client",
            tenant_id="tenant",
        )

        with self.assertRaisesRegex(ValueError, "client secret"):
            build_account_submission(
                AccountFormValues(
                    label="Microsoft",
                    provider=MailProvider.MICROSOFT_GRAPH,
                    auth_mode=AuthMode.OAUTH_APPLICATION,
                    username="mail@example.com",
                    client_id="new-client",
                    tenant_id="tenant",
                ),
                existing=existing,
            )

    def test_changed_oauth_user_binding_discards_the_old_token(self) -> None:
        existing = Account(
            id="account-1",
            label="Gmail",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            username="old@example.com",
            client_id="client-id",
        )

        submission = build_account_submission(
            AccountFormValues(
                label="Gmail",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_USER,
                username="new@example.com",
                client_id="client-id",
            ),
            existing=existing,
        )

        self.assertTrue(submission.replace_credentials)
        self.assertEqual(submission.credential_updates, {})

    def test_google_application_loads_only_an_explicit_new_key(self) -> None:
        loaded_paths: list[str] = []

        def load_key(path: str) -> dict[str, str]:
            loaded_paths.append(path)
            return {"type": "service_account"}

        submission = build_account_submission(
            AccountFormValues(
                label="Workspace",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_APPLICATION,
                username="mail@example.com",
                service_account_file=" service-account.json ",
            ),
            service_account_loader=load_key,
        )

        self.assertEqual(loaded_paths, ["service-account.json"])
        self.assertEqual(
            submission.credential_updates,
            {"google_service_account": {"type": "service_account"}},
        )

    def test_invalid_numeric_fields_have_clear_errors(self) -> None:
        values = AccountFormValues(
            label="Work",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.PASSWORD,
            username="mail@example.com",
            host="imap.example.com",
            port="not-a-number",
            secret="password",
        )

        with self.assertRaisesRegex(ValueError, "whole number for the IMAP port"):
            build_account_submission(values)


if __name__ == "__main__":
    unittest.main()
