import unittest

from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    DateFolderPosition,
    MailField,
    MailProvider,
    MatchMode,
    MatchOperator,
    Rule,
    SaveMode,
    Settings,
)


class ModelTests(unittest.TestCase):
    def test_default_destination_is_optional_and_legacy_implicit_inbox_is_preserved(self) -> None:
        self.assertEqual(Rule("New rule").destination, "")
        self.assertEqual(Settings.defaults().rules[0].destination, "")
        self.assertEqual(Settings.from_dict({"schema_version": 6}).rules[0].destination, "Inbox")
        self.assertEqual(Settings.from_dict({"schema_version": 7}).rules[0].destination, "")

    def test_date_folder_positions_round_trip_and_reject_unknown_values(self) -> None:
        for position in DateFolderPosition:
            rule = Rule("Mail", "", date_folder_position=position)
            self.assertEqual(Rule.from_dict(rule.to_dict()), rule)
        old = Rule.from_dict({"name": "Existing", "destination": "Finance/Supplier"})
        self.assertEqual(old.destination, "Finance/Supplier")
        self.assertEqual(old.date_folder_position, DateFolderPosition.NONE)
        for invalid in ("unknown", None, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Rule.from_dict({"date_folder_position": invalid})

    def test_rule_account_scopes_round_trip_without_broadening_empty_selection(self) -> None:
        for account_ids in (None, [], ["work", "personal"], ["unavailable-account"]):
            with self.subTest(account_ids=account_ids):
                rule = Rule("Invoices", "Finance", account_ids=account_ids)
                self.assertEqual(Rule.from_dict(rule.to_dict()).account_ids, account_ids)
        self.assertIsNone(Rule.from_dict({"name": "Old rule", "destination": "Inbox"}).account_ids)

    def test_malformed_rule_scope_is_rejected_instead_of_running_on_all_accounts(self) -> None:
        for account_ids in ("work", {}, [None], [1], [""]):
            with (
                self.subTest(account_ids=account_ids),
                self.assertRaisesRegex(ValueError, "account IDs"),
            ):
                Rule.from_dict({"name": "Bad rule", "account_ids": account_ids})

    def test_rule_scope_copies_ids_and_removes_duplicates(self) -> None:
        account_ids = ["work", "work", "personal"]
        rule = Rule("Invoices", "Finance", account_ids=account_ids)
        account_ids.append("new-account")
        self.assertEqual(rule.account_ids, ["work", "personal"])
        rule.to_dict()["account_ids"].append("new-account")
        self.assertEqual(rule.account_ids, ["work", "personal"])

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
            archive_existing_messages=True,
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
                    auth_mode=AuthMode.OAUTH_APPLICATION,
                ),
                "delegated OAuth",
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

    def test_generic_imap_supports_delegated_oauth_without_account_client_id(self) -> None:
        account = Account(
            label="Outlook IMAP",
            host="outlook.office365.com",
            username="me@outlook.com",
            provider=MailProvider.GENERIC_IMAP,
            auth_mode=AuthMode.OAUTH_USER,
            tenant_id="consumers",
        )

        account.validate()
        self.assertEqual(Account.from_dict(account.to_dict()), account)

    def test_generic_imap_oauth_rejects_untrusted_or_insecure_endpoints(self) -> None:
        base = {
            "label": "Outlook IMAP",
            "host": "outlook.office365.com",
            "port": 993,
            "username": "me@outlook.com",
            "provider": MailProvider.GENERIC_IMAP,
            "auth_mode": AuthMode.OAUTH_USER,
        }
        scenarios = (
            {"host": "imap.attacker.example"},
            {"port": 143},
            {"use_ssl": False},
        )
        for changes in scenarios:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "outlook.office365.com"),
            ):
                Account(**(base | changes)).validate()

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
        self.assertEqual(len(settings.rules), 1)
        self.assertEqual(Settings.from_dict(settings.to_dict()).to_dict(), settings.to_dict())

    def test_new_accounts_do_not_archive_existing_messages_by_default(self) -> None:
        self.assertFalse(Account(label="Mail").archive_existing_messages)
        self.assertFalse(
            Account.from_dict(
                {
                    "label": "Mail",
                    "host": "imap.example.org",
                    "username": "me@example.org",
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
