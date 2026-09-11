from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from mailarchive.models import Account, AuthMode, MailProvider

COMMON_ACCOUNT_FIELDS = frozenset(
    {
        "label",
        "provider",
        "auth",
        "username",
        "folder",
        "poll",
    }
)


def visible_account_fields(
    provider: MailProvider,
    auth_mode: AuthMode,
) -> frozenset[str]:
    """Return the exact account fields required by a provider/authentication pair."""
    if provider == MailProvider.GENERIC_IMAP:
        return COMMON_ACCOUNT_FIELDS | {"host", "port", "secret"}
    if provider == MailProvider.GMAIL_API:
        if auth_mode == AuthMode.OAUTH_APPLICATION:
            return COMMON_ACCOUNT_FIELDS | {"service_account_file"}
        return COMMON_ACCOUNT_FIELDS | {"client_id", "secret"}
    if provider == MailProvider.MICROSOFT_GRAPH:
        if auth_mode == AuthMode.OAUTH_APPLICATION:
            return COMMON_ACCOUNT_FIELDS | {"client_id", "tenant_id", "secret"}
        return COMMON_ACCOUNT_FIELDS | {"client_id", "tenant_id"}
    raise ValueError(f"Unsupported mail provider: {provider}")


@dataclass(frozen=True, slots=True)
class AccountFormValues:
    """UI-independent values collected by the account editor."""

    label: str
    provider: MailProvider
    auth_mode: AuthMode
    username: str
    host: str = ""
    port: str = "993"
    secret: str = ""
    folder: str = ""
    client_id: str = ""
    tenant_id: str = ""
    service_account_file: str = ""
    poll_minutes: str = ""
    use_ssl: bool = True
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AccountSubmission:
    """Validated account data and the credential mutation it requires."""

    account: Account
    credential_updates: dict[str, Any]
    replace_credentials: bool


def _parse_integer(value: str, field_name: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Enter a whole number for {field_name}.") from exc


def _credential_binding(account: Account) -> tuple[object, ...]:
    """Return fields that determine which remote identity credentials belong to."""
    common: tuple[object, ...] = (account.provider, account.auth_mode)
    if account.provider == MailProvider.GENERIC_IMAP:
        return common + (
            account.host.strip().casefold(),
            account.port,
            account.username.strip().casefold(),
        )
    if account.auth_mode == AuthMode.OAUTH_USER:
        return common + (
            account.client_id.strip(),
            account.tenant_id.strip().casefold(),
            account.username.strip().casefold(),
        )
    if account.provider == MailProvider.MICROSOFT_GRAPH:
        return common + (
            account.client_id.strip(),
            account.tenant_id.strip().casefold(),
        )
    return common


def build_account_submission(
    values: AccountFormValues,
    *,
    existing: Account | None = None,
    service_account_loader: Callable[[str], dict[str, Any]] | None = None,
) -> AccountSubmission:
    """Normalize and validate an account form without depending on Tk widgets."""
    provider = values.provider
    auth_mode = values.auth_mode
    is_imap = provider == MailProvider.GENERIC_IMAP
    is_google = provider == MailProvider.GMAIL_API
    is_google_application = is_google and auth_mode == AuthMode.OAUTH_APPLICATION
    is_google_user = is_google and auth_mode == AuthMode.OAUTH_USER
    is_microsoft = provider == MailProvider.MICROSOFT_GRAPH
    is_microsoft_application = is_microsoft and auth_mode == AuthMode.OAUTH_APPLICATION

    poll_text = values.poll_minutes.strip()
    account = Account(
        id=existing.id if existing else str(uuid4()),
        label=values.label.strip(),
        provider=provider,
        auth_mode=auth_mode,
        host=values.host.strip() if is_imap else "",
        port=_parse_integer(values.port, "the IMAP port") if is_imap else 993,
        username=values.username.strip(),
        folder=values.folder.strip() or ("inbox" if is_microsoft else "INBOX"),
        client_id=(
            values.client_id.strip()
            if (auth_mode == AuthMode.OAUTH_USER and (is_google or is_microsoft))
            or is_microsoft_application
            else ""
        ),
        tenant_id=(
            values.tenant_id.strip()
            if is_microsoft and auth_mode in {AuthMode.OAUTH_USER, AuthMode.OAUTH_APPLICATION}
            else ""
        ),
        poll_minutes=(_parse_integer(poll_text, "the polling interval") if poll_text else None),
        use_ssl=values.use_ssl if is_imap else True,
        enabled=values.enabled,
    )
    account.validate()

    binding_changed = existing is None or _credential_binding(existing) != _credential_binding(
        account
    )
    secret = values.secret
    service_account_file = values.service_account_file.strip()
    if (is_imap or is_microsoft_application) and binding_changed and not secret:
        raise ValueError("Enter the password or OAuth client secret.")
    if is_google_application and binding_changed and not service_account_file:
        raise ValueError("Select the Google service-account JSON key file.")

    credential_updates: dict[str, Any] = {}
    if secret and (is_imap or is_microsoft_application):
        credential_updates["password" if is_imap else "client_secret"] = secret
    if is_google_user and secret:
        credential_updates["oauth_client_secret"] = secret
    if is_google_application and service_account_file:
        if service_account_loader is None:
            raise RuntimeError("A Google service-account loader is required.")
        credential_updates["google_service_account"] = service_account_loader(service_account_file)

    explicitly_replaced = bool(
        credential_updates.keys()
        & {"client_secret", "google_service_account", "oauth_client_secret"}
    )
    return AccountSubmission(
        account=account,
        credential_updates=credential_updates,
        replace_credentials=existing is not None and (binding_changed or explicitly_replaced),
    )
