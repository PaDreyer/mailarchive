# Development

Run the commands below from the repository root. The setup scripts create `.venv` and install
MailArchive in editable mode. Restart the application to pick up Python source changes.
Quit any running tray instance first; MailArchive allows one instance per user session.

## Windows

Install Python 3.12 with Tcl/Tk support and use PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup-dev.ps1
.\.venv\Scripts\Activate.ps1
python -m mailarchive
```

## Linux

On Debian or Ubuntu, install the packages needed for Python virtual environments and Tk:

```bash
sudo apt install python3-venv python3-tk
```

Then create the environment and start MailArchive:

```bash
./scripts/setup-dev.sh
source .venv/bin/activate
python -m mailarchive
```

MailArchive uses the StatusNotifierItem D-Bus protocol directly on Linux. `setup-dev.sh` installs
its Python dependency into `.venv`; it does not require PyGObject or either Ayatana AppIndicator
library. MailArchive deliberately does not use the legacy Xorg tray protocol, because GNOME can
destroy its tray manager when switching views. If no StatusNotifier host is available, the
application stays open as a normal window instead.

## Microsoft OAuth registration

Development uses the bundled Microsoft registration. To test your own registration,
set `MAILARCHIVE_MICROSOFT_CLIENT_ID` before starting the application.

On Linux:

```bash
MAILARCHIVE_MICROSOFT_CLIENT_ID=11111111-2222-4333-8444-555555555555 python -m mailarchive
```

In PowerShell:

```powershell
$env:MAILARCHIVE_MICROSOFT_CLIENT_ID = "11111111-2222-4333-8444-555555555555"
python -m mailarchive
```

Replace the example with your registration's application ID. This runtime override does
not change the bundled registration used by package builds. See
[Custom Microsoft OAuth setup](MICROSOFT_OAUTH_SETUP.md) for the registration steps.

## Tests and code checks

With `.venv` active, install the test and quality tools:

```bash
python -m pip install -e ".[test,quality]"
python -m coverage run -m unittest discover -s tests -v
python -m coverage report
python -m ruff check src tests
python -m ruff format --check src tests
```

Tests use local fakes instead of real mail accounts. Tk widget tests need a display and are
skipped when none is available. CI runs coverage checks on Python 3.10 through 3.14 and a
separate Windows test job on Python 3.12. The coverage threshold is 80%; Ruff limits function
complexity to 15. See the [test workflow](../.github/workflows/test.yml) and
[project configuration](../pyproject.toml).

## Building packages

Packages are built for their target operating system. Build scripts create a temporary
environment for dependencies and remove it afterward; they do not reuse `.venv`.
Package builds require a valid bundled Microsoft client ID in
`src/mailarchive/provider_config.py`.

### Windows installer

Requirements: Windows 10 or 11, Python 3.12, PowerShell and Inno Setup 6 or 7.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build-windows.ps1
```

The script runs tests, builds and smoke-tests the PyInstaller application, then creates
`dist\installer\MailArchive-Setup-<version>-x64.exe`.

### Linux AppImage

With Docker installed, build in the supplied Ubuntu 22.04 container:

```bash
./scripts/build-linux-container.sh
```

The output is `dist/MailArchive-<version>-x86_64.AppImage`. For a native build with the required
system libraries, `xvfb`, `xauth`, `x11-utils`, and `appimagetool` installed, use
`./scripts/build-linux.sh`.

Both build scripts explicitly bundle Pillow's dynamic Tk helpers and run `--smoke-test` on
the frozen application. This creates the real window and bundled icon, processes GUI events,
hides and restores the window, and exits without opening user settings or accounts. Linux runs
the tests and smoke checks under Xvfb, including a check of the final AppImage through
`--appimage-extract-and-run`. Any smoke-test failure or 30-second timeout stops the build.

Smoke-test desktop integration using a disposable user account or isolated `XDG_DATA_HOME`
and `XDG_CONFIG_HOME`; the normal source entry point does not offer AppImage installation.
Check first-run setup, skipping and reopening settings, a localized or disabled desktop folder,
login autostart, and applying a newer AppImage over an integrated installation. The automated
suite also injects staging, commit and rollback failures and validates generated desktop files
when `desktop-file-validate` is available. Windows shortcuts remain installer-owned.

Also check closing to the tray and reopening repeatedly, including after a minimized login
start: the dock should keep the MailArchive name and icon and match the menu launcher.
`tests.test_window` checks the native window identity when a display is available, including
the X11 icon properties when `xprop` and `xwininfo` are installed. Existing AppImage installations need
Settings > Desktop integration > Configure > Apply from the updated AppImage to refresh
their launcher files, followed by quitting and reopening MailArchive.

The [release guide](RELEASE.md) covers version changes and publication through GitHub Actions.
