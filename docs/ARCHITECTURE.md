# Architecture

MailArchive is a local desktop application with provider integrations. The code is split by
responsibility so that provider and persistence behavior can be tested without creating a GUI.

## Component boundaries

- `app.py` is the composition root and process entry point. It selects platform services,
  creates the desktop controller, and owns single-instance startup and shutdown.
- `desktop.py` coordinates the long-lived UI state and application services. It does not parse
  provider-specific form values itself.
- `dialogs.py`, `tray.py`, and `ui_text.py` contain Tk dialogs, notification-area integration,
  and presentation labels respectively.
- `desktop_setup.py` owns the Linux AppImage setup dialog and settings page.
  `linux_integration.py` owns its user-scoped installation and separate versioned receipt;
  it has no dependency on Tk or credential storage. `desktop_entry.py` serializes shared
  freedesktop launchers and login entries with both required escaping layers.
- `account_form.py`, `rule_form.py`, and `settings_form.py` normalize and validate user input using immutable data
  transfer objects. These modules have no dependency on Tk.
- `service.py` owns one archive run. `runner.py` schedules runs, while `mail_sources.py`,
  `oauth.py`, and `imap_client.py` isolate remote-provider behavior.
  Each provider scan owns its pagination and download state explicitly. Target processing
  keeps rule snapshots, download filtering, archive results, and checkpoint commits together.
- `config.py`, `credential_data.py`, `credentials.py`, and `storage.py` own local persistence.
  Secret values never enter the normal settings file.

Large Windows OAuth caches use a manifest plus multiple protected Credential Manager entries so
the native per-entry blob limit does not prevent token persistence.

The domain model has three distinct levels. `Account` owns the connection, authentication
identity, credential reference, polling interval, and rule scope. Its `mailboxes` contain
addressed `Mailbox` targets with independent enabled and existing-mail preferences. Each
mailbox selects multiple folders/labels, or all folders when the selection is empty.
`MailTarget` is an adapter input for one technical synchronization scope; it is not a login.

`mail_identity.py` separates `MessageScope.processing_namespace` from
`synchronization_namespace`. Gmail message IDs and Graph immutable IDs are mailbox-wide;
processed-message lookups therefore use the physical provider/mailbox identity independently
of account ID and folders. Account ID remains as archive provenance. Unmatched rule fingerprints
and initial exclusion remain scoped to the configuring account and mailbox preferences.
IMAP UIDs are only unique within server, port, addressed mailbox, folder, and UIDVALIDITY.
The authentication username is not part of message identity. IMAP moves cannot generally be
deduplicated because the protocol assigns a new folder-local UID.

The UI edits mailboxes beneath one connection. Adding or removing targets preserves connection
credentials. Provider capabilities constrain additional addresses: Microsoft delegated/shared
and application access, including shared IMAP XOAUTH2, and Workspace domain-wide delegation
support multiple addresses with the required server permissions. Generic IMAP password access
and Gmail user OAuth expose the signed-in user's mailbox only.

Old configuration binds its single folder and existing-mail preference to a migrated mailbox,
retaining the account ID and original source binding for one-time history adoption. Ambiguous
`imap:` records cannot be assigned safely; the existing-mail preference controls a one-time
new baseline or recheck. See [database migrations](MIGRATIONS.md).

Rules optionally restrict their scope to stable account IDs. `None` applies to every account;
an explicit list applies only to those accounts, including no accounts when empty. The service
passes the source account ID into rule selection before message conditions are checked.
Unknown or deleted IDs never broaden the scope, and account renames leave rules intact.

Rule destinations are optional relative subfolders beneath the global archive directory.
`date_folder_position` selects no date folders or year/month folders before or after the
complete rule subfolder. Storage resolves and validates the full combined path, including
existing symlinks. The rule editor preview and destination summary use the same path builder
with `YYYY/MM` placeholders. Archiving computes the local email date once for both folders
and filenames, falling back to the archive time for missing or invalid dates. These storage
options do not affect matching fingerprints or cause successfully archived messages to be
processed again.

Messages checked without a matching rule are stored separately with a fingerprint of the
enabled matching conditions applicable to their account. Unchanged checks skip these IDs
before downloading MIME content. Changing applicable matching behavior permits another check;
archive destinations, rule names, and unrelated accounts do not invalidate the fingerprint.
An archive run uses a rule snapshot for both its fingerprint and message evaluation, so edits
during a download apply on the next run. Successful archiving removes the unmatched entry in
the same transaction as recording completion. Failed processing remains eligible for retry.

