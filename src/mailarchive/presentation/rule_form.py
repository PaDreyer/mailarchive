"""Normalize rule-editor values before changing settings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

from mailarchive.domain.archive_paths import destination_path
from mailarchive.domain.configuration import (
    Account,
    Condition,
    MailField,
    MatchMode,
    MatchOperator,
    Rule,
    RuleTarget,
)


@dataclass(frozen=True, slots=True)
class RuleFormValues:
    name: str
    targets: tuple[RuleTarget, ...]
    field: MailField
    operator: MatchOperator
    value: str
    sender_values: tuple[str, ...]
    enabled: bool
    all_accounts: bool
    selected_account_ids: tuple[str, ...]
    conditions: tuple[Condition, ...] | None = None
    match_mode: MatchMode | None = None


class DestinationValidationError(ValueError):
    def __init__(self, index: int, message: str) -> None:
        super().__init__(f"Destination {index + 1}: {message}")
        self.index = index


def rule_account_options(accounts: list[Account], rule: Rule | None) -> list[tuple[str, str]]:
    """Keep unavailable references editable without broadening a rule's scope."""
    options = [
        (
            account.id,
            f"{account.label} ({', '.join(mailbox.address for mailbox in account.mailboxes) or account.username})",
        )
        for account in accounts
    ]
    known_ids = {account.id for account in accounts}
    if rule and rule.account_ids is not None:
        options.extend(
            (account_id, f"Unavailable account ({account_id})")
            for account_id in rule.account_ids
            if account_id not in known_ids
        )
    return options


def _validated_condition(condition: Condition) -> Condition:
    value = condition.value.strip()
    if condition.field not in {MailField.ALL, MailField.HAS_ATTACHMENT} and not value:
        if condition.field == MailField.SENDER:
            raise ValueError("Enter a value in each sender field or remove it.")
        raise ValueError("Enter a comparison value.")
    if condition.field == MailField.HAS_ATTACHMENT and value.casefold() not in {
        "yes",
        "no",
        "true",
        "false",
        "1",
        "0",
    }:
        raise ValueError('For "Has attachments", enter Yes or No.')
    return replace(condition, value=value)


def _simple_conditions(values: RuleFormValues) -> list[Condition]:
    if values.field == MailField.SENDER:
        sender_values = [item.strip() for item in values.sender_values]
        if not sender_values or any(not item for item in sender_values):
            raise ValueError("Enter a value in each sender field or remove it.")
        return [
            Condition(field=values.field, operator=values.operator, value=item)
            for item in sender_values
        ]
    return [_validated_condition(Condition(values.field, values.operator, values.value))]


def has_simple_matching(rule: Rule) -> bool:
    """Whether the single-field/sender-list controls represent every condition."""
    return len(rule.conditions) <= 1 or (
        rule.match_mode == MatchMode.ANY
        and all(
            condition.field == MailField.SENDER
            and condition.operator == rule.conditions[0].operator
            for condition in rule.conditions
        )
    )


def _matching(values: RuleFormValues, existing: Rule | None) -> tuple[list[Condition], MatchMode]:
    if values.conditions is None:
        if existing is not None and not has_simple_matching(existing):
            raise ValueError("Edit the complete condition list to preserve this rule.")
        conditions = _simple_conditions(values)
        mode = values.match_mode or (MatchMode.ANY if len(conditions) > 1 else MatchMode.ALL)
    else:
        # Existing supported profiles may contain values the simplified editor
        # cannot create. Metadata edits must retain those matching semantics.
        previous = existing.conditions if existing is not None else []
        conditions = [
            replace(condition) if condition in previous else _validated_condition(condition)
            for condition in values.conditions
        ]
        mode = values.match_mode or MatchMode.ALL
    return conditions, mode


def build_rule(values: RuleFormValues, *, existing: Rule | None = None) -> Rule:
    name = values.name.strip()
    if not name:
        raise ValueError("Enter a name for the rule.")
    if not values.targets:
        raise ValueError("Add at least one destination.")
    for index, target in enumerate(values.targets):
        try:
            destination_path(target.path)
        except ValueError as exc:
            raise DestinationValidationError(index, str(exc)) from exc
    conditions, match_mode = _matching(values, existing)
    account_ids = None if values.all_accounts else list(values.selected_account_ids)
    if account_ids == []:
        raise ValueError("Select at least one email account or choose All email accounts.")
    return Rule(
        id=existing.id if existing else str(uuid4()),
        name=name,
        conditions=conditions,
        match_mode=match_mode,
        enabled=values.enabled,
        account_ids=account_ids,
        targets=[replace(target) for target in values.targets],
    )
