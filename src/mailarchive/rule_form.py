"""Normalize rule-editor values before changing settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from mailarchive.models import (
    Account,
    Condition,
    DateFolderPosition,
    MailField,
    MatchMode,
    MatchOperator,
    Rule,
    SaveMode,
)
from mailarchive.storage import destination_path


@dataclass(frozen=True, slots=True)
class RuleFormValues:
    name: str
    destination: str
    field: MailField
    operator: MatchOperator
    value: str
    sender_values: tuple[str, ...]
    save_mode: SaveMode
    enabled: bool
    all_accounts: bool
    selected_account_ids: tuple[str, ...]
    date_folder_position: DateFolderPosition = DateFolderPosition.NONE
    attachments_in_destination: bool = False


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


def build_rule(values: RuleFormValues, *, archive_root: Path, existing: Rule | None = None) -> Rule:
    name = values.name.strip()
    if not name:
        raise ValueError("Enter a name for the rule.")
    destination = values.destination.strip()
    destination_path(archive_root, destination, values.date_folder_position)
    value = values.value.strip()
    if (
        values.field not in {MailField.ALL, MailField.HAS_ATTACHMENT, MailField.SENDER}
        and not value
    ):
        raise ValueError("Enter a comparison value.")
    if values.field == MailField.HAS_ATTACHMENT and value.casefold() not in {
        "yes",
        "no",
        "true",
        "false",
        "1",
        "0",
    }:
        raise ValueError('For "Has attachments", enter Yes or No.')
    if values.field == MailField.SENDER:
        sender_values = [item.strip() for item in values.sender_values]
        if not sender_values or any(not item for item in sender_values):
            raise ValueError("Enter a value in each sender field or remove it.")
        conditions = [
            Condition(field=values.field, operator=values.operator, value=item)
            for item in sender_values
        ]
    else:
        conditions = [Condition(field=values.field, operator=values.operator, value=value)]
    account_ids = None if values.all_accounts else list(values.selected_account_ids)
    if account_ids == []:
        raise ValueError("Select at least one email account or choose All email accounts.")
    return Rule(
        id=existing.id if existing else str(uuid4()),
        name=name,
        destination=destination,
        conditions=conditions,
        save_mode=values.save_mode,
        match_mode=MatchMode.ANY if len(conditions) > 1 else MatchMode.ALL,
        enabled=values.enabled,
        account_ids=account_ids,
        date_folder_position=values.date_folder_position,
        attachments_in_destination=values.attachments_in_destination,
    )