Synchronization checkpoints are separate from processing history and initial-scan checkpoints.
`synchronization.py` defines a run-scoped `SyncSession`: the service supplies cursor and local
recheck lookups; provider adapters publish a candidate cursor only after consuming all pages.
Gmail captures a pre-scan history ID on full scans and uses history events thereafter. Microsoft
uses folder-scoped delta queries and preserves immutable IDs. IMAP uses UIDs greater than the
saved UID within the existing UIDVALIDITY namespace, filtering the reversed `n:*` range edge
case and batching targeted UID rechecks. Selection must supply a nonzero 32-bit UIDVALIDITY;
a missing response causes one read-only reopen, then fails safely. A validity change during
search or download stops the scope. Invalid IDs, cursor fields, and repeating continuation
pages fail without advancing the checkpoint.

`mailbox_check` independently records the attempt, result, and last complete successful check
for each addressed mailbox, starting before folder discovery. Partial failure retains the
previous mailbox success time while other targets continue.

The service commits each technical scope's cursor, cursor-commit time, initial skipped IDs,
and message availability together when its downloads and processing succeed. A mailbox baseline
completes only after every selected scope succeeds. Failure in one folder or mailbox does not
stop the others. Gmail combines selected labels as a union with one history cursor; Graph
recursively discovers physical folders and keeps a delta cursor per folder; IMAP lists all
selectable folders and keeps a cursor per UIDVALIDITY scope. Completion records are
written per message, so a retry can replay changes without downloading completed mail. Rule
changes select unmatched IDs with a different fingerprint; backfill selects initial skips,
excluding already processed or unchanged unmatched IDs. Rechecks verify current selected-folder or label membership. Graph performs mailbox-wide
targeted rechecks once, so old mail moved between watched folders can still be backfilled. Unavailable IDs suppress repeated targeted requests without deleting processing
history, and provider responses mark reappearing IDs available again.

API cursors are additionally bound to the connection/authentication identity, addressed mailbox,
and folder selection. Changing selection reconciles once while preserving processing history.
Microsoft continuation links must stay on the configured Graph HTTPS origin and API path before
they receive a bearer token. Token expiration restarts enumeration once with the existing
baseline and processing history. A database copy preserves checkpoints; merging histories
invalidates cursors and availability so the next run reconciles safely. See the
[provider contract audit](PROVIDER_SYNC_CONTRACTS.md).

Dependencies should point from the entry point and UI toward these application and persistence
modules. Provider, model, rule, and storage modules must not import desktop UI code.

## State-change guarantees

Linux desktop setup stages every file before committing. AppImages are replaced by rename,
never overwritten in place, preserving a running executable's inode. A commit failure restores
the previous files, including launchers, enabled autostart and the installation receipt; backups
are retained and their locations reported if restoration itself fails. This is recoverable
error handling, not a guarantee of a multi-file atomic commit across a power loss. Foreign
launchers and symlink targets are refused. GUI setup runs on a worker thread with completion
posted to the UI queue; shutdown is blocked until that transaction finishes. The initial prompt
is suppressed for `--minimized`, Windows and development runs. Skipping it persists only the
prompt decision; changing shortcuts does not change archive settings, credentials or databases.

Account changes treat the settings list and credential entry as one recoverable operation. If
the configuration file cannot be saved, the previous in-memory accounts and raw credential entry
are restored. Changing fields that bind credentials to a remote identity invalidates the old
credentials; IMAP and Microsoft application accounts require a replacement secret immediately.
Account saves and removal share the archive-run lock and fail promptly during an active run.
This prevents a previous connection snapshot from reading replacement credentials.

Rule changes are saved as a candidate configuration before becoming active. Failed saves
preserve the active rules, displayed rows, and selected rule.

Settings changes are normalized before any side effects occur. Database relocation and startup
configuration are rolled back when saving the configuration fails.
The database-change context holds the archive-run lock across relocation, configuration save,
rollback, and publication of the new settings. Its relocation callback updates the service
state while that lock is held, so polling cannot write to an uncommitted database.
Failed restoration is reported alongside the original error. Changes that were never applied
do not trigger restoration.

The polling worker catches failures at the run boundary, reports them to the activity log/UI,
and retries on a later tick. Runs that raise do not advance scheduling completion times. The
service signals a busy run with `ArchiveRunBusyError`; polling retries on a later tick and keeps
manual requests pending. Only accounts returned by an accepted run receive completion times. Callback
failures are logged without stopping the worker.

## Change guidance

- Put input normalization and validation in a UI-independent module before wiring it to widgets.
- Keep provider credentials filtered through `credential_data.py`; do not serialize secrets in
  models or configuration.
- Add focused unit tests for state transitions and rollback paths. GUI tests should verify only
  widget wiring and user-visible behavior.
- Run unit tests with branch coverage plus Ruff lint and format checks before merging.
- Ruff limits function complexity to 15; split responsibilities into named methods before
  adding further branches to a function at that limit.
