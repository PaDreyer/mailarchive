from __future__ import annotations

import hashlib
import json

from mailarchive.models import Condition, MailField, MatchMode, MatchOperator, ParsedMail, Rule


def matching_rules_fingerprint(rules: list[Rule], account_id: str) -> str:
    """Identify matching behavior for this account, independent of archive destinations."""
    signatures = {
        json.dumps(
            {
                "conditions": [condition.to_dict() for condition in rule.conditions],
                "match_mode": rule.match_mode.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for rule in rules
        if rule.enabled and (rule.account_ids is None or account_id in rule.account_ids)
    }
    # Bump this version if rule-matching semantics change without a settings change.
    payload = json.dumps({"version": 1, "rules": sorted(signatures)}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _condition_value(condition: Condition, mail: ParsedMail) -> str:
    return {
        MailField.SENDER: mail.sender,
        MailField.RECIPIENT: mail.recipients,
        MailField.SUBJECT: mail.subject,
        MailField.BODY: mail.body,
    }.get(condition.field, "")


def condition_matches(condition: Condition, mail: ParsedMail) -> bool:
    if condition.field == MailField.ALL:
        return True
    if condition.field == MailField.HAS_ATTACHMENT:
        desired = condition.value.strip().casefold() not in {"", "0", "false", "no"}
        return bool(mail.attachments) is desired

    actual = _condition_value(condition, mail).casefold()
    expected = condition.value.strip().casefold()
    if not expected:
        return False
    if condition.operator == MatchOperator.EQUALS:
        return actual.strip() == expected
    if condition.operator == MatchOperator.STARTS_WITH:
        return actual.startswith(expected)
    if condition.operator == MatchOperator.ENDS_WITH:
        return actual.endswith(expected)
    return expected in actual


def rule_matches(rule: Rule, mail: ParsedMail, account_id: str | None = None) -> bool:
    if not rule.enabled:
        return False
    if rule.account_ids is not None and account_id not in rule.account_ids:
        return False
    if not rule.conditions:
        return True
    matches = (condition_matches(condition, mail) for condition in rule.conditions)
    return any(matches) if rule.match_mode == MatchMode.ANY else all(matches)


def select_rule(rules: list[Rule], mail: ParsedMail, account_id: str | None = None) -> Rule | None:
    return next((rule for rule in rules if rule_matches(rule, mail, account_id)), None)
