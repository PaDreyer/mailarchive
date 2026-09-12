# Email provider authentication

MailArchive keeps provider selection separate from authentication selection. User access
and unattended application access are different security models and require different
provider-side configuration.

## Supported combinations

| Provider | Authentication | Mailbox scope | Interactive sign-in |
| --- | --- | --- | --- |
| Generic IMAP | Password or app password | Configured IMAP user | No |
| Generic IMAP | Microsoft OAuth (XOAUTH2) | Own and permitted shared mailboxes | Yes |
| Gmail API | Google OAuth user sign-in | Signed-in Google account | Yes |
| Gmail API | Google Workspace domain-wide delegation | Multiple impersonated Workspace users | No |
| Microsoft Graph | Microsoft delegated user access | Own and permitted shared mailboxes | Yes |
| Microsoft Graph | Microsoft application access | Multiple mailboxes permitted for the application | No |

## Generic IMAP

Choose **Generic IMAP** and enter the server, port, sign-in username, and password or app
password. Add the login's mailbox under **Mailboxes** and select folders or leave the selection
blank for all folders. Direct TLS is enabled by default. When it is disabled, MailArchive upgrades the
connection with STARTTLS before authentication.

Gmail can also be used through this provider with `imap.gmail.com`, port `993`, direct TLS,
and an app password when the Google account and its administrator permit app passwords.
The [Gmail app-password setup guide](GMAIL_APP_PASSWORD_SETUP.md) walks through enabling
2-Step Verification, creating the password, and entering the MailArchive settings.

### Microsoft OAuth over IMAP

For Outlook.com, Hotmail, and Microsoft 365 mailboxes that require modern authentication,
choose **Microsoft OAuth (XOAUTH2)** and enter the sign-in identity. Under **Mailboxes**, add
the own or shared mailbox addresses for which this user has access. MailArchive fixes the
connection to `outlook.office365.com`, port `993`, with direct TLS and does not send the Microsoft
bearer token to user-configured IMAP hosts. Save the account, select it, choose **Authorize**,
and complete sign-in in the system browser.

MailArchive requests the delegated scope
`https://outlook.office.com/IMAP.AccessAsUser.All`, stores the serialized MSAL token cache in
the operating-system credential store, renews access tokens silently, and sends each transient
token with SASL `AUTHENTICATE XOAUTH2`. Users never paste an access token or client secret.
The Outlook.com setting that permits devices and apps to use IMAP must also be enabled.

Microsoft documents the required [IMAP OAuth scopes and XOAUTH2 exchange](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth).

## Gmail API with Google OAuth user sign-in

This mode works with personal Gmail and Google Workspace accounts. It uses Google's OAuth
flow for desktop applications and requests only the read-only Gmail scope.

1. In MailArchive, choose **Gmail (Google API)** and
   **Google OAuth - user sign-in**.
2. In Google Cloud, enable the Gmail API, configure the OAuth consent screen, and create an
   OAuth client whose application type is **Desktop app**. Add the Google account as a test
   user while the consent screen remains in testing.
3. Enter the sign-in address and desktop client's OAuth client ID in MailArchive. Add that
   address under **Mailboxes**, with label IDs one per line or blank for all mail. If Google supplied a client secret, enter it as well; otherwise leave that
   field blank.
4. Save the account, select it, choose **Authorize**, and complete the Google sign-in and
   consent in the system browser.

