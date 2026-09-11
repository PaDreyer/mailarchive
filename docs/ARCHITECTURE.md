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
- `account_form.py` and `settings_form.py` normalize and validate user input using immutable data
  transfer objects. These modules have no dependency on Tk.
- `service.py` owns one archive run. `runner.py` schedules runs, while `mail_sources.py`,
  `oauth.py`, and `imap_client.py` isolate remote-provider behavior.
- `config.py`, `credential_data.py`, `credentials.py`, and `storage.py` own local persistence.
  Secret values never enter the normal settings file.

The processing database keeps archived message records separately from the message IDs skipped
when a provider namespace establishes its initial checkpoint. This lets the settings toggle
exclude existing mail by default while still allowing a later opt-in to backfill it.

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
