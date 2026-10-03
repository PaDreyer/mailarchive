import unittest
from dataclasses import replace
from pathlib import Path

from mailarchive.domain.configuration import (
    Account,
    MailField,
    MatchMode,
    MatchOperator,
    Rule,
    RuleTarget,
    SaveMode,
)
from mailarchive.presentation.rule_form import (
    DestinationValidationError,
    RuleFormValues,
    build_rule,
    rule_account_options,
)
from mailarchive.presentation.ui_text import _account_scope_summary


class RuleFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.destination = str(Path.cwd() / "archive" / "Finance")
        self.values = RuleFormValues(
            name=" Invoices ",
            targets=(RuleTarget(self.destination, SaveMode.EMAIL_ONLY, id="target-id"),),
            field=MailField.SUBJECT,
            operator=MatchOperator.CONTAINS,
            value=" invoice ",
            sender_values=(),
            enabled=True,
            all_accounts=True,
            selected_account_ids=(),
        )

    def test_edit_preserves_rule_and_first_target_identity(self) -> None:
        previous = Rule("Old", id="rule-id", targets=[RuleTarget("/old", id="target-id")])
        rule = build_rule(
            replace(self.values, all_accounts=False, selected_account_ids=("work", "work")),
            existing=previous,
        )
        self.assertEqual(rule.id, "rule-id")
        self.assertEqual(rule.targets[0].id, "target-id")
        self.assertEqual(rule.targets[0].path, self.destination)
        self.assertEqual(rule.targets[0].save_mode, SaveMode.EMAIL_ONLY)
        self.assertEqual(rule.account_ids, ["work"])
        self.assertEqual(rule.conditions[0].value, "invoice")

    def test_full_paths_and_date_templates_are_valid(self) -> None:
        template = str(Path.cwd() / "archive" / "{year}" / "{month}" / "Finance")
        for destination in (self.destination, template):
            with self.subTest(destination=destination):
                rule = build_rule(replace(self.values, targets=(RuleTarget(destination),)))
                self.assertEqual(rule.targets[0].path, destination)

    def test_attachment_placement_is_per_target(self) -> None:
        targets = (
            RuleTarget(self.destination, SaveMode.EMAIL_ONLY, True, id="email"),
            RuleTarget(
                self.destination + "/attachments", SaveMode.ATTACHMENTS_ONLY, False, id="files"
            ),
        )
        rule = build_rule(replace(self.values, targets=targets))
        self.assertEqual(rule.targets, list(targets))
        self.assertIsNot(rule.targets[0], targets[0])
        rule.targets[0].path = "/changed"
        self.assertEqual(targets[0].path, self.destination)

    def test_edit_retains_remaining_target_identity_after_first_is_removed(self) -> None:
        first = RuleTarget(self.destination, id="first")
        second = RuleTarget(self.destination + "/second", id="second")
        previous = Rule("Old", id="rule-id", targets=[first, second])
        rule = build_rule(replace(self.values, targets=(second,)), existing=previous)
        self.assertEqual(rule.id, previous.id)
        self.assertEqual([target.id for target in rule.targets], ["second"])
        self.assertEqual(previous.targets, [first, second])

    def test_invalid_additional_target_identifies_its_index(self) -> None:
        values = replace(self.values, targets=(*self.values.targets, RuleTarget("relative")))
        with self.assertRaises(DestinationValidationError) as error:
            build_rule(values)
        self.assertEqual(error.exception.index, 1)
        self.assertIn("Destination 2", str(error.exception))

    def test_account_scope_retains_missing_ids_and_can_return_to_all(self) -> None:
        account = Account("Renamed work", username="work@example.com", id="work")
        scoped = Rule("Scoped", account_ids=["work", "removed"])
        self.assertEqual(
            rule_account_options([account], scoped),
            [
                ("work", "Renamed work (work@example.com)"),
                ("removed", "Unavailable account (removed)"),
            ],
        )
        self.assertEqual(
            _account_scope_summary(scoped, [account]), "Renamed work, Unavailable account"
        )
        self.assertEqual(_account_scope_summary(Rule("All"), []), "All email accounts")
        self.assertIsNone(build_rule(self.values, existing=scoped).account_ids)

    def test_sender_values_use_any_match(self) -> None:
        rule = build_rule(
            replace(
                self.values,
                field=MailField.SENDER,
                sender_values=(" one@example.com ", "two@example.com"),
            )
        )
        self.assertEqual(rule.match_mode, MatchMode.ANY)
        self.assertEqual(
            [condition.value for condition in rule.conditions],
            ["one@example.com", "two@example.com"],
        )

    def test_rejects_invalid_rule_fields(self) -> None:
        invalid = (
            replace(self.values, name=" "),
            replace(self.values, targets=(RuleTarget("../outside"),)),
            replace(self.values, targets=()),
            replace(self.values, value=" "),
            replace(self.values, field=MailField.HAS_ATTACHMENT, value="sometimes"),
            replace(self.values, field=MailField.SENDER, sender_values=(" ",)),
            replace(self.values, all_accounts=False),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                build_rule(values)


if __name__ == "__main__":
    unittest.main()
