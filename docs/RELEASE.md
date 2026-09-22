# Release procedure

MailArchive's first release in the new product line is **0.0.1**, with tag `v0.0.1`. The user handles removal of older GitHub releases. Do not delete old Git tags or Git history as part of this restart. Do not create or push a tag, publish a release, or deploy without a separate release request.

## Version sources

`pyproject.toml` (`project.version`) and `src/mailarchive/__init__.py` (`__version__`) must match. Build scripts and the desktop title read these values. The release workflow rejects a tag that differs from `v` plus this version. Download asset names use the same version.

The 0.0.1 profile is a fresh format. Earlier prototype JSON and SQLite data are not imported, merged, or rolled back. The database format marker is a separate value from the application release version. Later upgrade rules should be designed when a published format actually has users.

## Before a requested release

1. Review the implementation and documentation against [the 0.0.1 plan](PLAN-0.0.1.md).
2. Run Ruff, formatting, tests and branch coverage from [Development](DEVELOPMENT.md). Exercise the GUI on a display and test representative Windows/Linux packages on their platforms.
3. Inspect the complete diff and commit the reviewed release preparation.
4. Only after explicit authorization, create the annotated `v0.0.1` tag on that commit and push the tag. Pushing a branch alone does not launch publication.

The `.github/workflows/release.yml` workflow validates the version, runs verification, builds the Linux AppImage and Windows installer, computes checksums, then creates the GitHub release. For **exactly `v0.0.1`**, it uses [`docs/releases/0.0.1.md`](releases/0.0.1.md) as the release note body. This avoids generating “Changes since v1.0.4” from old Git tags after older GitHub releases are deleted. Later tags can use the ordinary previous-tag changelog.

The intended assets are:

- `MailArchive-0.0.1-x86_64.AppImage`
- `MailArchive-Setup-0.0.1-x64.exe`
- `SHA256SUMS.txt`

After an authorized publication, verify that the public latest-release link resolves to `v0.0.1`, that the desktop update check handles it, and that both packages and checksums are downloadable. GitHub release entries, Git tags, Actions build artifacts, and local source history are separate objects. Deleting a release entry does not delete its tag or old Actions artifacts. The current workflow retains build artifacts for 14 days; any additional cleanup requires its own explicit decision.
