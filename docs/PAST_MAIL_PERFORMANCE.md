# Past-mail header preselection

Past-mail scans reject a message before creating a work copy only when available
headers disprove the frozen rule. Candidate messages still use the complete MIME
parser and matching logic. Missing, defective, duplicate, or truncated fields
remain unknown. AND rules can reject on one known false condition; OR rules need
every condition to be known false. Body and attachment conditions remain unknown.

Graph range pages select `id,receivedDateTime,from,subject` with the existing page
size of 999. The `from` address corresponds to MIME From, including delegated
messages; `sender` is not used. Old saved continuation URLs are followed unchanged,
with a single metadata GET when the page lacks reception metadata. Gmail adds
From, To, Cc, Bcc, Subject, and Date to its existing metadata request and parses
them using the same email policy as complete MIME messages. IMAP fetches size,
INTERNALDATE, and at most 64 KiB of headers for at most 100 UIDs in one command.
Intake reservation remains per message. Complete IMAP downloads request an extra
byte with the final data chunk to detect overflow, and require existence searches
only for missing or contradictory responses. Message and spool limits remain in
force for complete downloads.

Unmatched Activity records retain reception time and available subject/Date
metadata. Graph does not fetch the MIME Date header, so its sender timestamp
remains unknown. No profile migration or new user setting is needed. An already
running application uses the updated code after its next start.

## Verification and benchmark

`tests/test_past_mail_headers.py` runs real provider adapters and the real service,
SQLite repositories, spool, and output writer against counting fake servers. Each
provider scans 1,000 clearly nonmatching messages with no MIME downloads and no
spool writes. Further tests cover Unicode and all operators, AND/OR with unknown
body/attachments, frozen rules, malformed and truncated headers, delegated From,
legacy Graph metadata, pagination, OAuth renewal, Stop, missing UIDs, and IMAP
attributes following a header literal. Full-download and optimized executions
produce identical archive bytes, attachments, and reused receipts.

Run a transport-latency benchmark with:

```bash
.venv/bin/python -m tests.benchmark_past_mail --count 1000 --latency-ms 1
```

Measured on 2026-10-04 against the unchanged source at `be53562` and the updated
source, with identical fixtures and 1 ms of simulated latency per server command:

| Provider | Requests before → after | Metadata before → after | MIME downloads before → after | Spool writes before → after | Wall time before → after |
| --- | ---: | ---: | ---: | ---: | ---: |
| Microsoft Graph | 2,002 → 2 | 1,000 → 0 | 1,000 → 0 | 1,000 → 0 | 5.480 s → 2.310 s |
| Gmail | 2,002 → 1,002 | 1,000 → 1,000 | 1,000 → 0 | 1,000 → 0 | 5.481 s → 3.911 s |
| IMAP | 4,001 → 11 | 1,000 → 10 batches | 1,000 → 0 | 1,000 → 0 | 8.294 s → 3.128 s |

Request counts include listing/SEARCH; IMAP login and SELECT are unchanged and
excluded. Wall time includes real local SQLite writes and parsing of simulated
responses. These are local comparisons, not measurements of live provider speed;
network latency, bandwidth, message sizes, and local storage affect real runs.

For a reproducible comparison, run the same benchmark module with a pristine
baseline `src` snapshot first on `PYTHONPATH`, and then with the working source.
No real mailbox, stored credentials, or running scan is accessed by the benchmark.
