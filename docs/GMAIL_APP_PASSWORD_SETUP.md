# Setting up a Gmail app password

This guide connects a Gmail mailbox to MailArchive through **Generic IMAP** with an app
password. This setup does not require a Google Cloud project or an OAuth client ID.
The separate **Gmail (Google API)** provider uses
[Google OAuth](AUTHENTICATION.md#gmail-api-with-google-oauth-user-sign-in) instead.

## 1. Enable 2-Step Verification

Sign in to the Google account whose mailbox you want to archive. In your
[Google Account security settings](https://myaccount.google.com/security), open
**2-Step Verification** and complete the setup if it is not already enabled.
Google requires this before you can create an app password.

## 2. Create the app password

1. Open [Google App passwords](https://myaccount.google.com/apppasswords).
2. Sign in again if prompted and check that the selected account is the intended mailbox.
3. Enter an app name such as `MailArchive` and choose **Create**.
4. Copy the generated 16-character app password. Google shows it only once; keep the page
   open until you have entered it in MailArchive.

Google documents the prerequisites and management of app passwords in
[Sign in with app passwords](https://support.google.com/accounts/answer/185833?hl=en).

## 3. Configure MailArchive

Open **Accounts**, choose **Add**, and enter these settings:

| Setting | Value |
| --- | --- |
| Provider | **Generic IMAP** |
| Authentication | **Password** |
| Account name | A recognizable name, such as `My Gmail` |
| Server | `imap.gmail.com` |
| Port | `993` |
| Direct TLS | Enabled |
| Username / mailbox | Your full Gmail address, such as `your.name@gmail.com` |
| Folder | `INBOX` |
| Password | The generated app password, with no spaces between its characters |

Choose whether to archive existing messages, then save the account. Password authentication
uses the stored credential directly; **Authorize** is only needed for OAuth accounts.
MailArchive stores the app password in the operating system's credential store.

Configure an archive rule and destination as described in the
[first-run instructions](../README.md#first-run), then choose **Archive now** and check the
activity log for connection errors. Only messages in the configured folder are considered.

Personal Gmail accounts already have IMAP enabled; there is no **Enable IMAP** switch to
turn on. Managed Google Workspace accounts may require their administrator to permit IMAP.
See Google's [Gmail email-client help](https://support.google.com/mail/answer/7126229?hl=en).

## Common problems

- **App passwords is unavailable:** check that 2-Step Verification is enabled. The option
  may be unavailable for organizational accounts, accounts using only security keys for
  2-Step Verification, or accounts enrolled in Advanced Protection. For a managed account,
  ask the administrator which authentication methods are allowed.
- **Authentication fails:** use the full mailbox address and the app password created for
  that same account. Enter all 16 characters without spaces, rather than the normal Google
  account password. Verify **Generic IMAP**, **Password**, `imap.gmail.com`, port `993`, and TLS.
- **Access stopped after changing the Google account password:** Google revokes app passwords
  when the account password changes. Create a new one and update the saved MailArchive account.
- **The app password was lost:** create a replacement; Google cannot show the old one again.

## Remove access

When you stop using MailArchive with this mailbox, open
[Google App passwords](https://myaccount.google.com/apppasswords) and remove its entry.
The saved app password will then stop working.
