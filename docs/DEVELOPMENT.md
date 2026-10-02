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

The release workflow runs these checks before either package build. The Ubuntu CI jobs use `xvfb-run` so the dialog tests count toward the 80% branch coverage gate. On a headless local machine, run the coverage command through `xvfb-run -a`; without a display, the GUI tests skip and coverage can fall below that gate. Profile database tests must start with temporary directories. The 0.0.1 format has no import or migration path from prototype data. To check the bundled Tcl/Tk and Pillow bridge:

```bash
.venv/bin/python -m mailarchive --smoke-test
```

An end-to-end fake-provider test should drive the application facade from configuration through the execution coordinator, provider adapter, local spool, activity queries, and real output files. Keep lower-level repository tests for durable transitions. Important cases include baseline plus new discovery; exact UTC range boundaries and saved timezone; the first matching rule with multiple destinations; per-output failure and retry after the provider mail is gone; one past-mail operation with several selected mailboxes; whole-operation Stop during scanning or publication; retry from a frozen selection while preserving earlier attempts; a waiting operation remaining in Current jobs until outputs settle; a repeated range with an added destination or a previously archived receipt; a missing work copy; crash between publication and receipt; unresolved intakes; keyset-paginated History; bounded provider streams and spool cleanup; a settings save during a poll; failed Gmail label baseline; Graph folder moves and pending rechecks; duplicate attachments; and IMAP UIDVALIDITY reset. A repeated run must identify a reused receipt as **Previously archived**; it must not claim a fresh save or silently recreate an archive file that was later deleted.

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

The application version is in `pyproject.toml` and `src/mailarchive/__init__.py`. The first release, 0.0.1, uses profile schema 1. Schema upgrades will be introduced when an actually published profile format needs to change. For the first release, use the dedicated [0.0.1 notes](releases/0.0.1.md) and the [release procedure](RELEASE.md).
