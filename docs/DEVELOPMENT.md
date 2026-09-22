# Development

MailArchive 0.0.1 is a Python 3.10+ Tkinter application. Create an isolated virtual environment and install the local package:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test,quality]'
```

On Windows use `.venv\Scripts\python.exe`. Keep credential data and real mailboxes out of tests. Unit and integration tests use temporary profile directories and fake provider responses.

## Verification

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m coverage run -m unittest discover -s tests -v
.venv/bin/python -m coverage report
```

The release workflow runs these checks before either package build. The Ubuntu CI jobs use `xvfb-run` so the dialog tests count toward the 80% branch coverage gate. On a headless local machine, run the coverage command through `xvfb-run -a`; without a display, the GUI tests skip and coverage can fall below that gate. The profile database tests must start with temporary directories; the 0.0.1 format has no prototype import path. To check the bundled Tcl/Tk and Pillow bridge:

```bash
.venv/bin/python -m mailarchive --smoke-test
```

A meaningful end-to-end fake-provider test should exercise `ConfigStore`, `WorkspaceStore`, `ArchiveService`, an adapter, the local spool, and real output files. Important cases are baseline plus new discovery, exact UTC range boundaries and persisted timezone, first matching rule with multiple destinations, per-destination state including shared outputs, pause/resume, partial destination failure and later resume after source deletion, explicit resume with a missing work copy, crash between publication and receipt, a repeated range with an added destination, unresolved manual intake cancellation, cancellation racing a reservation, paginated processing history, bounded provider streams and spool cleanup, a stale poll racing a settings save, failed Gmail label baseline, Graph folder moves including pending rechecks, duplicate attachments, and IMAP UIDVALIDITY reset.

## Package builds

Linux AppImage (Ubuntu 22.04 container, Docker required):

```bash
./scripts/build-linux-container.sh
```

The output is `dist/MailArchive-0.0.1-x86_64.AppImage` when the project version is 0.0.1. On a suitable native Linux host, `./scripts/build-linux.sh` is also available.

Windows requires Python, PyInstaller and Inno Setup as described by the build script:

```powershell
./scripts/build-windows.ps1 -Python python
```

The installer output is `dist/installer/MailArchive-Setup-0.0.1-x64.exe`. A local Linux development run does not validate that Windows package. The release workflow builds on both operating systems after verification.

The application version is in `pyproject.toml` and `src/mailarchive/__init__.py`. The profile schema marker is independent. For the first release, use the dedicated [0.0.1 notes](releases/0.0.1.md) and the [release procedure](RELEASE.md).
