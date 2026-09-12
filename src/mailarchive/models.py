from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

SETTINGS_SCHEMA_VERSION = 8


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


class DateFolderPosition(str, Enum):
    NONE = "none"
    BEFORE_SUBFOLDER = "before_subfolder"
    AFTER_SUBFOLDER = "after_subfolder"


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


MICROSOFT_IMAP_HOST = "outlook.office365.com"
MICROSOFT_IMAP_PORT = 993


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
    destination: str = ""
    conditions: list[Condition] = field(default_factory=list)
    save_mode: SaveMode = SaveMode.EMAIL_AND_ATTACHMENTS
    match_mode: MatchMode = MatchMode.ALL
    enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid4()))
    # None includes all current and future accounts. An empty list matches no account.
    account_ids: list[str] | None = None
    date_folder_position: DateFolderPosition = DateFolderPosition.NONE

    def __post_init__(self) -> None:
        self.date_folder_position = DateFolderPosition(self.date_folder_position)
        if self.account_ids is not None:
            if not isinstance(self.account_ids, list) or any(
                not isinstance(account_id, str) or not account_id.strip()
                for account_id in self.account_ids
            ):
                raise ValueError("Rule email accounts must be a list of nonempty account IDs.")
            self.account_ids = list(dict.fromkeys(self.account_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "destination": self.destination,
            "date_folder_position": self.date_folder_position.value,
            "conditions": [condition.to_dict() for condition in self.conditions],
            "save_mode": self.save_mode.value,
            "match_mode": self.match_mode.value,
            "enabled": self.enabled,
            "account_ids": self.account_ids.copy() if self.account_ids is not None else None,
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
            account_ids=value.get("account_ids"),
            date_folder_position=DateFolderPosition(
                value.get("date_folder_position", DateFolderPosition.NONE.value)
            ),
        )


@dataclass(slots=True)
class Mailbox:
    address: str
    folders: list[str] = field(default_factory=list)
    archive_existing_messages: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        self.address = self.address.strip()
        if isinstance(self.folders, list):
            self.folders = [
                folder.strip() if isinstance(folder, str) else folder for folder in self.folders
            ]

    def validate(self) -> None:
        if not self.address.strip() or any(char in self.address for char in "\r\n\x00"):
            raise ValueError("Enter a valid mailbox address or username.")
        if not isinstance(self.folders, list) or any(
            not isinstance(folder, str)
            or not folder.strip()
            or any(char in folder for char in "\r\n\x00")
            for folder in self.folders
        ):
            raise ValueError("Mailbox folders must be nonempty names or IDs.")
        if len(set(self.folders)) != len(self.folders):
            raise ValueError("Each mailbox folder may be selected only once.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "folders": self.folders.copy(),
            "archive_existing_messages": self.archive_existing_messages,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Mailbox:
        mailbox = cls(
            address=str(value.get("address", "")),
            folders=value.get("folders", []),
            archive_existing_messages=bool(value.get("archive_existing_messages", False)),
            enabled=bool(value.get("enabled", True)),
        )
        mailbox.validate()
        return mailbox


@dataclass(slots=True)
class Account:
    label: str
    host: str = ""
    username: str = ""
    provider: MailProvider = MailProvider.GENERIC_IMAP
    auth_mode: AuthMode = AuthMode.PASSWORD
    port: int = 993
    use_ssl: bool = True
    client_id: str = ""
    tenant_id: str = ""
    poll_minutes: int | None = None
    enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid4()))
    mailboxes: list[Mailbox] = field(default_factory=list)
    legacy_source: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.mailboxes and self.username.strip():
            self.mailboxes = [Mailbox(self.username)]

    def validate(self, *, require_user_oauth_client: bool = True) -> None:
        if not self.label.strip():
            raise ValueError("Enter a name for the email account.")
        if self.auth_mode != AuthMode.OAUTH_APPLICATION and not self.username.strip():
            raise ValueError("Enter the sign-in email address or username.")
        if self.provider == MailProvider.GENERIC_IMAP:
            if self.auth_mode not in {AuthMode.PASSWORD, AuthMode.OAUTH_USER}:
                raise ValueError(
                    "Generic IMAP supports password or delegated OAuth authentication."
                )
            if not self.host.strip():
                raise ValueError("Enter the IMAP server.")
            if not 1 <= self.port <= 65535:
                raise ValueError("The IMAP port must be between 1 and 65535.")
            if self.auth_mode == AuthMode.OAUTH_USER and (
                self.host.strip().casefold() != MICROSOFT_IMAP_HOST
                or self.port != MICROSOFT_IMAP_PORT
                or not self.use_ssl
            ):
                raise ValueError(
                    "Microsoft OAuth IMAP requires outlook.office365.com on port 993 with "
                    "direct SSL/TLS."
                )
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
        if (
            (
                self.provider == MailProvider.MICROSOFT_GRAPH
                or (
                    self.provider == MailProvider.GENERIC_IMAP
                    and self.auth_mode == AuthMode.OAUTH_USER
                )
            )
            and self.tenant_id.strip()
            and (
                any(character in self.tenant_id for character in "/?#[]@")
                or any(character.isspace() for character in self.tenant_id)
            )
        ):
            raise ValueError("Enter a valid Microsoft tenant ID or audience.")
        if self.poll_minutes is not None and not 1 <= self.poll_minutes <= 1440:
            raise ValueError("The polling interval must be between 1 and 1440 minutes.")
        if not self.mailboxes:
            raise ValueError("Configure at least one mailbox for this email account.")
        addresses = set()
        for mailbox in self.mailboxes:
            mailbox.validate()
            address = mailbox.address.strip().casefold()
            if address in addresses:
                raise ValueError("Each mailbox address may be configured only once per account.")
            addresses.add(address)
            if mailbox.address.strip().casefold() != self.username.strip().casefold() and (
                (self.provider == MailProvider.GENERIC_IMAP and self.auth_mode == AuthMode.PASSWORD)
                or (
                    self.provider == MailProvider.GMAIL_API
                    and self.auth_mode == AuthMode.OAUTH_USER
                )
            ):
                raise ValueError(
                    "This sign-in method can read its own mailbox only. For multiple addresses, "
                    "use Microsoft delegated/application access or Google Workspace domain-wide delegation."
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider.value,
            "auth_mode": self.auth_mode.value,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "use_ssl": self.use_ssl,
            "client_id": self.client_id,
            "tenant_id": self.tenant_id,
            "poll_minutes": self.poll_minutes,
            "enabled": self.enabled,
            "mailboxes": [mailbox.to_dict() for mailbox in self.mailboxes],
            **({"legacy_source": self.legacy_source} if self.legacy_source else {}),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Account:
        if "mailboxes" in value and (
            not isinstance(value["mailboxes"], list) or not value["mailboxes"]
        ):
            raise ValueError("Configure at least one mailbox for this email account.")
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
            use_ssl=bool(value.get("use_ssl", True)),
            client_id=str(value.get("client_id", "")),
            tenant_id=str(value.get("tenant_id", "")),
            poll_minutes=(
                int(value["poll_minutes"]) if value.get("poll_minutes") not in {None, ""} else None
            ),
            enabled=bool(value.get("enabled", True)),
            mailboxes=(
                [Mailbox.from_dict(item) for item in value["mailboxes"]]
                if "mailboxes" in value
                else [
                    Mailbox(
                        str(value.get("username", "")),
                        folders=[str(value.get("folder", value.get("mailbox", "INBOX")))],
                        archive_existing_messages=bool(
                            value.get("archive_existing_messages", False)
                        ),
                    )
                ]
            ),
            legacy_source=(
                value.get("legacy_source")
                if "mailboxes" in value
                else {
                    "provider": str(value.get("provider", MailProvider.GENERIC_IMAP.value)),
                    "address": str(value.get("username", "")),
                    "host": str(value.get("host", "")),
                    "port": int(value.get("port", 993)),
                    "folder": str(value.get("folder", value.get("mailbox", "INBOX"))),
                }
            ),
        )
        # Keep user-OAuth accounts from older versions editable when their client
        # ID previously came from build-level configuration.
        account.validate(require_user_oauth_client=False)
        return account


