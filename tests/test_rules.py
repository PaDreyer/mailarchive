import unittest

from mailarchive.mail_parser import parse_mail
from mailarchive.models import Condition, MailField, MatchMode, MatchOperator, Rule
from mailarchive.rules import condition_matches, rule_matches, select_rule
from tests.helpers import sample_mail


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mail = parse_mail(sample_mail(attachments=[("invoice.pdf", b"pdf")]))

    def test_contains_is_case_insensitive(self) -> None:
        condition = Condition(MailField.SUBJECT, MatchOperator.CONTAINS, "INVOICE")
        self.assertTrue(condition_matches(condition, self.mail))

    def test_text_operators_and_fields(self) -> None:
        matching = [
            Condition(MailField.SENDER, MatchOperator.EQUALS, "invoices@example.com"),
            Condition(MailField.RECIPIENT, MatchOperator.EQUALS, "customer@example.org"),
            Condition(MailField.SUBJECT, MatchOperator.STARTS_WITH, "monthly"),
            Condition(MailField.BODY, MatchOperator.CONTAINS, "attached"),
        ]

        for condition in matching:
            with self.subTest(condition=condition):
                self.assertTrue(condition_matches(condition, self.mail))

    def test_blank_text_condition_never_matches(self) -> None:
        self.assertFalse(condition_matches(Condition(MailField.SUBJECT, value="  "), self.mail))

    def test_attachment_condition_understands_yes_no(self) -> None:
        self.assertTrue(
            condition_matches(Condition(MailField.HAS_ATTACHMENT, value="Yes"), self.mail)
        )
        self.assertFalse(
            condition_matches(Condition(MailField.HAS_ATTACHMENT, value="No"), self.mail)
        )

    def test_all_and_any_matching(self) -> None:
        conditions = [
            Condition(MailField.SUBJECT, MatchOperator.CONTAINS, "Invoice"),
            Condition(MailField.SENDER, MatchOperator.CONTAINS, "wrong.example"),
        ]
        self.assertFalse(rule_matches(Rule("All", "A", conditions), self.mail))
        self.assertTrue(
            rule_matches(Rule("Any", "A", conditions, match_mode=MatchMode.ANY), self.mail)
        )

    def test_multiple_sender_conditions_match_any_address(self) -> None:
        rule = Rule(
            "Known senders",
            "A",
            [
                Condition(MailField.SENDER, MatchOperator.EQUALS, "other@example.com"),
                Condition(MailField.SENDER, MatchOperator.EQUALS, "invoices@example.com"),
            ],
            match_mode=MatchMode.ANY,
        )

        self.assertTrue(rule_matches(rule, self.mail))

    def test_first_matching_rule_wins(self) -> None:
        first = Rule("Invoices", "Finance", [Condition(MailField.SUBJECT, value="Invoice")])
        fallback = Rule("Other", "Inbox", [Condition(MailField.ALL)])
        self.assertIs(select_rule([first, fallback], self.mail), first)

    def test_account_scope_filters_before_first_matching_rule_selection(self) -> None:
        scoped = Rule("Work invoices", "Work", account_ids=["work", "second-work"])
        fallback = Rule("Other", "Inbox")
        for account_id in ("work", "second-work"):
            with self.subTest(account_id=account_id):
                self.assertIs(select_rule([scoped, fallback], self.mail, account_id), scoped)
        self.assertIs(select_rule([scoped, fallback], self.mail, "personal"), fallback)

    def test_account_scope_does_not_bypass_message_conditions_or_enabled_flag(self) -> None:
        scoped = Rule(
            "Work", "Work", [Condition(MailField.SUBJECT, value="wrong")], account_ids=["work"]
        )
        self.assertFalse(rule_matches(scoped, self.mail, "work"))
        scoped.conditions = [Condition(MailField.ALL)]
        scoped.enabled = False
        self.assertFalse(rule_matches(scoped, self.mail, "work"))

    def test_missing_account_context_and_empty_or_deleted_scope_never_match(self) -> None:
        scoped = Rule("Scoped", "Work", account_ids=["deleted-account"])
        self.assertFalse(rule_matches(scoped, self.mail))
        self.assertIsNone(select_rule([scoped], self.mail, "new-account"))
        scoped.account_ids = []
        self.assertFalse(rule_matches(scoped, self.mail, "work"))

    def test_any_sender_matching_is_still_restricted_to_selected_accounts(self) -> None:
        scoped = Rule(
            "Work",
            "Work",
            [
                Condition(MailField.SENDER, value="invoices@example.com"),
                Condition(MailField.SENDER, value="other@example.com"),
            ],
            match_mode=MatchMode.ANY,
            account_ids=["work"],
        )
        self.assertTrue(rule_matches(scoped, self.mail, "work"))
        self.assertFalse(rule_matches(scoped, self.mail, "personal"))

    def test_disabled_rule_is_ignored_and_empty_rule_matches(self) -> None:
        disabled = Rule("Disabled", "A", enabled=False)
        empty = Rule("Empty", "B", conditions=[])

        self.assertFalse(rule_matches(disabled, self.mail))
        self.assertTrue(rule_matches(empty, self.mail))
        self.assertIs(select_rule([disabled, empty], self.mail), empty)
        self.assertIsNone(select_rule([disabled], self.mail))


if __name__ == "__main__":
    unittest.main()
