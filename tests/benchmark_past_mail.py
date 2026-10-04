"""Run via python -m tests.benchmark_past_mail with either source revision on PYTHONPATH."""

import argparse
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Condition,
    Mailbox,
    MailField,
    MailProvider,
    MatchOperator,
    Rule,
    RuleTarget,
    Settings,
)
from tests.helpers import sample_mail
from tests.past_mail_fixture import provider_source
from tests.test_restart_core import Registry
from tests.workspace_fixture import WorkspaceStore, make_service


def benchmark(provider, count, latency):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        mailbox = Mailbox("owner@example.org", ["INBOX"])
        account = Account(
            "Owner",
            "imap.example.org",
            mailbox.address,
            provider=provider,
            auth_mode=AuthMode.PASSWORD
            if provider == MailProvider.GENERIC_IMAP
            else AuthMode.OAUTH_USER,
            client_id="test-client",
            mailboxes=[mailbox],
        )
        raw = sample_mail(sender="other@example.org")
        source, server = provider_source(
            provider, account, {str(uid): raw for uid in range(1, count + 1)}, latency=latency
        )
        state = WorkspaceStore(root / "profile" / "workspace.sqlite3")
        service = make_service(state, Registry(source))
        rule = Rule(
            "Selected",
            conditions=[Condition(MailField.SENDER, MatchOperator.EQUALS, "wanted@example.org")],
            targets=[RuleTarget(str(root / "archive"))],
        )
        started = time.perf_counter()
        with patch.object(state.spool, "stage", wraps=state.spool.stage) as stage:
            result = service.run_range(
                Settings(accounts=[account], rules=[rule]), {mailbox.id}, rule_id=rule.id
            )[0]
        elapsed = time.perf_counter() - started
        if provider == MailProvider.GENERIC_IMAP:
            requests = len(server.calls)
            metadata = sum("RFC822.SIZE" in str(call[-1]) for call in server.calls)
            downloads = sum("BODY.PEEK[]<0." in str(call[-1]) for call in server.calls)
        else:
            requests = len(server.requests)
            metadata = sum(
                "/messages/" in url and not ("format=raw" in url or "/$value" in url)
                for url in server.requests
            )
            downloads = len(server.downloads)
        assert (result.unmatched, result.archived, result.failed) == (count, 0, 0), result
        assert list(state.spool_dir.iterdir()) == []
        return {
            "provider": provider.value,
            "messages": count,
            "latency_ms": latency * 1000,
            "requests": requests,
            "metadata_requests": metadata,
            "mime_downloads": downloads,
            "spool_writes": stage.call_count,
            "elapsed_seconds": round(elapsed, 3),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--latency-ms", type=float, default=1)
    arguments = parser.parse_args()
    for provider in (
        MailProvider.MICROSOFT_GRAPH,
        MailProvider.GMAIL_API,
        MailProvider.GENERIC_IMAP,
    ):
        print(
            json.dumps(benchmark(provider, arguments.count, arguments.latency_ms / 1000)),
            flush=True,
        )


if __name__ == "__main__":
    main()
