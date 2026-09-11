# MailArchive

MailArchive is a small Windows and Linux tray application that periodically checks email
accounts and stores messages locally according to simple rules. It supports generic IMAP,
the Gmail API, and Microsoft Graph for Outlook and Microsoft 365. It never deletes or moves
messages on the provider and fetches them without marking them as read.

## MVP features

- Multiple accounts using generic IMAP, Gmail, Outlook, or Microsoft 365
- Password, user OAuth, Microsoft application access, and Google Workspace domain-wide
  delegation
- A visible global polling interval with an optional override per account
- Simple rules for sender, recipient, subject, message body, and attachments
- Predictable top-to-bottom rule evaluation
- Storage as the original `.eml`, `.eml` plus extracted attachments, or attachments only
- A configurable local archive folder and destination subfolder per rule
- Background operation in the Windows notification area or Linux system tray
- Automatic startup when the current user signs in
- Tray and desktop warnings plus a readable activity log for connection and storage errors
- Provider-independent duplicate protection in a local SQLite database
- Passwords, OAuth client secrets, refresh tokens, token caches, and imported service-account
  keys in the protected operating-system credential store, never in `config.json`

## Using the application

1. Start `MailArchive.exe` on Windows or the MailArchive AppImage on Linux.
2. Open **Accounts**, add an account, and select its provider and authentication method.
   Interactive OAuth accounts must be saved and then connected with **Authorize**;
   application-access accounts authenticate automatically.
3. Open **Rules** and decide which emails go into which subfolders. Keep the built-in
   **All remaining emails** rule at the bottom of the list.
4. Open **Settings** to choose the archive folder, the default polling interval, and startup
   behavior. An account can optionally override that interval.
5. Select **Archive now** for the first check. MailArchive then continues in the background
   at the configured interval.

With **Attachments only**, a matching email without attachments creates no file but is
still recorded as processed. The message remains on the mail server.

## Automatic polling and initial scan

MailArchive starts a background check as soon as the application starts. Each enabled
account is then checked again after its account-specific polling interval or the global
default shown in **Settings**. **Archive now** only requests an additional immediate check;
it is not required for normal operation.

The first check searches all messages in the configured folder or label, including old
messages. Successfully processed provider message IDs are skipped before their MIME content
is downloaded on later checks. Messages without a matching rule are checked again so that
newly added or changed rules can process them later.

The processing state is stored in a local SQLite database named `archive-state.sqlite3`.
Its primary key combines the MailArchive account ID, a provider namespace, and the provider's
message ID. For IMAP, that namespace includes the server's UIDVALIDITY value; for Gmail and
Microsoft Graph it includes the API provider and selected label or folder. A row is written
only after a rule matched and the archive operation completed successfully. Failed messages
and messages without a matching rule are not recorded and are therefore retried. Existing
IMAP processing records are migrated automatically to the general index.

## Email providers and authentication

MailArchive supports password-based generic IMAP, interactive user OAuth for Google and
Microsoft, Microsoft application access, and unattended Google Workspace access through a
service account with domain-wide delegation. The Gmail application mode impersonates one
configured Workspace mailbox and does not support personal Gmail accounts. Account dialogs
show only the fields required by the selected provider and authentication mode. Gmail user
sign-in takes a Google Desktop OAuth client ID and an optional client secret. Microsoft
delegated user access takes a Microsoft Entra public-client application ID and an optional
tenant or audience value.

See [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) for the provider matrix and complete
setup instructions for every authentication mode.

## Development environment with venv

Local development always uses the project-local `.venv`. No global Python packages are
required or modified.

On Windows:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup-dev.ps1
.\.venv\Scripts\Activate.ps1
python -m mailarchive
```

On Linux, first install Python's venv and Tk packages. To include the native AppIndicator
backend on Debian or Ubuntu, install the complete development prerequisites:

```bash
sudo apt install \
  python3-venv python3-tk pkg-config \
  libcairo2-dev libgirepository1.0-dev \
  gir1.2-ayatanaappindicator3-0.1
