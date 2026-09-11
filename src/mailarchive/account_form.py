from __future__ import annotations

from mailarchive.models import AuthMode, MailProvider


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
