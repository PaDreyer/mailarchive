# Database migrations and updates

Application releases, JSON settings, and the SQLite processing index have independent
versions. A new application release does not need a new database schema unless the stored
structure or data changes.

| Version | Source | Current value |
| --- | --- | --- |
| Application | `mailarchive.__version__`, matching `pyproject.toml` | `0.2.1` |
| JSON settings | `models.SETTINGS_SCHEMA_VERSION` | `8` |
| Processing database | `migrations.DATABASE_SCHEMA_VERSION`, persisted in `PRAGMA user_version` | `6` |

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
| 3 | Record unmatched message IDs with the applicable rule fingerprint and check timestamp, so unchanged checks skip them before downloading. |
| 4 | Add synchronization checkpoints with remote identity, opaque cursor, and successful-check time; add unavailable-message IDs for targeted rechecks. Preserve all processing and initial-scan history. |
| 5 | Add one-time mailbox-history adoption markers and an index for physical message identity across connections. Preserve every earlier table and record. |
| 6 | Add per-mailbox check attempts, results, and last-success timestamps independently of remote cursors. Preserve every earlier checkpoint and history record. |

After commit, subsequent starts skip these steps. JSON configuration compatibility remains
in `Settings.from_dict`; older account and settings formats are normalized on load. A JSON
schema newer than supported is rejected. Unreadable or incompatible settings stop startup
instead of loading defaults that could later overwrite the original configuration.

Settings schema 6 adds `account_ids` to rules. Missing or `null` means all current and future
accounts, preserving existing rules from schema 5 and earlier. A list restricts a rule to
those account IDs; an empty list applies to no accounts. The editor requires a nonempty
selection for restricted rules. Unavailable IDs are retained so removing an account cannot
broaden a rule. Schema 6 prevents older builds with the schema guard from silently ignoring
these restrictions. That settings change did not require a SQLite migration. Processing schema
3 separately adds persistent unmatched-message history. Existing unmatched messages are checked
once after upgrading because earlier versions did not record them.

Settings schema 7 adds `date_folder_position` to rules: `none` (the default),
`before_subfolder`, or `after_subfolder`. Missing values in older settings disable date
folders and retain each rule's existing destination. An empty destination now selects the
archive directory itself. New installations use that destination for the catch-all rule;
older configurations without an explicit rule retain their implicit `Inbox` destination.
Schema 7 prevents older builds from silently dropping date-folder choices. No SQLite migration
is required because the processing index already stores the actual destination and file paths.

## Independent mailbox checks (SQLite schema 6)

`mailbox_check` records each connection/target mailbox's latest attempt, completion, status,
error, and last complete successful-check time. It does not replace technical synchronization
checkpoints and is updated even when discovery or required IMAP UIDVALIDITY fails. Existing
scope `checked_at` timestamps and cursors remain untouched during migration. Previous schemas
have no complete mailbox-level attempt/result evidence, so no historical success is invented;
the next check starts the new record. Database copies preserve the records. Merges keep the
latest attempt and latest success timestamp while reconciling remote cursors as before.

## Account and mailbox separation (settings schema 8, SQLite schema 5)

An account now owns authentication and a `mailboxes` list. Each mailbox stores its address,
multiple folder/label selections (empty means all), enabled flag, and existing-mail preference.
Old single-folder accounts migrate to one mailbox with the same selection and preference.
Account IDs remain unchanged, so credentials and rule scope are preserved. Application access
no longer requires a sign-in username; addressed targets are configured separately.

The normalized account retains `legacy_source`, the original non-secret connection/mailbox
binding. At the first check, SQLite adopts unambiguous Gmail/Graph folder histories into their
mailbox-wide processing namespace and IMAP v2 histories into the addressed folder/UIDVALIDITY
namespace. A durable migration marker prevents a later check from restoring obsolete unmatched
fingerprints or skipped IDs. The binding ensures adding/reordering targets cannot assign old
records to a different address. Original records remain as evidence. New technical scopes
reconcile once to establish correctly bound cursors; they do not reuse old folder cursors.
Copies and merges preserve the adoption markers. JSON and SQLite compatibility guards require
a compatible application for downgrades; recovery must restore both settings and database.

## IMAP namespace upgrade

The earlier IMAP upgrade introduced server, port, login, folder, and UIDVALIDITY in
`imap-v2:` namespaces. Settings schema 8 and SQLite schema 5 adopt these records into
`imap-v3:` addressed-mailbox namespaces, removing login from message identity. Host names and INBOX are case-insensitive; other folder
names retain their case. Changing the folder or remote identity cannot reuse another
mailbox's message history.

Legacy `imap:` records do not contain enough information to assign them to a specific
mailbox. They remain in the database for reference, but no longer suppress downloads.
For an account with legacy history and no completed scoped checkpoint, the mailbox's existing-mail preference controls the upgrade:

- **Disabled:** establish a new baseline by recording the current message IDs as skipped,
  without downloading or archiving their MIME content. The original starting point cannot
  be reliably assigned to this mailbox. Mail received before the new baseline is also
  skipped; only messages first seen in subsequent checks are archived. An informational
  activity-log entry explains this behavior. An interrupted listing leaves the baseline
  incomplete and retries the listing on the next check.
- **Enabled:** recheck existing mail, including previously skipped and unmatched messages.
  An activity-log warning explains the possibility of archiving messages again. Successfully
  processed messages receive scoped records immediately; an interrupted listing retries
  without downloading those messages again.

The first completed listing ends the transition. Later folder changes follow the same
existing-mail preference. Rule destination changes never reset a completed baseline.

Rechecking uses the current rules and destination. Unchanged messages with valid dates
normally overwrite the same deterministic filenames; changed destinations, rules, or
missing dates can produce additional files. Verify the archive after this upgrade.
The initial IMAP v2 namespace transition required no table change; schema 5 separately
adds markers for adopting that history into the new mailbox model.
Database relocation preserves both legacy history and the scoped completion checkpoint.

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

Schema 4 leaves existing histories and initial checkpoints intact. The first successful check
after upgrading enumerates the selected folder or label once to obtain a synchronization cursor.
Unknown messages follow the existing baseline rather than establishing a new one. Subsequent
checks use incremental synchronization. Failed checks leave the old cursor unchanged.
Copying the database to a new path preserves synchronization; merging into an existing database
invalidates synchronization cursors and availability records because neither history proves
that its cursor covers all work in the combined database. The next check reconciles once.

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
