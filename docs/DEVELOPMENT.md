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

On Debian or Ubuntu, install the packages needed for Tk and AppIndicator support:

```bash
sudo apt install \
  python3-venv python3-tk pkg-config \
  libcairo2-dev libgirepository1.0-dev \
  gir1.2-ayatanaappindicator3-0.1
```

Then create the environment and start MailArchive:

```bash
./scripts/setup-dev.sh --with-appindicator
source .venv/bin/activate
python -m mailarchive
```

For development without AppIndicator support, run `./scripts/setup-dev.sh` without the option.

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
system libraries and `appimagetool` installed, use `./scripts/build-linux.sh`.

The [release guide](RELEASE.md) covers version changes and publication through GitHub Actions.
