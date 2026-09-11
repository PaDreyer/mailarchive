from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class MailField(str, Enum):
    ALL = "all"
    SENDER = "sender"
    RECIPIENT = "recipient"
    SUBJECT = "subject"
    BODY = "body"
    HAS_ATTACHMENT = "has_attachment"


class MatchOperator(str, Enum):
    CONTAINS = "contains"
    EQUALS = "equals"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"


class SaveMode(str, Enum):
    EMAIL_ONLY = "email_only"
    EMAIL_AND_ATTACHMENTS = "email_and_attachments"
    ATTACHMENTS_ONLY = "attachments_only"


class MatchMode(str, Enum):
    ALL = "all"
    ANY = "any"


class MailProvider(str, Enum):
    GENERIC_IMAP = "generic_imap"
    GMAIL_API = "gmail_api"
    MICROSOFT_GRAPH = "microsoft_graph"


class AuthMode(str, Enum):
    PASSWORD = "password"
    OAUTH_USER = "oauth_user"
    OAUTH_APPLICATION = "oauth_application"


@dataclass(slots=True)
class Condition:
    field: MailField = MailField.ALL
    operator: MatchOperator = MatchOperator.CONTAINS
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field.value,
            "operator": self.operator.value,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Condition:
        return cls(
            field=MailField(value.get("field", MailField.ALL.value)),
            operator=MatchOperator(value.get("operator", MatchOperator.CONTAINS.value)),
            value=str(value.get("value", "")),
        )


@dataclass(slots=True)
class Rule:
    name: str
    destination: str
    conditions: list[Condition] = field(default_factory=list)
    save_mode: SaveMode = SaveMode.EMAIL_AND_ATTACHMENTS
    match_mode: MatchMode = MatchMode.ALL
    enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "destination": self.destination,
            "conditions": [condition.to_dict() for condition in self.conditions],
            "save_mode": self.save_mode.value,
            "match_mode": self.match_mode.value,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Rule:
        return cls(
            id=str(value.get("id") or uuid4()),
            name=str(value.get("name", "Unnamed rule")),
            destination=str(value.get("destination", "Inbox")),
            conditions=[Condition.from_dict(item) for item in value.get("conditions", [])],
            save_mode=SaveMode(value.get("save_mode", SaveMode.EMAIL_AND_ATTACHMENTS.value)),
            match_mode=MatchMode(value.get("match_mode", MatchMode.ALL.value)),
            enabled=bool(value.get("enabled", True)),
        )