MailArchive performs the authorization-code flow with PKCE and a temporary loopback callback.
It stores the optional client secret and resulting refresh-token data in the operating-system
credential store. The client ID is stored with the non-secret account settings. Google
documents the
[desktop OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app)
and the available [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

## Gmail API with Google Workspace application access

This unattended mode is available only for a managed Google Workspace domain. Google does
not provide general app-only Gmail access to personal Gmail mailboxes. Instead, a Workspace
super administrator grants a service account domain-wide authority, and MailArchive uses
that account to impersonate the configured mailbox user.

1. Create or select a Google Cloud project and enable the Gmail API.
2. Create a service account, enable Google Workspace domain-wide delegation for it, and
   create a JSON key.
3. In the Google Workspace Admin console, add the service account's numeric OAuth client ID
   under domain-wide delegation and grant exactly this scope:

   ```text
   https://www.googleapis.com/auth/gmail.readonly
   ```

4. In MailArchive, choose **Gmail (Google API)** and
   **Google Workspace - domain-wide delegation**.
5. Select the service-account JSON key file. Under **Mailboxes**, add each Workspace
   user address to impersonate. Each target obtains a token with its own delegation subject
   from the same stored key. An OAuth client ID, tenant ID, and interactive authorization are not used.
6. Save the account and choose **Archive now** to verify the configuration.

MailArchive validates the selected JSON, retains only the fields needed for authentication,
and stores them in the operating-system credential store. It stores neither the selected
file path nor the key in `config.json`. MailArchive does not delete the original key file;
handle or remove that file according to the organization's key-management policy.

Google documents the required
[service-account and domain-wide delegation flow](https://developers.google.com/identity/protocols/oauth2/service-account).
Domain-wide delegation is powerful: grant only the read-only Gmail scope and restrict who
can administer or use the service-account key.

## Microsoft Graph with delegated user access

This mode uses an interactive authorization-code flow with PKCE:

1. In MailArchive, choose **Microsoft OAuth - delegated user access** and enter the sign-in
   identity. Under **Mailboxes**, add the own and permitted shared addresses. Folder IDs or
   well-known names can be selected one per line; blank reads all folders recursively.
2. Optionally enter a tenant ID or audience. Leave it blank to use `common`, or enter a
   directory tenant ID, `organizations`, or `consumers` when that matches the app
   registration.
3. Save the account, choose **Authorize**, and complete sign-in and consent in the system
   browser.

For additional addresses MailArchive requests `Mail.Read.Shared` alongside `Mail.Read`.
Reauthorize after adding the first shared mailbox to grant the additional scope. Sharing or
Exchange mailbox delegation must already permit the signed-in user to read that target.
Microsoft documents [shared/delegated message access](https://learn.microsoft.com/en-us/graph/outlook-share-messages-folders).

MailArchive stores the resulting MSAL token cache in the operating-system credential store.
The bundled client ID is a public application identifier; a desktop public client does not use
a client secret. Microsoft documents [public client applications](https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-applications),
[interactive MSAL Python token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens),
and [desktop app configuration](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration).

## Microsoft Graph with application access

For unattended access, create a Microsoft Entra app registration, add the Microsoft Graph
application permission `Mail.Read`, grant administrator consent, and create a client secret.
Choose **Microsoft OAuth - application access** and enter the tenant ID, client ID, and client
secret. Under **Mailboxes**, add each permitted mailbox address. The application credentials
are stored once and shared across those targets. A tenant-specific ID is required; `common` is not valid for
application access. Save the account and choose **Archive now** to test it.

Microsoft documents
[delegated and app-only access](https://learn.microsoft.com/en-us/graph/auth/auth-concepts)
and the [client-credentials flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

## Bundled Microsoft public-client registration

A production release uses one MailArchive-owned Entra public-client registration for delegated
Graph and IMAP sign-in. Maintainers must configure it with:

- account types covering organizational directories and personal Microsoft accounts;
- **Mobile and desktop applications** with redirect URI `http://localhost`;
- public-client flows enabled;
- delegated Microsoft Graph permissions `Mail.Read` and `Mail.Read.Shared`;
- delegated Office 365 Exchange Online permission `IMAP.AccessAsUser.All`;
- no client secret.

The production application ID belongs in
`src/mailarchive/provider_config.py` as `BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID`. Package builds
reject a missing, invalid, or zero UUID. Developers can temporarily set
`MAILARCHIVE_MICROSOFT_CLIENT_ID` at runtime; this override is deliberately not accepted by the
release gate.

Developers and self-hosters who only have a personal Outlook.com or Hotmail account must first
create a free Azure account to obtain the Entra tenant that owns their registration. They do not
need a company, Microsoft 365 subscription, or purchased domain. The
[Microsoft OAuth self-configuration guide](MICROSOFT_OAUTH_SETUP.md) explains the complete setup
and the misleading `Microsoft Services` tenant error.

## Stored credentials

Passwords, OAuth client secrets, refresh tokens, token caches, and imported Google service
account keys are stored in Windows Credential Manager or, on Linux, through Secret Service,
GNOME Keyring, or KWallet. MailArchive deliberately does not fall back to an unencrypted
credential file. On Windows, large OAuth caches are split across multiple protected Credential
Manager entries because Windows limits the size of each individual entry.
