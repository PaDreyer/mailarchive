# Provider synchronization contracts

The synchronization audit checks the complete path from the addressed mailbox and authorization
context, through request parameters and response fields, to the persisted SQLite checkpoint.
`tests/test_provider_contracts.py` executes these contracts through the real adapters, service,
and SQLite database. Scripted protocol responses make incorrect or additional requests fail.

## Request, response, and storage mapping

| Provider | Initial request | Incremental request | Required response and saved checkpoint | Message identity |
| --- | --- | --- | --- | --- |
| Gmail API | `GET /gmail/v1/users/{mailbox-address}/profile?fields=historyId`, followed by `/messages?maxResults=500&includeSpamTrash=true` with one `labelIds` filter for each selected label | `/history?startHistoryId={saved-historyId}&maxResults=500`, following `nextPageToken` | A positive ASCII decimal string `historyId`. The initial checkpoint is captured **before** listing. A later check saves the final history page's ID after all downloads and targeted rechecks complete. History must not regress below its starting ID. | Addressed mailbox plus the provider's nonempty string message `id`; one history checkpoint covers the union of selected labels. |
| Microsoft Graph | `GET /v1.0/{mailbox-root}/mailFolders/{folder}/messages/delta?$select=id&$top=999` | The entire previously saved `@odata.deltaLink` URL, unchanged | Every intermediate `@odata.nextLink` is followed unchanged. A final `@odata.deltaLink` is required. Both fields cannot occur on one page. Only the final link is persisted after all downloads and targeted rechecks complete. | Addressed mailbox plus the immutable message `id`; a separate delta checkpoint covers each physical folder. |
| IMAP | Read-only `EXAMINE {quoted-folder}`, then `UID SEARCH ALL` | `UID SEARCH UID {saved-UID + 1}:*`; filter the reversed-range edge case and never wrap beyond `4294967295` | `UIDVALIDITY` and returned UIDs must be nonzero unsigned 32-bit ASCII numbers. Save the highest enumerated UID in its UIDVALIDITY namespace, including initially excluded mail. An empty folder saves `0` as the local checkpoint. | Server, port, addressed mailbox, folder, UIDVALIDITY, and UID; a UID is never treated as a mailbox-wide ID. |

For Gmail, multiple label filters on a single list request mean intersection. Separate requests
and local ID deduplication implement the configured union. Incremental synchronization consumes
`messagesAdded` and `labelsAdded`, then verifies current membership with
`format=minimal&fields=labelIds`. Downloads use `format=raw&fields=raw,labelIds` and validated
base64url MIME data. Empty repeated fields may be absent; an explicitly malformed list is an
error. Gmail history IDs are increasing and can contain gaps; the code reuses the returned
value and does not calculate the next ID.

Graph sends `Prefer: IdType="ImmutableId"` on folder discovery, delta, metadata, and MIME
requests. It does not extract or rebuild opaque skip/delta tokens. All returned links must
stay on the Graph HTTPS origin and configured API path before receiving authorization.
Current membership is verified through `parentFolderId`; downloads use `$value` with
`Accept: message/rfc822`. Folder discovery includes hidden folders, follows pagination and
children, and excludes virtual search folders. Physical mailbox-wide IDs prevent duplicates
when messages move between selected folders.

IMAP uses `BODY.PEEK[]` and never modifies the server's read flags. Missing UIDVALIDITY causes
exactly one new read-only selection. If it remains absent, or if a returned validity value is
invalid, that folder fails before any UID lookup or processing-history comparison. There is
no `unknown` epoch and no date-based fallback. Cached UIDVALIDITY response codes are also
checked after search and fetch commands: a changed epoch stops the current folder before
further messages can be associated with the old identity. A subsequent check opens the folder
and establishes the new epoch normally.

## Authorization and mailbox addressing

| Authentication | Address and token mapping |
| --- | --- |
| IMAP password | Authenticate the connection's `username`; configuration permits only that user's mailbox. Folder selection remains independent. |
| Microsoft delegated IMAP OAuth | Obtain the connection user's token with `IMAP.AccessAsUser.All`; the XOAUTH2 `user=` field is the **target mailbox address**, including a shared mailbox. The Microsoft endpoint is fixed to `outlook.office365.com:993` with TLS. |
| Google user OAuth | Request `gmail.readonly`; configuration permits the connection user's mailbox. Every Gmail request explicitly addresses that mailbox rather than an ambiguous `me`. |
| Google Workspace domain-wide delegation | Load the connection's one service-account credential and call `with_subject(target-mailbox-address)` with `gmail.readonly` for each mailbox. |
| Microsoft Graph delegated OAuth | Request `Mail.Read` and also `Mail.Read.Shared` when enabled targets include another mailbox. Use `/me` for the connection user's mailbox and `/users/{target-address}` for other mailboxes. |
| Microsoft Graph application OAuth | Acquire the connection's application token with `https://graph.microsoft.com/.default`; use `/users/{target-address}` for every mailbox. No human sign-in username is required. |

