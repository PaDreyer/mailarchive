import unittest
from dataclasses import replace
from pathlib import Path

from mailarchive.models import (
    Account,
    DateFolderPosition,
    MailField,
    MatchMode,
    MatchOperator,
    Rule,
    SaveMode,
)
from mailarchive.rule_form import RuleFormValues, build_rule, rule_account_options
from mailarchive.ui_text import _account_scope_summary


class RuleFormTests(unittest.TestCase):
    def test_edit_can_enable_and_disable_direct_attachments_without_changing_date_folders(self):
        previous = Rule(
            "Invoices", "Finance", date_folder_position=DateFolderPosition.AFTER_SUBFOLDER
        )
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                rule = build_rule(
                    replace(
                        self.values,
                        date_folder_position=previous.date_folder_position,
                        attachments_in_destination=enabled,
                    ),
                    archive_root=Path("/archive"),
                    existing=previous,
                )
                self.assertEqual(rule.id, previous.id)
                self.assertEqual(rule.date_folder_position, previous.date_folder_position)
                self.assertEqual(rule.attachments_in_destination, enabled)
                previous = rule

    def test_edit_accepts_optional_subfolder_and_can_change_date_order(self) -> None:
        previous = Rule(
            "Old", "Finance", id="rule-id", date_folder_position=DateFolderPosition.BEFORE_SUBFOLDER
        )
        for destination in ("", " ", "Finance/Supplier"):
            for position in DateFolderPosition:
                with self.subTest(destination=destination, position=position):
                    rule = build_rule(
                        replace(
                            self.values, destination=destination, date_folder_position=position
                        ),
                        archive_root=Path("/archive"),
                        existing=previous,
                    )
                    self.assertEqual(rule.id, previous.id)
                    self.assertEqual(rule.destination, destination.strip())
                    self.assertEqual(rule.date_folder_position, position)

    def setUp(self) -> None:
        self.values = RuleFormValues(
            name=" Invoices ",
            destination=" Finance ",
            field=MailField.SUBJECT,
            operator=MatchOperator.CONTAINS,
            value=" invoice ",
            sender_values=(),
            save_mode=SaveMode.EMAIL_ONLY,
            enabled=True,
            all_accounts=True,
            selected_account_ids=(),
        )

    def test_edit_preserves_identity_and_normalizes_values_and_account_scope(self) -> None:
        previous = Rule("Old", "Old", id="rule-id")
        values = replace(
            self.values, all_accounts=False, selected_account_ids=("work", "work", "personal")
        )
        rule = build_rule(values, archive_root=Path("/archive"), existing=previous)
        self.assertEqual(rule.id, previous.id)
        self.assertEqual(rule.account_ids, ["work", "personal"])
        self.assertEqual(rule.name, "Invoices")
        self.assertEqual(rule.destination, "Finance")
        self.assertEqual(rule.conditions[0].value, "invoice")

    def test_switching_back_to_all_accounts_clears_previous_restriction(self) -> None:
        previous = Rule("Old", "Old", account_ids=["work"])
        rule = build_rule(
            replace(self.values, selected_account_ids=("work",)),
            archive_root=Path("/archive"),
            existing=previous,
        )
        self.assertIsNone(rule.account_ids)

    def test_restricted_scope_requires_at_least_one_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "Select at least one email account"):
            build_rule(replace(self.values, all_accounts=False), archive_root=Path("/archive"))

    def test_account_choices_and_summary_follow_renames_and_preserve_missing_ids(self) -> None:
        account = Account("Renamed work", username="work@example.com", id="work")
        rule = Rule("Scoped", "Inbox", account_ids=["work", "removed"])
        self.assertEqual(
            rule_account_options([account], rule),
            [
                ("work", "Renamed work (work@example.com)"),
                ("removed", "Unavailable account (removed)"),
            ],
        )
        self.assertEqual(
            _account_scope_summary(rule, [account]), "Renamed work, Unavailable account"
        )
        self.assertEqual(_account_scope_summary(Rule("All", "Inbox"), []), "All email accounts")
        self.assertEqual(
            _account_scope_summary(Rule("None", "Inbox", account_ids=[]), []), "No email accounts"
        )

    def test_sender_rules_keep_any_matching_with_a_mailbox_restriction(self) -> None:
        values = replace(
            self.values,
            field=MailField.SENDER,
            sender_values=(" one@example.com ", "two@example.com"),
            all_accounts=False,
            selected_account_ids=("work",),
        )
        rule = build_rule(values, archive_root=Path("/archive"))
        self.assertEqual(rule.match_mode, MatchMode.ANY)
        self.assertEqual(
            [item.value for item in rule.conditions], ["one@example.com", "two@example.com"]
        )
        self.assertEqual(rule.account_ids, ["work"])

    def test_rule_form_rejects_invalid_values(self) -> None:
        invalid = (
            replace(self.values, name=" "),
            replace(self.values, destination="../outside"),
            replace(self.values, value=" "),
            replace(self.values, field=MailField.HAS_ATTACHMENT, value="sometimes"),
            replace(self.values, field=MailField.SENDER, sender_values=()),
            replace(self.values, field=MailField.SENDER, sender_values=(" ",)),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                build_rule(values, archive_root=Path("/archive"))
