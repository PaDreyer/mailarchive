# ADR 0001: Microsoft OAuth for generic IMAP

## Status

Accepted for implementation.

## Context

Outlook.com and Exchange Online can expose IMAP while rejecting password-based authentication.
Their supported delegated IMAP authentication uses a Microsoft OAuth access token with SASL
XOAUTH2. Access tokens expire and therefore cannot be treated as user-entered account secrets.

MailArchive already obtains delegated Microsoft Graph tokens with MSAL, persists the serialized
token cache in the operating-system credential store, and renews tokens silently. The existing
generic IMAP source supports only password authentication.

## Decision

- Generic IMAP supports either password authentication or delegated Microsoft OAuth with
  `https://outlook.office.com/IMAP.AccessAsUser.All`.
- OAuth IMAP uses `AUTHENTICATE XOAUTH2`; `imaplib` performs the base64 transfer encoding.
- Microsoft bearer tokens are sent only to `outlook.office365.com:993` over direct TLS; OAuth
  mode does not accept an arbitrary IMAP endpoint.
- The interactive and background paths share the same scope and client-ID resolution.
- A production release bundles one MailArchive public-client ID. Per-account client IDs remain
  compatible only as development or legacy overrides. No client secret is used for delegated
  desktop sign-in.
- Tokens and MSAL caches are never stored in normal configuration. Only the serialized cache is
  persisted in the operating-system credential store.
- Switching an account's provider, authentication mode, endpoint, identity, audience, or client
  override invalidates credentials bound to the previous configuration.
- Password IMAP behavior remains available for servers that still support it.

## Release gate

A production Entra registration must support organizational and personal Microsoft accounts,
use the `http://localhost` desktop redirect, enable public-client flows, and grant delegated
`Mail.Read` and `IMAP.AccessAsUser.All`. Release builds must fail when the bundled client ID is
missing or is not a valid UUID.

## Consequences

Outlook users authorize through the system browser and do not paste tokens or create client
secrets. Development builds may use an explicit client-ID override until the production
registration exists. Older MailArchive binaries cannot use accounts saved as generic IMAP with
OAuth and must be upgraded or the account must be switched back to password authentication.