```

Then create and activate the environment:

```bash
chmod +x scripts/setup-dev.sh
./scripts/setup-dev.sh --with-appindicator
source .venv/bin/activate
python -m mailarchive
```

For development without tray integration, the smaller setup remains available:

```bash
./scripts/setup-dev.sh
```

After activation, `python` and `pip` resolve to `.venv`. Release scripts create an isolated
virtual environment in the operating system's temporary directory and remove it when the
build finishes. They do not create another venv inside the project.

The development setup installs MailArchive in editable mode. Consequently,
`.venv/bin/mailarchive` on Linux or `.venv\Scripts\mailarchive.exe` on Windows imports the
current Python files directly from `src`; ordinary source changes do not require a rebuild
or reinstall. The already-running tray process must still be quit and restarted; launching
`mailarchive` a second time only activates the existing single instance. Dependency and
packaging-metadata changes require running the setup script again. A PyInstaller executable
or AppImage is a snapshot and must be rebuilt.

Interactive Google or Microsoft OAuth credentials are entered on the corresponding account;
no provider configuration files are needed for development or packaging. See
[docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) for the provider-side registration steps.

## Building the Windows installer

Requirements: Windows 10 or 11, Python 3.12, PowerShell, and Inno Setup 7 or 6.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build-windows.ps1
```

The script creates an isolated build environment, runs the tests, creates a PyInstaller
`onedir` bundle, and packages it with Inno Setup as
`dist\installer\MailArchive-Setup-0.1.0-x64.exe`. The per-user installer requires no
administrator rights, adds a Start menu shortcut, and registers an uninstaller. The target
computer does not need a separate Python installation.

## Building the Linux AppImage

The AppImage build includes the Ayatana AppIndicator backend. The Linux desktop must also
provide a compatible indicator host to display tray icons. KDE Plasma normally provides
one. On Debian or Ubuntu with GNOME, install the AppIndicator shell extension:

```bash
sudo apt install gnome-shell-extension-appindicator
```

Log out and back in after installing the extension, or enable it through the GNOME
Extensions application. Without an indicator host, MailArchive stays usable as a normal
window and does not hide itself when closed.

For a direct build on Debian or Ubuntu, install Python with `venv` and Tk, the development
packages listed above, and `appimagetool`:

```bash
chmod +x scripts/build-linux.sh
APPIMAGETOOL=/path/to/appimagetool ./scripts/build-linux.sh
```

The output is `dist/MailArchive-0.1.0-x86_64.AppImage`. Build the Linux package on Linux;
PyInstaller does not cross-compile. For broad compatibility, create releases on the oldest
supported Ubuntu LTS release or in an equivalent container.

For a reproducible Ubuntu 22.04 container build:

```bash
chmod +x scripts/build-linux-container.sh
./scripts/build-linux-container.sh
```

The container installs system build dependencies and `appimagetool` in the Docker image.
Python build dependencies are installed into a temporary venv inside the running container;
the venv disappears afterward. Only the finished packages are written to the local `dist/`
directory.

## Release pipeline

Releases are triggered by version tags and produce tested Linux and Windows packages,
workflow artifacts, SHA-256 checksums, and a GitHub Release. See
[docs/RELEASE.md](docs/RELEASE.md) for the authoritative maintainer and automation-agent
procedure.

The first credential save may cause GNOME Keyring or KWallet to request access. MailArchive
deliberately does not fall back to an unencrypted password file. On GNOME with Wayland, the
desktop environment must support AppIndicator icons. Otherwise, MailArchive remains usable
as a normal window and will not hide itself in an unavailable tray.

## Tests

The core logic has no external service dependency and uses Python's standard test runner:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The suite covers rule ordering, yes/no attachment conditions, MIME parsing, safe filenames
and paths, all three save modes, configuration and legacy-state migrations, global and
per-account polling intervals, IMAP/Gmail/Graph message retrieval, duplicate protection,
secure keyring behavior, Linux autostart and single-instance handling, and visible errors
for missing credentials.

## Local data

On Windows, application settings and the processing index are stored in
`%LOCALAPPDATA%\MailArchive`. On Linux, they default to `~/.local/share/mailarchive` in
accordance with XDG. Linux autostart uses `~/.config/autostart/mailarchive.desktop`.
Passwords, client secrets, OAuth token data, and imported Google service-account keys are
stored as generic credentials in Windows Credential Manager or through Secret Service,
GNOME Keyring, or KWallet on Linux. Archived files exist only in the chosen archive folder.

## Current MVP limitations

- Google application access requires a managed Workspace domain, domain-wide delegation,
  and a service-account key; it is unavailable for personal Gmail accounts
- One folder or label per email account, defaulting to `INBOX` or `inbox`
- Provider APIs currently enumerate message IDs on every check; previously archived MIME
  bodies are not downloaded again, but provider-native delta synchronization is not yet used
- One condition per rule in the UI; the internal model already supports multiple conditions
- No deletion or movement of server-side messages
- No code signing or automatic update service yet

MailArchive intentionally runs as an autostart tray application in the user's session, not
as a Windows service or systemd system service. System services run in a separate session
and cannot provide the user's tray UI and desktop notifications.
