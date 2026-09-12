from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

from mailarchive.models import (
    Account,
    AuthMode,
    DateFolderPosition,
    MailField,
    MailProvider,
    MatchMode,
    MatchOperator,
    Rule,
    SaveMode,
)
from mailarchive.storage import destination_path

FIELD_LABELS = {
    "All emails": MailField.ALL,
    "Sender": MailField.SENDER,
    "Recipient": MailField.RECIPIENT,
    "Subject": MailField.SUBJECT,
    "Message body": MailField.BODY,
    "Has attachments": MailField.HAS_ATTACHMENT,
}
OPERATOR_LABELS = {
    "contains": MatchOperator.CONTAINS,
    "equals": MatchOperator.EQUALS,
    "starts with": MatchOperator.STARTS_WITH,
    "ends with": MatchOperator.ENDS_WITH,
}
SAVE_LABELS = {
    "Email and attachments": SaveMode.EMAIL_AND_ATTACHMENTS,
    "Email only (.eml)": SaveMode.EMAIL_ONLY,
    "Attachments only": SaveMode.ATTACHMENTS_ONLY,
}
DATE_FOLDER_LABELS = {
    "No date folders": DateFolderPosition.NONE,
    "Year/month before subfolder": DateFolderPosition.BEFORE_SUBFOLDER,
    "Year/month after subfolder": DateFolderPosition.AFTER_SUBFOLDER,
}
PROVIDER_LABELS = {
    "Generic IMAP": MailProvider.GENERIC_IMAP,
    "Gmail (Google API)": MailProvider.GMAIL_API,
    "Outlook / Microsoft 365 (Microsoft Graph)": MailProvider.MICROSOFT_GRAPH,
}
AUTH_LABELS = {
    "Password": AuthMode.PASSWORD,
    "Microsoft OAuth (XOAUTH2)": AuthMode.OAUTH_USER,
    "Google OAuth - user sign-in": AuthMode.OAUTH_USER,
    "Google Workspace - domain-wide delegation": AuthMode.OAUTH_APPLICATION,
    "Microsoft OAuth - delegated user access": AuthMode.OAUTH_USER,
    "Microsoft OAuth - application access": AuthMode.OAUTH_APPLICATION,
}


LabelValue = TypeVar("LabelValue")


def _label_for(mapping: Mapping[str, LabelValue], value: object) -> str:
    return next((label for label, item in mapping.items() if item == value), str(value))


def _auth_label_for(provider: MailProvider, auth_mode: AuthMode) -> str:
    if provider == MailProvider.GENERIC_IMAP:
        if auth_mode == AuthMode.OAUTH_USER:
            return "Microsoft OAuth (XOAUTH2)"
        return "Password"
    if provider == MailProvider.GMAIL_API:
        if auth_mode == AuthMode.OAUTH_APPLICATION:
            return "Google Workspace - domain-wide delegation"
        return "Google OAuth - user sign-in"
    if provider == MailProvider.MICROSOFT_GRAPH:
        if auth_mode == AuthMode.OAUTH_APPLICATION:
            return "Microsoft OAuth - application access"
        return "Microsoft OAuth - delegated user access"
    return "Password"


def _condition_summary(rule: Rule) -> str:
    if not rule.conditions or rule.conditions[0].field == MailField.ALL:
        return "All emails"
    condition = rule.conditions[0]
    field = _label_for(FIELD_LABELS, condition.field)
    if condition.field == MailField.HAS_ATTACHMENT:
        yes = condition.value.strip().casefold() not in {"", "0", "false", "no"}
        return f"{field}: {'Yes' if yes else 'No'}"
    operator = _label_for(OPERATOR_LABELS, condition.operator)
    if (
        len(rule.conditions) > 1
        and rule.match_mode == MatchMode.ANY
        and all(
            item.field == MailField.SENDER and item.operator == condition.operator
            for item in rule.conditions
        )
    ):
        values = '", "'.join(item.value for item in rule.conditions)
        return f'{field} {operator} any of: "{values}"'
    return f'{field} {operator} "{condition.value}"'


def _account_scope_summary(rule: Rule, accounts: list[Account]) -> str:
    if rule.account_ids is None:
        return "All email accounts"
    if not rule.account_ids:
        return "No email accounts"
    labels = {account.id: account.label for account in accounts}
    return ", ".join(
        labels.get(account_id, "Unavailable account") for account_id in rule.account_ids
    )


def _destination_summary(rule: Rule, archive_root: Path) -> str:
    try:
        path = destination_path(archive_root, rule.destination, rule.date_folder_position)
    except ValueError:
        return "Invalid destination"
    relative = path.relative_to(archive_root)
    return str(relative) if relative != Path() else "Archive folder"
