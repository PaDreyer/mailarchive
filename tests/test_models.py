import unittest

from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    MailField,
    MailProvider,
    MatchMode,
    MatchOperator,
    Rule,
    SaveMode,
    Settings,
)


class ModelTests(unittest.TestCase):
    def test_condition_and_rule_round_trip(self) -> None:
        rule = Rule(
            name="Invoices",
            destination="Finance",
            conditions=[Condition(MailField.SUBJECT, MatchOperator.ENDS_WITH, "invoice")],
            save_mode=SaveMode.EMAIL_ONLY,
            match_mode=MatchMode.ANY,
            enabled=False,
            id="rule-id",
        )

        self.assertEqual(Rule.from_dict(rule.to_dict()), rule)

    def test_account_round_trip_preserves_provider_settings(self) -> None:
        account = Account(
            label="Work",
            username="person@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            tenant_id="organizations",
            poll_minutes=17,
            enabled=False,
            id="account-id",
        )

        self.assertEqual(Account.from_dict(account.to_dict()), account)

    def test_account_validation_rejects_invalid_common_fields(self) -> None:
        invalid_accounts = [
            (Account(label=" ", host="mail.example", username="user"), "name"),
            (Account(label="Mail", host="mail.example", username=" "), "mailbox"),
            (Account(label="Mail", host="", username="user"), "server"),
            (Account(label="Mail", host="mail.example", username="user", port=0), "port"),
            (
                Account(
                    label="Mail",
                    host="mail.example",
                    username="user",
                    auth_mode=AuthMode.OAUTH_USER,
                ),
                "password authentication",
            ),
            (
                Account(
                    label="Mail",
                    host="mail.example",
                    username="user",
                    poll_minutes=1441,
                ),
                "polling interval",
            ),
        ]

        for account, message in invalid_accounts:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    account.validate()

    def test_gmail_validation_requires_oauth_and_user_client_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "Gmail requires"):
            Account(
                label="Gmail",
                username="person@example.com",
                provider=MailProvider.GMAIL_API,
            ).validate()

        with self.assertRaisesRegex(ValueError, "client ID"):
            Account(
                label="Gmail",
                username="person@example.com",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_USER,
            ).validate()

        Account(
            label="Workspace",
            username="person@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_APPLICATION,
        ).validate()

    def test_microsoft_application_validation_requires_tenant_specific_config(self) -> None:
        base = {
            "label": "Microsoft",
            "username": "person@example.com",
            "provider": MailProvider.MICROSOFT_GRAPH,
            "auth_mode": AuthMode.OAUTH_APPLICATION,
        }
        invalid = [
            ({}, "client ID"),
            ({"client_id": "client"}, "tenant ID"),
            ({"client_id": "client", "tenant_id": "common"}, "tenant-specific"),
            ({"client_id": "client", "tenant_id": "bad tenant"}, "valid Microsoft tenant"),
        ]

        for overrides, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    Account(**base, **overrides).validate()

        Account(**base, client_id="client", tenant_id="tenant-id").validate()

    def test_settings_round_trip_supplies_default_rule_and_legacy_startup_flag(self) -> None:
        settings = Settings.from_dict(
            {
                "archive_root": "/archive",
                "rules": [],
                "start_with_windows": False,
                "default_poll_minutes": 10,
            }
        )

        self.assertEqual(settings.archive_root, "/archive")
        self.assertFalse(settings.start_at_login)
        self.assertFalse(settings.archive_existing_messages)
        self.assertEqual(len(settings.rules), 1)
        self.assertEqual(Settings.from_dict(settings.to_dict()).to_dict(), settings.to_dict())

    def test_new_settings_do_not_archive_existing_messages_by_default(self) -> None:
        settings = Settings.defaults()

        self.assertFalse(settings.archive_existing_messages)
        self.assertFalse(
            Settings.from_dict(
                {
                    "schema_version": 4,
                    "archive_root": "/archive",
                    "archive_existing_messages": False,
                }
            ).archive_existing_messages
        )

    def test_settings_validation_rejects_invalid_default_poll_interval(self) -> None:
        for value in (0, 1441):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "default polling interval"):
                    Settings("/archive", default_poll_minutes=value).validate()


if __name__ == "__main__":
    unittest.main()
