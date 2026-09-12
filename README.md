# <img src="assets/mailarchive.svg" alt="" width="40"> MailArchive

MailArchive is a local email archiver for Windows and Linux. It runs in the notification
area, checks configured mailboxes on a schedule, applies simple rules, and saves matching
messages and attachments to folders on the computer.

It supports generic IMAP, the Gmail API, and Microsoft Graph. MailArchive reads messages
without marking them as read and never deletes or moves anything on the mail server.

> [!IMPORTANT]
> MailArchive is currently an early MVP. Verify archived output before relying on it, keep
> independent backups, and do not treat it as an immutable or regulatory-compliance archive.

## Highlights

- Runs automatically in the Windows notification area or Linux system tray
- Supports multiple IMAP, Gmail, Outlook, and Microsoft 365 accounts
- Provides interactive OAuth sign-in for Google and Microsoft accounts
- Supports unattended Microsoft application access and Google Workspace domain-wide
  delegation
- Applies easy top-to-bottom rules for sender, recipient, subject, body, or attachments
- Saves the original `.eml`, extracted attachments, or both
- Uses a global polling interval with an optional per-account override
- Lets each account archive existing messages or start with newly received mail
- Avoids duplicate archives with a provider-independent SQLite processing index
- Displays connection and storage problems in the UI, activity log, and tray notifications
- Stores passwords, OAuth tokens, client secrets, and service-account keys in the operating
  system's protected credential store

## Supported accounts

| Provider | Authentication | Configuration in MailArchive |
| --- | --- | --- |
| Generic IMAP | Password or app password | Server, port, mailbox, folder, and password |
| Generic IMAP | Microsoft OAuth (XOAUTH2) | Mailbox, folder, and optional tenant/audience; the Microsoft endpoint is fixed securely |
| Gmail API | OAuth user sign-in | Mailbox, label, Google Desktop OAuth client ID, and optional client secret |
| Gmail API | Workspace domain-wide delegation | Mailbox to impersonate and service-account JSON key |
| Microsoft Graph | Delegated user sign-in | Mailbox, folder, and optional tenant/audience |
| Microsoft Graph | Application access | Mailbox, tenant ID, client ID, and client secret |

Google user sign-in requires the account owner's Google Desktop OAuth registration. Microsoft
delegated sign-in uses one public-client registration bundled with MailArchive, so users do not
enter a client ID, client secret, or token. See the [authentication guide](docs/AUTHENTICATION.md)
for the exact scopes and release configuration.

## Installation

Download the package for your platform from the [latest GitHub release](../../releases/latest).
Release packages are self-contained and do not require a separate Python installation.

### Windows

1. Download `MailArchive-Setup-<version>-x64.exe`.
2. Run the installer and start MailArchive from the Start menu.
3. Open MailArchive from its notification-area icon after closing the main window.

The installer is per-user and does not require administrator rights. Current packages are
not code-signed, so Windows may display an unknown-publisher warning.

### Linux

1. Download `MailArchive-<version>-x86_64.AppImage`.
2. Make it executable and start it:

   ```bash
   chmod +x MailArchive-*.AppImage
   ./MailArchive-*.AppImage
   ```

MailArchive needs a compatible StatusNotifier/AppIndicator host for its tray icon. KDE Plasma
normally provides one. On Debian or Ubuntu with GNOME, install the AppIndicator extension:

```bash
sudo apt install gnome-shell-extension-appindicator
```

Log out and back in after installation, or enable the extension through GNOME Extensions.
Without a compatible tray host, MailArchive remains usable as a normal window and will not
hide itself when closed.

## First run

1. Open **Accounts**, select a provider and authentication mode, and enter the fields shown
   for that combination. Choose per account whether messages already in its mailbox should
   be archived.
2. Save the account. For Google or Microsoft user access, select it and choose
   **Authorize** to complete sign-in in the system browser.
3. Open **Rules** and define where matching mail should be stored. Rules are evaluated from
   top to bottom; the first match wins.
4. Open **Settings** to choose the archive directory, polling interval, startup behavior,
   and warning behavior. Under **Advanced**, you can also choose where the SQLite processing
   database is stored.
5. Choose **Archive now** to request an immediate check. Scheduled checks run automatically
   while MailArchive is active.

The built-in **All remaining emails** rule is a useful final catch-all rule. With
**Attachments only**, a matching message without attachments creates no file but is still
recorded as processed.

## Processing behavior

MailArchive requests a background check when it starts and then checks every enabled account
at its configured interval. **Archive now** adds an immediate check; it is not required for
normal background operation.

By default, the first successful check of a new account records the messages already in the
configured IMAP folder, Gmail label, or Microsoft folder without downloading or archiving
them. Later checks archive only messages that were not present at that starting point. Enable
**Archive messages that already exist in this mailbox** when adding or editing an account to
include older messages for that account. Enabling it later also makes messages skipped at the
starting point eligible for archiving without affecting other accounts.

After a matching message is archived successfully, its provider message ID is recorded in
`archive-state.sqlite3` (or the custom SQLite file selected under **Settings > Advanced**).
Later checks skip known messages before downloading their MIME content.

Failed messages and messages without a matching rule are not recorded as complete and are
retried. This allows newly created or changed rules to match existing mail. For IMAP, the
processing namespace includes the server's `UIDVALIDITY`; Gmail and Microsoft namespaces
include the selected label or folder.

## Security and local data

MailArchive keeps account settings and its processing index in these locations:

| Platform | Application data |
| --- | --- |
| Windows | `%LOCALAPPDATA%\MailArchive` |
| Linux | `$XDG_DATA_HOME/mailarchive` or `~/.local/share/mailarchive` |

The processing index can be moved to another SQLite file under **Settings > Advanced**.
MailArchive copies the existing processing history to the selected file and keeps the old
database as a fallback.

Passwords, OAuth client secrets, refresh tokens, MSAL token caches, and imported Google
service-account keys are stored in Windows Credential Manager or, on Linux, through Secret
Service, GNOME Keyring, or KWallet. MailArchive does not fall back to an unencrypted
credential file. OAuth client IDs are public identifiers: Google and legacy/custom per-account
IDs remain in normal account configuration, while MailArchive's Microsoft ID is bundled with
the application.

Archived email and attachment files are ordinary files in the selected archive directory.
MailArchive does not encrypt them; use filesystem permissions, full-disk encryption, and
backups appropriate for the sensitivity of the mailbox.

## Development

Development uses one project-local virtual environment named `.venv`. The setup scripts
install MailArchive in editable mode, so a restart is enough after ordinary Python source
changes; rebuilding is not required.

### Windows development setup

Requirements: Windows 10 or 11, Python 3.12 with Tcl/Tk, and PowerShell.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup-dev.ps1
.\.venv\Scripts\Activate.ps1
python -m mailarchive
```

### Linux development setup

Install the development packages on Debian or Ubuntu:

```bash
sudo apt install \
  python3-venv python3-tk pkg-config \
  libcairo2-dev libgirepository1.0-dev \
  gir1.2-ayatanaappindicator3-0.1
```

Create the venv with tray support and start the application:

```bash
./scripts/setup-dev.sh --with-appindicator
source .venv/bin/activate
python -m mailarchive
```

Until a production Microsoft client ID is bundled, development runs can supply a public-client
registration without changing account data:

```bash
MAILARCHIVE_MICROSOFT_CLIENT_ID=00000000-0000-4000-8000-000000000000 python -m mailarchive
```

Use a real nonzero application ID in place of the example UUID. This is a public identifier, not
a client secret.

For development without native tray integration, run `./scripts/setup-dev.sh` without the
option. The already-running tray process must be quit before starting updated code because
MailArchive permits only one instance per user session.

## Tests

Activate `.venv`, install the test dependency, then run the suite with branch coverage:

```bash
python -m pip install -e ".[test]"
python -m coverage run -m unittest discover -s tests -v
python -m coverage report
```

Install the optional quality tools and run the same lint and formatting checks as CI:

```bash
python -m pip install -e ".[quality]"
python -m ruff check src tests
python -m ruff format --check src tests
```

The tests cover authentication configuration, credential filtering, provider clients,
message parsing, rule evaluation, all storage modes, safe paths and filenames, duplicate
protection, polling behavior, migrations, Linux autostart, single-instance handling, and
error reporting. They use local fakes and do not access real mail accounts. Pull requests
and branch pushes enforce linting, formatting, and at least 80% branch-aware coverage on
Python 3.10 through 3.14; an additional Windows job runs the suite with Python 3.12.

## Building packages

PyInstaller packages must be built on their target operating system. Build dependencies are
installed into a temporary venv and removed after the build; the project-local `.venv` is
not reused for release packaging.

### Windows installer

Requirements: Windows 10 or 11, Python 3.12, PowerShell, and Inno Setup 6 or 7.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build-windows.ps1
```

The script runs the tests, creates and smoke-tests a PyInstaller `onedir` application, and
packages it as `dist\installer\MailArchive-Setup-<version>-x64.exe` with Inno Setup.

### Linux AppImage

The recommended Linux build runs in the supplied Ubuntu 22.04 container:

```bash
./scripts/build-linux-container.sh
```

The build smoke-tests the packaged executable before assembling
`dist/MailArchive-<version>-x86_64.AppImage`. Docker is required on the build host. A direct
native build is also available through `scripts/build-linux.sh` when the required system
libraries and `appimagetool` are already installed.

## Project documentation

- [Architecture](docs/ARCHITECTURE.md) — component boundaries, state guarantees, and change rules
- [Authentication](docs/AUTHENTICATION.md) — provider modes, OAuth registrations,
  permissions, and credential storage
- [Microsoft OAuth self-configuration](docs/MICROSOFT_OAUTH_SETUP.md) — creating the free Azure
  account, Entra tenant, and public-client ID needed for a custom setup
- [Release process](docs/RELEASE.md) — versioning, tag-triggered CI, artifacts, checksums,
  and publication verification

## Current limitations

- Google application access requires a managed Workspace domain, domain-wide delegation,
  and a service-account key; it is not available for personal Gmail accounts.
- Microsoft sign-in in development requires a public-client ID through
  `MAILARCHIVE_MICROSOFT_CLIENT_ID` until the production registration is bundled.
- Each account watches one folder or label, defaulting to `INBOX` or `inbox`.
- Provider APIs enumerate message IDs on every check; provider-native delta synchronization
  is not implemented yet.
- The UI supports one condition per rule, although the internal rule model supports multiple
  conditions.
- MailArchive does not delete, move, or mark server-side messages as read.
- Packages are not code-signed, and there is no automatic update mechanism yet.
- MailArchive runs in the signed-in user's desktop session, not as a Windows service or
  systemd system service. A system service cannot provide the same tray UI and desktop
  notifications.
