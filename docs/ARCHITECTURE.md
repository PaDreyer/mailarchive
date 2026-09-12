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
- `account_form.py`, `rule_form.py`, and `settings_form.py` normalize and validate user input using immutable data
  transfer objects. These modules have no dependency on Tk.
- `service.py` owns one archive run. `runner.py` schedules runs, while `mail_sources.py`,
  `oauth.py`, and `imap_client.py` isolate remote-provider behavior.
- `config.py`, `credential_data.py`, `credentials.py`, and `storage.py` own local persistence.
  Secret values never enter the normal settings file.

Large Windows OAuth caches use a manifest plus multiple protected Credential Manager entries so
the native per-entry blob limit does not prevent token persistence.

The processing database keeps archived message records separately from the message IDs skipped
when a provider namespace establishes its initial checkpoint. The account-level setting can
therefore exclude existing mail by default while still allowing a later opt-in to backfill that
account independently.

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

Dependencies should point from the entry point and UI toward these application and persistence
modules. Provider, model, rule, and storage modules must not import desktop UI code.

## State-change guarantees

Account changes treat the settings list and credential entry as one recoverable operation. If
the configuration file cannot be saved, the previous in-memory accounts and raw credential entry
are restored. Changing fields that bind credentials to a remote identity invalidates the old
credentials; IMAP and Microsoft application accounts require a replacement secret immediately.

Settings changes are normalized before any side effects occur. Database relocation and startup
configuration are rolled back when saving the configuration fails.

## Change guidance

- Put input normalization and validation in a UI-independent module before wiring it to widgets.
- Keep provider credentials filtered through `credential_data.py`; do not serialize secrets in
  models or configuration.
- Add focused unit tests for state transitions and rollback paths. GUI tests should verify only
  widget wiring and user-visible behavior.
- Run unit tests with branch coverage plus Ruff lint and format checks before merging.
