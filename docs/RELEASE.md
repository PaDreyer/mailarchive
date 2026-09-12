# Releasing MailArchive

This document is the authoritative release procedure for maintainers and automation agents.
The GitHub Actions workflow in `.github/workflows/release.yml` performs the tests, native
platform builds, artifact collection, checksum generation, and GitHub Release publication.

## Agent safety rules

- Do not create or push a release tag unless the user explicitly requests a release.
- Inspect the working tree first and preserve unrelated user changes.
- Never tag uncommitted changes. A release tag must point to the reviewed release commit.
- Treat published version tags as immutable. Fix release defects with a new patch version
  instead of moving an existing tag.
- Do not upload locally built packages manually during the normal release process. The
  workflow produces the authoritative packages on native GitHub-hosted runners.

## Version sources

The release version is stored in both of these files and must be identical:

- `pyproject.toml`: `project.version`
- `src/mailarchive/__init__.py`: `__version__`

The tag is the same version prefixed with `v`. For version `0.2.0`, the only valid tag is
`v0.2.0`. The workflow rejects a tag that does not match both version sources. The Inno
Setup version is supplied by the Windows build script; do not change its fallback solely
for a release.

The desktop UI and `--version` read `mailarchive.__version__`. The SQLite and JSON schema
versions are independent of the release version. When persistence changes, follow the
[migration guide](MIGRATIONS.md), append a database migration, and verify historical upgrade
paths. Published databases must not require manually deleting the processing index.

## Release trigger

The release workflow is intentionally tag-only. Its sole trigger is a push of a Git tag
whose name starts with `v`:

- `git push origin main` does not start the release workflow.
- `git tag -a v0.2.0 -m "MailArchive 0.2.0"` only creates a local tag and does not start
  the release workflow.
- `git push origin v0.2.0` publishes the tag to GitHub and starts the release workflow.

Avoid `git push --tags` and `git push --follow-tags` during routine branch work because
they can publish a previously created release tag and therefore trigger a release.

## Release procedure

1. Verify that `BUNDLED_MICROSOFT_PUBLIC_CLIENT_ID` in
   `src/mailarchive/provider_config.py` contains the reviewed production Entra public-client
   application ID. The build scripts reject placeholders and development environment overrides.
2. Choose the next version and update both version sources listed above.
3. Update user-facing documentation when the release changes behavior, configuration, or
   system requirements.
4. From the repository root, activate the development venv and run the complete test suite:

   ```bash
   source .venv/bin/activate
   python -m pip install -e ".[test]"
   python -m coverage run -m unittest discover -s tests -v
   python -m coverage report
   ```

5. Optionally verify the Linux package locally when Linux packaging changed:

   ```bash
   ./scripts/build-linux-container.sh
   ```

6. Review the complete diff, commit the release preparation, and push that commit to the
   intended branch. Do not include unrelated changes. This branch push does not start the
   release workflow.
7. Create an annotated version tag locally, then push that tag explicitly. Replace `0.2.0`
   with the actual version:

   ```bash
   git tag -a v0.2.0 -m "MailArchive 0.2.0"
   git push origin v0.2.0
   ```

The second command is the publication trigger. No separate manual workflow dispatch is
needed.

## Pipeline stages

The release workflow performs these stages in order:

1. **Verify:** compare the tag with both version sources, create an isolated venv, install
   the project, and run all tests with the 80% branch-coverage gate on Ubuntu 22.04.
2. **Build:** build the Linux AppImage in the Ubuntu 22.04 container and build the Windows
   x64 installer with PyInstaller and Inno Setup on Windows Server 2022. These jobs run in
   parallel, repeat the platform-relevant tests, and smoke-test each packaged executable
   through the build scripts.
3. **Artifacts:** retain both platform packages as GitHub Actions artifacts for 14 days.
4. **Release:** download the successful build artifacts, generate SHA-256 checksums, and
   create the GitHub Release with generated release notes. Re-running this stage replaces
   existing assets with the newly produced files.

The expected GitHub Release assets for version `0.2.0` are:

```text
MailArchive-0.2.0-x86_64.AppImage
MailArchive-Setup-0.2.0-x64.exe
SHA256SUMS.txt
```

GitHub also provides its automatically generated source archives.

## Verification after publication

Confirm all of the following before announcing the release:

- Every job in the tag-triggered `Release` workflow completed successfully.
- The GitHub Release uses the expected tag and is not a draft.
- Both platform packages and `SHA256SUMS.txt` are attached.
- The filenames contain the intended version.
- The downloaded packages match the published checksums. On Linux, run this command in the
  directory containing all three assets:

  ```bash
  sha256sum --check SHA256SUMS.txt
  ```

The packages are currently unsigned. Windows and some Linux desktops can therefore display
an unknown-publisher warning.

## Handling failures

- **Version validation failed:** correct both version sources in a new commit and release a
  new version tag. Do not move a published tag.
- **Tests or a build failed:** fix the cause in a new commit, increment at least the patch
  version, and create a new tag.
- **Transient runner or network failure:** use GitHub Actions to re-run the failed jobs for
  the same workflow run. The release step is safe to re-run because asset uploads use
  replacement semantics.
- **Release publication failed after both builds succeeded:** re-run only the failed release
  job when possible so the successful build artifacts remain the publication inputs.
