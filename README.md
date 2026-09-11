# MailArchive

<img src="assets/mailarchive.svg" alt="MailArchive logo" width="128">

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
- Scans existing messages on the first run, not only newly received mail
- Avoids duplicate archives with a provider-independent SQLite processing index
- Displays connection and storage problems in the UI, activity log, and tray notifications
- Stores passwords, OAuth tokens, client secrets, and service-account keys in the operating
  system's protected credential store

## Supported accounts

| Provider | Authentication | Configuration in MailArchive |
| --- | --- | --- |
| Generic IMAP | Password or app password | Server, port, mailbox, folder, and password |
| Gmail API | OAuth user sign-in | Mailbox, label, Google Desktop OAuth client ID, and optional client secret |
| Gmail API | Workspace domain-wide delegation | Mailbox to impersonate and service-account JSON key |
| Microsoft Graph | Delegated user sign-in | Mailbox, folder, Entra application client ID, and optional tenant/audience |
| Microsoft Graph | Application access | Mailbox, tenant ID, client ID, and client secret |

OAuth user sign-in requires an appropriate application registration from Google Cloud or
Microsoft Entra. The credentials are entered on the corresponding account; MailArchive does
not require build-time provider configuration. See the
[authentication guide](docs/AUTHENTICATION.md) for exact registration and permission steps.

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
   for that combination.
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

The first check searches every message in the configured IMAP folder, Gmail label, or
Microsoft folder, including older messages. After a matching message is archived
successfully, its provider message ID is recorded in `archive-state.sqlite3` (or the custom
SQLite file selected under **Settings > Advanced**). Later checks skip known messages before
downloading their MIME content.

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
credential file. OAuth client IDs are public identifiers and remain in the normal account
configuration.

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

For development without native tray integration, run `./scripts/setup-dev.sh` without the
option. The already-running tray process must be quit before starting updated code because
MailArchive permits only one instance per user session.

## Tests

Activate `.venv`, then run the standard-library test suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover authentication configuration, credential filtering, provider clients,
message parsing, rule evaluation, all storage modes, safe paths and filenames, duplicate
protection, polling behavior, migrations, Linux autostart, single-instance handling, and
error reporting. They use local fakes and do not access real mail accounts.

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

The script runs the tests, creates a PyInstaller `onedir` application, and packages it as
`dist\installer\MailArchive-Setup-<version>-x64.exe` with Inno Setup.

### Linux AppImage

The recommended Linux build runs in the supplied Ubuntu 22.04 container:

```bash
./scripts/build-linux-container.sh
```

The result is `dist/MailArchive-<version>-x86_64.AppImage`. Docker is required on the build
host. A direct native build is also available through `scripts/build-linux.sh` when the
required system libraries and `appimagetool` are already installed.

## Project documentation

- [Authentication](docs/AUTHENTICATION.md) — provider modes, OAuth registrations,
  permissions, and credential storage
- [Release process](docs/RELEASE.md) — versioning, tag-triggered CI, artifacts, checksums,
  and publication verification

## Current limitations

- Google application access requires a managed Workspace domain, domain-wide delegation,
  and a service-account key; it is not available for personal Gmail accounts.
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
