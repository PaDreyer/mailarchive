# Email provider authentication

MailArchive keeps provider selection separate from authentication selection. User access
and unattended application access are different security models and require different
provider-side configuration.

## Supported combinations

| Provider | Authentication | Mailbox scope | Interactive sign-in |
| --- | --- | --- | --- |
| Generic IMAP | Password or app password | Configured IMAP user | No |
| Gmail API | Google OAuth user sign-in | Signed-in Google account | Yes |
| Gmail API | Google Workspace domain-wide delegation | Impersonated Workspace user | No |
| Microsoft Graph | Microsoft delegated user access | Signed-in Microsoft user | Yes |
| Microsoft Graph | Microsoft application access | Mailbox selected by the application | No |

## Generic IMAP

Choose **Generic IMAP** and enter the server, port, username, folder, and password or app
password. Direct TLS is enabled by default. When it is disabled, MailArchive upgrades the
connection with STARTTLS before authentication.

Gmail can also be used through this provider with `imap.gmail.com`, port `993`, direct TLS,
and an app password when the Google account and its administrator permit app passwords.

## Gmail API with Google OAuth user sign-in

This mode works with personal Gmail and Google Workspace accounts. It uses Google's OAuth
flow for desktop applications and requests only the read-only Gmail scope.

1. In MailArchive, choose **Gmail (Google API)** and
   **Google OAuth - user sign-in**.
2. In Google Cloud, enable the Gmail API, configure the OAuth consent screen, and create an
   OAuth client whose application type is **Desktop app**. Add the Google account as a test
   user while the consent screen remains in testing.
3. Enter the mailbox address, folder or label, and the desktop client's OAuth client ID in
   MailArchive. If Google supplied a client secret, enter it as well; otherwise leave that
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
5. Enter the Workspace mailbox address to impersonate and select the service-account JSON
   key file. An OAuth client ID, tenant ID, and interactive authorization are not used.
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

1. Create a Microsoft Entra app registration and select the account types the user should
   be allowed to sign in with.
2. Add **Mobile and desktop applications** as a platform, register `http://localhost` as a
   redirect URI, enable public-client flows, and add the delegated Microsoft Graph
   permission `Mail.Read`.
3. In MailArchive, choose **Microsoft OAuth - delegated user access** and enter the mailbox
   address, folder, and application client ID.
4. Optionally enter a tenant ID or audience. Leave it blank to use `common`, or enter a
   directory tenant ID, `organizations`, or `consumers` when that matches the app
   registration.
5. Save the account, choose **Authorize**, and complete sign-in and consent in the system
   browser.

MailArchive stores the resulting MSAL token cache in the operating-system credential store.
The client ID is a public application identifier; a desktop public client does not use a
client secret. Microsoft documents [public client applications](https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-applications),
[interactive MSAL Python token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens),
and [desktop app configuration](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration).

## Microsoft Graph with application access

For unattended access, create a Microsoft Entra app registration, add the Microsoft Graph
application permission `Mail.Read`, grant administrator consent, and create a client secret.
Choose **Microsoft OAuth - application access** and enter the mailbox address, tenant ID,
client ID, and client secret. A tenant-specific ID is required; `common` is not valid for
application access. Save the account and choose **Archive now** to test it.

Microsoft documents
[delegated and app-only access](https://learn.microsoft.com/en-us/graph/auth/auth-concepts)
and the [client-credentials flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

## Stored credentials

Passwords, OAuth client secrets, refresh tokens, token caches, and imported Google service
account keys are stored in Windows Credential Manager or, on Linux, through Secret Service,
GNOME Keyring, or KWallet. MailArchive deliberately does not fall back to an unencrypted
credential file.
