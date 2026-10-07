"""Counting provider servers for past-mail integration tests and latency benchmarks."""

from __future__ import annotations

import re
import time
from email import policy
from email.parser import BytesHeaderParser
from urllib.parse import parse_qs, unquote, urlsplit

from mailarchive.application.account_credentials import store_account_credentials
from mailarchive.domain.configuration import MailProvider
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.providers.gmail import GmailMessageSource
from mailarchive.infrastructure.providers.graph import MicrosoftGraphMessageSource
from mailarchive.infrastructure.providers.imap import ImapMessageSource
from mailarchive.infrastructure.providers.imap_client import ImapMailbox
from tests.test_mail_sources import FakeOAuth


class CountingHttp:
    def __init__(self, messages: dict[str, bytes], *, latency: float = 0):
        self.messages = messages
        self.latency = latency
        self.requests: list[str] = []
        self.downloads: list[str] = []
        self.metadata_override: dict[str, dict] = {}
        self.omit_list_metadata = False
        self.failures: dict[str, Exception] = {}

    def record(self, url):
        self.requests.append(url)
        if self.latency:
            time.sleep(self.latency)
        if url in self.failures:
            raise self.failures.pop(url)

    def metadata(self, remote_id):
        message = BytesHeaderParser(policy=policy.default).parsebytes(self.messages[remote_id])
        return {
            "id": remote_id,
            "receivedDateTime": "2026-09-21T00:00:00Z",
            "parentFolderId": "inbox-id",
            "from": {"emailAddress": {"address": message["From"].addresses[0].addr_spec}},
            "subject": str(message.get("Subject", "")),
            "internalDate": "1789948800000",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": name, "value": re.sub(r"\r?\n[ \t]+", " ", value)}
                    for name, value in message.raw_items()
                    if name.lower() in {"from", "to", "cc", "bcc", "subject", "date"}
                ]
            },
        } | self.metadata_override.get(remote_id, {})

    def get_json(self, url, _token, headers=None, *, cancellation=None):
        if cancellation:
            cancellation.checkpoint()
        self.record(url)
        parsed = urlsplit(url)
        parameters = parse_qs(parsed.query)
        folder = re.search(r"/mailFolders/([^/]+)$", parsed.path)
        if folder:
            return {"id": "inbox-id"}
        match = re.search(r"/messages/([^/]+)$", parsed.path)
        if match:
            remote_id = unquote(match[1])
            return self.metadata(remote_id)
        graph = parsed.hostname == "graph.microsoft.com"
        page_size = int(
            parameters.get("$top" if graph else "maxResults", [999 if graph else 500])[0]
        )
        start = int(parameters.get("$skiptoken" if graph else "pageToken", [0])[0])
        ids = list(self.messages)[start : start + page_size]
        if graph:
            complete = "from" in parameters.get("$select", [""])[0] and not self.omit_list_metadata
            result = {"value": [self.metadata(uid) if complete else {"id": uid} for uid in ids]}
            if start + page_size < len(self.messages):
                result["@odata.nextLink"] = (
                    f"https://graph.microsoft.com{parsed.path}?$top={page_size}"
                    f"&$select={parameters.get('$select', ['id'])[0]}&$skiptoken={start + page_size}"
                )
        else:
            result = {"messages": [{"id": uid} for uid in ids]}
            if start + page_size < len(self.messages):
                result["nextPageToken"] = str(start + page_size)
        return result

    def iter_bytes(self, url, _token, headers=None, *, cancellation=None, **_kwargs):
        if cancellation:
            cancellation.checkpoint()
        self.record(url)
        remote_id = unquote(urlsplit(url).path.split("/messages/", 1)[1].split("/", 1)[0])
        self.downloads.append(remote_id)
        yield self.messages[remote_id]

    def iter_gmail_raw(self, url, token, headers=None, *, cancellation=None):
        yield from self.iter_bytes(url, token, cancellation=cancellation)


class CountingImap:
    def __init__(self, messages: dict[str, bytes], *, latency: float = 0):
        self.messages = {uid.encode(): raw for uid, raw in messages.items()}
        self.latency = latency
        self.calls: list[tuple] = []
        self.metadata_override: dict[bytes, bytes] = {}
        self.closed = False
        self.logged_out = False

    def login(self, *_args):
        return "OK", [b""]

    def select(self, *_args, **_kwargs):
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        return name, [b"9001"]

    def fetch(self, sequence, attributes):
        assert sequence in {"1", "*"} and attributes == "(UID)"
        self.calls.append(("sequence_fetch", sequence, attributes))
        if self.latency:
            time.sleep(self.latency)
        uids = sorted(self.messages, key=int)
        if not uids:
            return "OK", [None]
        position = len(uids) if sequence == "*" else 1
        uid = uids[-1] if sequence == "*" else uids[0]
        return "OK", [str(position).encode() + b" (UID " + uid + b")"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if self.latency:
            time.sleep(self.latency)
        if command == "search":
            window = re.match(r"UID ([0-9]+):([0-9]+)(?: |$)", args[-1])
            if window:
                lower, upper = map(int, window.groups())
                return "OK", [b" ".join(uid for uid in self.messages if lower <= int(uid) <= upper)]
            wanted = set(args[-1][4:].encode().split(b",")) if args[-1].startswith("UID ") else None
            return "OK", [
                b" ".join(uid for uid in self.messages if wanted is None or uid in wanted)
            ]
        response = []
        for uid in args[0].split(b","):
            if uid not in self.messages:
                continue
            raw = self.messages[uid]
            if "RFC822.SIZE" in args[-1]:
                attributes = self.metadata_override.get(
                    uid,
                    (
                        b" RFC822.SIZE "
                        + str(len(raw)).encode()
                        + b' INTERNALDATE "21-Sep-2026 00:00:00 +0000"'
                    ),
                )
                prefix = b"1 (UID " + uid + attributes
                if "BODY.PEEK[HEADER]" in args[-1]:
                    end = re.search(rb"\r?\n\r?\n", raw)
                    header = raw[: end.end() if end else len(raw)][:65536]
                    response.extend(
                        [
                            (
                                prefix + b" BODY[HEADER]<0> {" + str(len(header)).encode() + b"}",
                                header,
                            ),
                            b")",
                        ]
                    )
                else:
                    response.append(prefix + b")")
            else:
                match = re.search(r"<([0-9]+)\.([0-9]+)>", args[-1])
                start, count = map(int, match.groups())
                chunk = raw[start : start + count]
                response.extend(
                    [
                        (
                            b"1 (UID "
                            + uid
                            + b" BODY[]<"
                            + str(start).encode()
                            + b"> {"
                            + str(len(chunk)).encode()
                            + b"}",
                            chunk,
                        ),
                        b")",
                    ]
                )
        return "OK", response

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


def provider_source(provider, account, messages, *, latency=0):
    """Use real adapters; only transport and authorization are simulated."""
    if provider == MailProvider.MICROSOFT_GRAPH:
        server = CountingHttp(messages, latency=latency)
        return MicrosoftGraphMessageSource(FakeOAuth(), server), server
    if provider == MailProvider.GMAIL_API:
        server = CountingHttp(messages, latency=latency)
        return GmailMessageSource(FakeOAuth(), server), server
    server = CountingImap(messages, latency=latency)
    mailbox = ImapMailbox()
    mailbox._connect = lambda *_args, **_kwargs: server
    credentials = MemoryCredentialStore()
    store_account_credentials(credentials, account, {"password": "test-password"})
    return ImapMessageSource(credentials, mailbox), server
