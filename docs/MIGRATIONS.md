# Database migrations and updates

Application releases, JSON settings, and the SQLite processing index have independent
versions. A new application release does not need a new database schema unless the stored
structure or data changes.

| Version | Source | Current value |
| --- | --- | --- |
| Application | `mailarchive.__version__`, matching `pyproject.toml` | `0.1.0` |
| JSON settings | `models.SETTINGS_SCHEMA_VERSION` | `5` |
| Processing database | `migrations.DATABASE_SCHEMA_VERSION`, persisted in `PRAGMA user_version` | `2` |

The application version is visible in the window title, header, and `--version`.
**Settings > Advanced** includes both schema versions for support diagnostics.

## Startup and upgrades

`ArchiveState` calls the migration runner before the archive service and polling worker
start. This also applies when selecting or relocating the processing database.

1. Acquire a SQLite write reservation with `BEGIN IMMEDIATE` and read `PRAGMA user_version`.
2. Refuse a schema newer than the application supports without changing it. If the schema
   is already current, skip backup and migration.
3. For an existing database with tables, create a unique adjacent backup named
   `<database-name>.pre-v<old-schema>-<unique>.bak` with SQLite's backup API. This includes
   committed data in the WAL. A backup failure prevents the upgrade. Empty databases need
   no backup.
4. Execute every pending migration in order and update `user_version` after each step,
   within one transaction.
5. Commit once after all steps succeed. Any failure rolls back schema, data, and version
   changes together. Startup stops with an error and the backup location when available.

Backups are retained and never overwritten automatically. They contain processing history,
not the archived email files, JSON settings, or protected credentials. Keep normal archive
and configuration backups separately. Old migration backups can be removed manually after
verifying an upgrade and keeping the required independent backups.

The runner uses an explicit transaction because implicit Python `sqlite3` transactions
do not automatically make a sequence of schema changes atomic. It uses a separate reader
for backup while the migration connection reserves writes, avoiding a backup that waits
for its own write transaction.

SQLite reserves `user_version` for application use. See the
[SQLite PRAGMA reference](https://www.sqlite.org/pragma.html#pragma_user_version) and
[SQLite backup API](https://www.sqlite.org/backup.html).

## Existing databases

All databases created before the migration runner have `user_version = 0`. They run these
idempotent baseline steps once, preserving tables and history that already exist:

| Schema | Migration |
| --- | --- |
| 1 | Create the provider-independent processed-message table and index; import old `processed_mail` IMAP rows without duplicates. Keep the legacy table. |
| 2 | Create skipped-message history and initial-scan checkpoints. Preserve existing entries. |

After commit, subsequent starts skip these steps. JSON configuration compatibility remains
in `Settings.from_dict`; older account and settings formats are normalized on load. A JSON
schema newer than supported is rejected. Unreadable or incompatible settings stop startup
instead of loading defaults that could later overwrite the original configuration.

## Adding a migration

Add a new function to `src/mailarchive/migrations.py` and append it to `MIGRATIONS`.
`DATABASE_SCHEMA_VERSION` is the length of that ordered list. Never edit or reorder a
migration after shipping it. Use `connection.execute` and `executemany` inside the runner's
transaction; do not commit, use `executescript`, or run nontransactional operations such as
`VACUUM` in a migration.

Test upgrades from the preceding schema and supported historical schemas, preservation of
processing history, reopening without another migration, failure rollback, and rejection
of a future schema. Update the table above when introducing a schema.

Database-file relocation in `ArchiveState.migrated_to` is a separate operation: it copies
or merges processing history into another file. The destination also passes through the
schema migration runner.

## Recovery and downgrades

For migration failures, correct the reported problem (for example, insufficient disk space
or a malformed database) before restarting. SQLite has already rolled back the attempted
upgrade, and the pre-upgrade backup remains available if needed.

Application downgrades are supported only when both schemas remain compatible. There are
no automatic down-migrations. Legacy builds predating the migration runner do not enforce
this compatibility check and should not be used to open upgraded databases.
Prefer reinstalling a current application if a downgrade is
rejected. To restore pre-upgrade state, quit all instances, retain the current database as
a separate fallback, and restore the selected `.bak` through SQLite's backup API into the
configured database path. Use an application version that supports the restored schema.
Processing performed after the backup will be missing and can be attempted again.

## Update delivery

**Overview > Check for updates** requests the latest public stable release from
`PaDreyer/mailarchive` using the [GitHub Releases API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release).
It compares numeric `major.minor.patch` versions, ignores drafts and prereleases, and
never offers an older version. Offline checks and API errors are displayed without affecting
mail processing. Checks occur only when requested and send no mailbox or configuration data.

The current implementation opens the release page after confirmation. Users download the
Windows installer or Linux AppImage, quit the running application, and install or replace
the package. The first start of the updated application owns database migration. A future
installer integration must preserve that order and add verified downloads and a reliable
shutdown/restart handoff; it must not modify the database from the installer.
