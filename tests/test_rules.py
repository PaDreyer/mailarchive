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

    def test_first_matching_rule_wins(self) -> None:
        first = Rule("Invoices", "Finance", [Condition(MailField.SUBJECT, value="Invoice")])
        fallback = Rule("Other", "Inbox", [Condition(MailField.ALL)])
        self.assertIs(select_rule([first, fallback], self.mail), first)

    def test_disabled_rule_is_ignored_and_empty_rule_matches(self) -> None:
        disabled = Rule("Disabled", "A", enabled=False)
        empty = Rule("Empty", "B", conditions=[])

        self.assertFalse(rule_matches(disabled, self.mail))
        self.assertTrue(rule_matches(empty, self.mail))
        self.assertIs(select_rule([disabled, empty], self.mail), empty)
        self.assertIsNone(select_rule([disabled], self.mail))


if __name__ == "__main__":
    unittest.main()