@dataclass(slots=True)
class Account:
    label: str
    host: str = ""
    username: str = ""
    provider: MailProvider = MailProvider.GENERIC_IMAP
    auth_mode: AuthMode = AuthMode.PASSWORD
    port: int = 993
    folder: str = "INBOX"
    use_ssl: bool = True
    client_id: str = ""
    tenant_id: str = ""
    poll_minutes: int | None = None
    enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid4()))

    def validate(self, *, require_user_oauth_client: bool = True) -> None:
        if not self.label.strip():
            raise ValueError("Enter a name for the email account.")
        if not self.username.strip():
            raise ValueError("Enter the mailbox email address or username.")
        if self.provider == MailProvider.GENERIC_IMAP:
            if self.auth_mode != AuthMode.PASSWORD:
                raise ValueError("Generic IMAP currently requires password authentication.")
            if not self.host.strip():
                raise ValueError("Enter the IMAP server.")
            if not 1 <= self.port <= 65535:
                raise ValueError("The IMAP port must be between 1 and 65535.")
        elif self.provider == MailProvider.GMAIL_API:
            if self.auth_mode not in {
                AuthMode.OAUTH_USER,
                AuthMode.OAUTH_APPLICATION,
            }:
                raise ValueError("Gmail requires user or application authentication.")
            if (
                require_user_oauth_client
                and self.auth_mode == AuthMode.OAUTH_USER
                and not self.client_id.strip()
            ):
                raise ValueError("Enter the Google OAuth desktop client ID.")
        elif self.provider == MailProvider.MICROSOFT_GRAPH:
            if self.auth_mode not in {
                AuthMode.OAUTH_USER,
                AuthMode.OAUTH_APPLICATION,
            }:
                raise ValueError("Microsoft Graph requires OAuth authentication.")
            if (
                require_user_oauth_client
                and self.auth_mode == AuthMode.OAUTH_USER
                and not self.client_id.strip()
            ):
                raise ValueError("Enter the Microsoft Entra application client ID.")
            if self.tenant_id.strip() and (
                any(character in self.tenant_id for character in "/?#[]@")
                or any(character.isspace() for character in self.tenant_id)
            ):
                raise ValueError("Enter a valid Microsoft tenant ID or audience.")
            if self.auth_mode == AuthMode.OAUTH_APPLICATION:
                if not self.client_id.strip():
                    raise ValueError("Enter the Microsoft Entra application client ID.")
                if not self.tenant_id.strip():
                    raise ValueError("Enter the Microsoft Entra tenant ID.")
                if self.tenant_id.strip().casefold() in {
                    "common",
                    "organizations",
                    "consumers",
                }:
                    raise ValueError(
                        "Microsoft application access requires a tenant-specific tenant ID."
                    )
        if self.poll_minutes is not None and not 1 <= self.poll_minutes <= 1440:
            raise ValueError("The polling interval must be between 1 and 1440 minutes.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider.value,
            "auth_mode": self.auth_mode.value,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "folder": self.folder,
            "use_ssl": self.use_ssl,
            "client_id": self.client_id,
            "tenant_id": self.tenant_id,
            "poll_minutes": self.poll_minutes,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Account:
        auth_mode_value = str(value.get("auth_mode", AuthMode.PASSWORD.value))
        if auth_mode_value == "oauth_delegated":
            auth_mode_value = AuthMode.OAUTH_USER.value
        account = cls(
            id=str(value.get("id") or uuid4()),
            label=str(value.get("label", "Mailbox")),
            provider=MailProvider(value.get("provider", MailProvider.GENERIC_IMAP.value)),
            auth_mode=AuthMode(auth_mode_value),
            host=str(value.get("host", "")),
            port=int(value.get("port", 993)),
            username=str(value.get("username", "")),
            folder=str(value.get("folder", value.get("mailbox", "INBOX"))),
            use_ssl=bool(value.get("use_ssl", True)),
            client_id=str(value.get("client_id", "")),
            tenant_id=str(value.get("tenant_id", "")),
            poll_minutes=(
                int(value["poll_minutes"]) if value.get("poll_minutes") not in {None, ""} else None
            ),
            enabled=bool(value.get("enabled", True)),
        )
        # Keep user-OAuth accounts from older versions editable when their client
        # ID previously came from build-level configuration.
        account.validate(require_user_oauth_client=False)
        return account


def default_rule() -> Rule:
    return Rule(
        name="All remaining emails",
        destination="Inbox",
        conditions=[Condition(field=MailField.ALL)],
        save_mode=SaveMode.EMAIL_AND_ATTACHMENTS,
    )


@dataclass(slots=True)
class Settings:
    archive_root: str
    accounts: list[Account] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=lambda: [default_rule()])
    start_at_login: bool = True
    minimize_to_tray: bool = True
    warn_on_error: bool = True
    default_poll_minutes: int = 5
    archive_existing_messages: bool = False
    state_database_path: str = ""
    schema_version: int = 4

    def validate(self) -> None:
        if not 1 <= self.default_poll_minutes <= 1440:
            raise ValueError("The default polling interval must be between 1 and 1440 minutes.")

    @classmethod
    def defaults(cls) -> Settings:
        return cls(archive_root=str(Path.home() / "Documents" / "MailArchive"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "archive_root": self.archive_root,
            "accounts": [account.to_dict() for account in self.accounts],
            "rules": [rule.to_dict() for rule in self.rules],
            "start_at_login": self.start_at_login,
            "minimize_to_tray": self.minimize_to_tray,
            "warn_on_error": self.warn_on_error,
            "default_poll_minutes": self.default_poll_minutes,
            "archive_existing_messages": self.archive_existing_messages,
            "state_database_path": self.state_database_path,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Settings:
        rules = [Rule.from_dict(item) for item in value.get("rules", [])]
        settings = cls(
            schema_version=4,
            archive_root=str(value.get("archive_root") or cls.defaults().archive_root),
            accounts=[Account.from_dict(item) for item in value.get("accounts", [])],
            rules=rules or [default_rule()],
            start_at_login=bool(value.get("start_at_login", value.get("start_with_windows", True))),
            minimize_to_tray=bool(value.get("minimize_to_tray", True)),
            warn_on_error=bool(value.get("warn_on_error", True)),
            default_poll_minutes=int(value.get("default_poll_minutes", 5)),
            archive_existing_messages=bool(value.get("archive_existing_messages", False)),
            state_database_path=str(value.get("state_database_path") or ""),
        )
        settings.validate()
        return settings


@dataclass(slots=True)
class Attachment:
    filename: str
    content: bytes


@dataclass(slots=True)
class ParsedMail:
    raw: bytes
    subject: str
    sender: str
    recipients: str
    body: str
    message_id: str
    date_header: str
    attachments: list[Attachment] = field(default_factory=list)
