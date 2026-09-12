# Self-configuring Microsoft OAuth

This guide is for developers, maintainers, and self-hosters who want to use their own
Microsoft OAuth application with MailArchive. It applies to Microsoft Graph and to generic
IMAP with XOAUTH2.

Users of a published MailArchive package with a bundled Microsoft client ID do **not** need
to perform these steps. They only choose **Authorize** and sign in. One registration owned
by the MailArchive publisher serves all of those users.

## What Microsoft requires

A personal Outlook.com or Hotmail account is a Microsoft account, but it is not by itself a
Microsoft Entra directory. An OAuth application registration must have an owning Entra tenant;
there is no ownerless client ID.

No company, Microsoft 365 subscription, or purchased domain is required. For self-configuration,
however, the person who owns the registration must first create a free Azure account. This
provides an Entra tenant and an automatically assigned `onmicrosoft.com` domain in which the
application can be registered.

Microsoft may require a telephone number, payment card, or other identity verification during
Azure registration. Microsoft Entra ID Free itself has no charge, but other Azure services can
incur charges if they are activated separately. See Microsoft's documentation for
[Microsoft Entra ID Free](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/microsoft-entra-id-free)
and the [Azure free account](https://azure.microsoft.com/free/).

The ownership chain is:

```text
Personal Microsoft account
        -> free Azure account
        -> Microsoft Entra tenant
        -> public-client app registration
        -> application (client) ID used by MailArchive
```

## 1. Create the free Azure account and tenant

1. Open the [Azure free account](https://azure.microsoft.com/free/) page and sign in with the
   personal Microsoft account that should own the MailArchive registration.
2. Complete Microsoft's account and identity verification.
3. Open the [Microsoft Entra admin center](https://entra.microsoft.com/) with the same account.
4. Use the directory created for the Azure account. If Microsoft asks for tenant details, choose
   an organization name such as `MailArchive` and an available initial domain such as
   `mailarchive-example.onmicrosoft.com`. The organization name is the tenant's display name; it
   does not require a legally registered company or a custom domain.

Microsoft documents the current process in
[Create a new tenant in Microsoft Entra ID](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-create-new-tenant).

### The `Microsoft Services` error

Signing in to the Entra admin center with only a personal Microsoft account can select a special
directory named `Microsoft Services`. That directory belongs to Microsoft and cannot contain
your MailArchive registration. The portal can then show an error similar to:

```text
Selected user account does not exist in tenant 'Microsoft Services' ...
The account needs to be added as an external user in the tenant first.
```

Do not try to add the account as a guest to `Microsoft Services`. Create the Azure account and
use the resulting directory instead. If a tenant already exists, use the directory selector in
the Entra portal to switch away from `Microsoft Services` to that tenant. Microsoft describes
this personal-account behavior in its
[AADSTS50020 troubleshooting guide](https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/error-code-AADSTS50020-user-account-identity-provider-does-not-exist).

## 2. Register MailArchive as a public client

In the Entra admin center:

1. Open **Identity > Applications > App registrations** and choose **New registration**.
2. Enter a name such as `MailArchive`.
3. Select **Accounts in any organizational directory and personal Microsoft accounts**. This
   allows both Microsoft 365 and Outlook.com/Hotmail accounts to sign in.
4. Create the registration, then copy its **Application (client) ID**. Do not copy the object ID
   or directory/tenant ID in its place.
5. Under **Authentication**, add the **Mobile and desktop applications** platform and the
   redirect URI `http://localhost`.
6. Enable public-client flows.
7. Under **API permissions**, add the delegated permissions needed for the modes that the build
   supports:

   - **Office 365 Exchange Online**: `IMAP.AccessAsUser.All` for generic IMAP with XOAUTH2.
   - **Microsoft Graph**: `Mail.Read` for Microsoft Graph delegated access.

Choose **Delegated permissions**, not application permissions. A desktop public client does not
use a client secret, so do not create or distribute one for these interactive sign-in modes.
Microsoft's [app-registration guide](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
explains the portal fields, and the Exchange documentation describes
[OAuth for IMAP and XOAUTH2](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth).

## 3. Give the client ID to MailArchive

For a development or self-hosted run, supply the public application ID through the environment.
On Linux:

```bash
MAILARCHIVE_MICROSOFT_CLIENT_ID=11111111-2222-4333-8444-555555555555 python -m mailarchive
```

In PowerShell:

```powershell
$env:MAILARCHIVE_MICROSOFT_CLIENT_ID = "11111111-2222-4333-8444-555555555555"
python -m mailarchive
```

Replace the example UUID with the **Application (client) ID** from the registration. The client
ID is a public identifier and may be stored in normal application configuration. OAuth tokens
remain in the operating system's protected credential store.

Maintainers producing distributable packages instead set
`BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID` in `src/mailarchive/provider_config.py`. The build scripts
reject a release without a valid bundled ID. End users of that package then do not set the
environment variable and do not need Azure accounts of their own.

## 4. Authorize the mailbox

Start MailArchive, add either a Microsoft Graph account or generic IMAP with
**Microsoft OAuth (XOAUTH2)**, save it, and choose **Authorize**. Sign in with the mailbox account
and approve the requested delegated access. MailArchive receives and renews tokens through the
browser-based OAuth flow; no access token, app password, or client secret is pasted into the
account form.

For Outlook.com and Hotmail IMAP, also enable the account setting that permits devices and apps
to use IMAP. That setting enables the protocol, while OAuth/XOAUTH2 supplies the authentication.

## Common problems

- **The portal still shows `Microsoft Services`:** switch to the tenant created with the Azure
  account. The Microsoft-owned directory cannot host the registration.
- **Personal Microsoft accounts cannot sign in:** verify that the app registration supports
  both organizational directories and personal Microsoft accounts.
- **MailArchive says Microsoft sign-in is not configured:** verify that the environment variable
  contains the application/client ID as a nonzero UUID, or use a build with a bundled ID.
- **IMAP authorization succeeds but access is rejected:** verify the delegated
  `IMAP.AccessAsUser.All` permission and the mailbox's IMAP setting.
- **The portal asks for a client secret:** the wrong application type or authentication flow was
  selected. Interactive desktop sign-in uses a public client and no secret.