def default_rule(*, destination: str = "") -> Rule:
    return Rule(
        name="All remaining emails",
        destination=destination,
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
    state_database_path: str = ""
    schema_version: int = SETTINGS_SCHEMA_VERSION

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
            "state_database_path": self.state_database_path,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Settings:
        if int(value.get("schema_version", 0)) > SETTINGS_SCHEMA_VERSION:
            raise ValueError(
                "These settings were written by a newer version of MailArchive. "
                "Install a newer version before opening them."
            )
        rules = [Rule.from_dict(item) for item in value.get("rules", [])]
        if not rules:
            # Preserve the implicit Inbox destination in older configurations.
            destination = "Inbox" if int(value.get("schema_version", 0)) < 7 else ""
            rules = [default_rule(destination=destination)]
        accounts = []
        for item in value.get("accounts", []):
            account_value = dict(item)
            account_value.setdefault(
                "archive_existing_messages",
                bool(value.get("archive_existing_messages", False)),
            )
            accounts.append(Account.from_dict(account_value))
        settings = cls(
            schema_version=SETTINGS_SCHEMA_VERSION,
            archive_root=str(value.get("archive_root") or cls.defaults().archive_root),
            accounts=accounts,
            rules=rules,
            start_at_login=bool(value.get("start_at_login", value.get("start_with_windows", True))),
            minimize_to_tray=bool(value.get("minimize_to_tray", True)),
            warn_on_error=bool(value.get("warn_on_error", True)),
            default_poll_minutes=int(value.get("default_poll_minutes", 5)),
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