Synchronization checkpoints include an authentication/selection binding. Changing the provider,
authentication mode, sign-in identity, client, tenant, target mailbox, or selected folders
forces reconciliation with existing processing history. An incompatible checkpoint is not
reused. Message-processing identity remains tied to the physical mailbox, allowing connections
to share duplicate-prevention evidence without sharing authorization or baseline preferences.

## Check timestamps and failure behavior

SQLite schema 6 adds `mailbox_check`, keyed by connection account and addressed mailbox. It
stores `started_at`, `finished_at`, `status` (`running`, `success`, or `failed`), `error`, and
`last_successful_at` in UTC. A check starts before folder discovery and finishes after all its
selected scopes have been attempted. Only complete mailbox success updates
`last_successful_at`. A failed first check records the attempt and error without inventing a
success timestamp or a cursor. An interrupted process leaves an unfinished `running` attempt.

These records are independent of `synchronization_checkpoint`. Its `checked_at` continues to
mean the time that particular technical scope committed its cursor. A successful folder may
commit while another folder fails; the mailbox then records failure and retains its previous
successful-check timestamp. Other mailboxes continue. Disabled mailboxes are not checked.

Malformed message IDs, response collections, label IDs, cursor fields, or repeating pages fail
the scope. An adapter that finishes without a candidate cursor cannot mark a scan complete.
Gmail history expiration (HTTP 404) and Graph invalid/expired delta state restart one full
reconciliation with the existing processing history. Other errors retain the checkpoint.
Successfully processed messages remain durable and are skipped during replay; failed work is
retried. Copying a database preserves both checkpoints and check records. Merging databases
retains the latest attempt and latest successful-check evidence while invalidating remote
cursors and availability state for a safe reconciliation.

## Validation evidence

`tests/test_provider_contracts.py` covers all supported authentication modes' native cursor
mapping; explicit mailbox roots and request limits; unchanged opaque Graph URLs after restart;
IMAP validity recovery, rejection, epoch changes during search/download, UID boundaries, and
per-mailbox OAuth authorization; malformed initial and incremental responses; repeating pages;
missing completion values; and persisted attempts, successes, partial failures, archive errors,
restarts, schema-5 upgrades, copies, and merges.

`tests/test_synchronization.py` additionally covers normal incremental runs without historical
listings, pagination, changes during initial scans, expired-token reconciliation, duplicate
changes, rule retries, backfill, deleted/moved messages, processing failures, and atomic cursor
commits. `tests/test_mailbox_architecture.py` verifies multiple mailboxes per connection,
Google impersonation subjects, Microsoft permission selection, mailbox-wide identities,
folder discovery, cross-folder backfill, selection changes, and history adoption.

## Primary specifications

- [Gmail messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list): mailbox addressing, list limits, pagination, and label intersection.
- [Gmail users.getProfile](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile): the mailbox's current history ID.
- [Gmail history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list): starting history IDs, change records, pagination, and expiry.
- [Google service-account delegation](https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority): authorizing requests on behalf of each Workspace user.
- [Microsoft message delta](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0): folder-scoped routes and supported initial query parameters.
- [Microsoft delta query](https://learn.microsoft.com/en-us/graph/delta-query-overview): opaque continuation/state URLs and mutually exclusive next/delta links.
- [Microsoft immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id): request headers and ID behavior when moving messages.
- [Microsoft shared mail folders](https://learn.microsoft.com/en-us/graph/outlook-share-messages-folders): delegated mailbox addressing and shared-read permissions.
- [Microsoft IMAP OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth): connection token and shared-mailbox XOAUTH2 identity.
- [IMAP4rev2 RFC 9051](https://www.rfc-editor.org/rfc/rfc9051.html#section-6.3.3) and [IMAP4rev1 RFC 3501](https://www.rfc-editor.org/rfc/rfc3501.html#section-6.3.2): selection responses, UIDVALIDITY, UID identity, and read-only access.
