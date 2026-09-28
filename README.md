# MailArchive 0.0.1

MailArchive is a local Python desktop application for saving email and attachments from IMAP, Gmail API, and Microsoft Graph mailboxes. It runs on Windows and Linux. Version 0.0.1 starts a fresh product format; it does not import settings or history from earlier prototypes.

## How archiving works

1. Add an account and one or more mailbox folders or label IDs. An empty folder list selects all accessible folders. Folder names use one line each; spaces are preserved.
2. Add rules in priority order. The first enabled rule whose account scope and conditions match chooses all destinations for that message. Every rule can be deleted; an empty rule list saves nothing.
3. Give each rule one or more **full destination paths**. Each destination chooses email, attachments, or both, and whether attachments go directly into that folder or into one folder per mail. `{year}` and `{month}` use the provider reception time in the configured archive timezone; `{{` and `}}` represent literal braces. Missing subfolders are created when written.
4. For each mailbox, choose **Archive messages already present on the first check** if its existing mail should be processed when monitoring starts. Otherwise the first check records that mail as a baseline and later automatic checks process newly discovered messages. Changing this choice later affects only folders that have not been checked yet. Use **Check mail now** to run a check immediately; a later imported old message can still be a new discovery.
5. Select a rule and use **Apply to past mail** to process mail in every enabled mailbox covered by the rule's email account scope, using each mailbox's configured folders or labels. The dialog only asks for an optional inclusive local date range and its timezone. This explicit run applies only the selected rule, regardless of other rules' priority, and does not move the automatic discovery cursor. Repeating it can add outputs after a rule change without duplicating identical outputs. Saving a rule does not start the run.
6. Use **Open work** when a destination is unavailable. It shows each destination, individual output errors, unresolved message intake errors, and interrupted range searches. Accepted plans can be paused, resumed, or aborted. After a mail has been fully accepted locally, its raw copy remains until every requested output succeeds or that plan is explicitly aborted. An interrupted range search can be resumed or cancelled separately.
7. Use **Processing history** to inspect completed, aborted, unmatched, filtered, and failed work. Each accepted plan retains its source, provider reception time, frozen rule, destination status, concrete output paths, and errors. Older entries remain available through **Load more**.

Archived mail is `.eml`. Attachment names are made safe for the destination filesystem. A file already present at the requested name is not overwritten or treated as a prior MailArchive success without a receipt. Successful outputs are tracked individually. If a user later deletes an archive file, a normal range run does not silently repair it.

## Install and run

The first planned release tag is `v0.0.1`. The tag and downloadable Windows installer/Linux AppImage are created only when the release is authorized and published. Build instructions are in [Development](docs/DEVELOPMENT.md).

For source development:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test,quality]'
.venv/bin/python -m mailarchive
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

Credentials remain in the operating system credential store. The profile database is `workspace.sqlite3` in the platform user data directory; accepted raw mail is kept in its adjacent `work` folder. Configuration, source identities, runs, receipts, and the activity log share that database. An unknown or damaged profile database is rejected. There is no automatic import of prototype files.

Provider message bodies are downloaded and written in bounded chunks. One message may contain at most 256 MiB of raw RFC 822 data. The work folder may contain at most 2 GiB and MailArchive keeps a 64 MiB disk reserve for state updates. At most 256 unresolved message intakes can remain active at once; discovery pauses at that boundary until existing errors are retried or cancelled. Capacity failures stay visible in Open work and do not advance the affected automatic cursor.

## What the source can identify

Gmail uses message IDs across labels; Graph uses immutable message IDs across folders within a mailbox. Generic IMAP identifies a message by folder, UIDVALIDITY, and UID. IMAP moves or UIDVALIDITY changes can therefore appear as new source occurrences. A UIDVALIDITY change pauses that folder until the user explicitly starts a new baseline or runs a range selection. MailArchive does not infer equality from Message-ID, date, or identical content across providers or mailboxes.

Provider reception times drive ranges: Gmail `internalDate`, Graph `receivedDateTime`, and IMAP `INTERNALDATE`. A missing provider time is an intake error. Gmail import operations can assign an old `internalDate` to newly inserted mail; discovery still uses IDs and cursors.

MailArchive writes to paths supplied by the operating system. It reports write errors; it cannot detect a missing mount when the path remains locally writable. Provider messages that disappear before complete local intake are not protected by the work queue. An already accepted plan can finish without another provider download.

## Project documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Provider synchronization](docs/PROVIDER_SYNC_CONTRACTS.md)
- [Development and tests](docs/DEVELOPMENT.md)
- [Release procedure](docs/RELEASE.md)
- [Authentication](docs/AUTHENTICATION.md)
