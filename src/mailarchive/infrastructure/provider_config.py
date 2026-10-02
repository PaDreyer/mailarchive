from __future__ import annotations

import os
from uuid import UUID

# Public identifiers are safe to bundle. Set this to the production MailArchive
# registration before creating a release build.
BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID = "102cd6b1-e120-4411-96c6-8ae34a35006e"
MICROSOFT_CLIENT_ID_ENV = "MAILARCHIVE_MICROSOFT_CLIENT_ID"


class ProviderConfigurationError(RuntimeError):
    pass


def _validated_client_id(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError) as exc:
        raise ProviderConfigurationError(
            "Microsoft sign-in is not configured in this MailArchive build."
        ) from exc
    if parsed.int == 0:
        raise ProviderConfigurationError(
            "Microsoft sign-in is not configured in this MailArchive build."
        )
    return candidate


def require_microsoft_public_client_id() -> str:
    """Return the development override or bundled Microsoft public-client ID."""
    configured = os.environ.get(MICROSOFT_CLIENT_ID_ENV, "").strip()
    return _validated_client_id(configured or BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID)


def require_bundled_microsoft_public_client_id() -> str:
    """Validate that a distributable build contains its production client ID."""
    return _validated_client_id(BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID)


def main() -> None:
    try:
        require_bundled_microsoft_public_client_id()
    except ProviderConfigurationError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
