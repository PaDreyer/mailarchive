import base64
import tempfile
import unittest
from pathlib import Path

from mailarchive.credentials import MemoryCredentialStore
from mailarchive.mail_sources import (
    MessageSourceRegistry,
    ProviderHttpError,
)
from mailarchive.models import (
    Account,
    AuthMode,
    Mailbox,
    MailProvider,
    Rule,
    Settings,
)
from mailarchive.service import ArchiveService
from mailarchive.storage import ArchiveState
from tests.helpers import sample_mail
from tests.test_mail_sources import FakeOAuth


class ScriptedHttp:
    """Reject unexpected requests, including accidental full listings on later runs."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def _request(self, kind, url, headers):
        self.calls.append((kind, url, headers))
        if not self.steps:
            raise AssertionError(f"Unexpected {kind} request: {url}")
        expected_kind, expected_url, response = self.steps.pop(0)
        if kind != expected_kind or expected_url not in url:
            raise AssertionError(f"Expected {expected_kind} {expected_url}, got {kind} {url}")
        if isinstance(response, Exception):
            raise response
        return response

    def get_json(self, url, access_token, headers=None):
        return self._request("json", url, headers)

    def get_bytes(self, url, access_token, headers=None):
        return self._request("bytes", url, headers)


def gmail_raw(message_id, *, subject="Invoice"):
    return (
        "json",
        f"/messages/{message_id}?format=raw",
        {
            "raw": base64.urlsafe_b64encode(sample_mail(subject=subject)).decode(),
            "labelIds": ["INBOX"],
            "internalDate": "1789948800000",
        },
    )


def gmail_metadata(message_id, labels=None):
    return ("json", f"/messages/{message_id}?format=minimal", {"labelIds": labels or ["INBOX"]})


def gmail_history(cursor, *, history=None, next_cursor="101", next_page=None):
    page = {"historyId": next_cursor}
    if history is not None:
        page["history"] = history
    if next_page is not None:
        page["nextPageToken"] = next_page
    return ("json", f"/history?startHistoryId={cursor}", page)


def graph_cursor(token, *, folder="INBOX", mailbox_root="/me", page=False):
    parameter = "$skiptoken" if page else "$deltatoken"
    return (
        f"https://graph.microsoft.com/v1.0{mailbox_root}/mailFolders/{folder}/messages/delta"
        f"?{parameter}={token}"
    )


def graph_delta(
    cursor,
    ids=(),
    *,
    next_cursor="next",
    next_page=None,
    folder="INBOX",
    mailbox_root="/me",
):
    page = {"value": [{"id": message_id} for message_id in ids]}
    if next_page:
        page["@odata.nextLink"] = graph_cursor(
            next_page, folder=folder, mailbox_root=mailbox_root, page=True
        )
    else:
        page["@odata.deltaLink"] = graph_cursor(
            next_cursor, folder=folder, mailbox_root=mailbox_root
        )
    expected = (
        cursor
        if "/messages/delta" in cursor
        else graph_cursor(cursor.removeprefix("/"), folder=folder, mailbox_root=mailbox_root)
    )
    return ("json", expected, page)


def graph_folder():
    return ("json", "/mailFolders/INBOX?$select=id", {"id": "folder-id"})


def graph_message(message_id, folder="folder-id"):
    return (
        "json",
        f"/messages/{message_id}?$select=parentFolderId,receivedDateTime",
        {"parentFolderId": folder, "receivedDateTime": "2026-09-21T00:00:00Z"},
    )


def graph_raw(message_id):
    return ("bytes", f"/messages/{message_id}/$value", sample_mail())


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = MemoryCredentialStore()
        self.state = ArchiveState(self.root / "workspace.sqlite3")

    def configure(self, provider, *, folders=None, rules=None):
        mailbox = Mailbox("me@example.org", folders=folders or ["INBOX"])
        self.account = Account(
            "Mailbox",
            "imap.example.org",
            mailbox.address,
            provider=provider,
            auth_mode=AuthMode.PASSWORD
            if provider == MailProvider.GENERIC_IMAP
            else AuthMode.OAUTH_USER,
            client_id="client",
            mailboxes=[mailbox],
        )
        self.credentials.set(self.account.id, "secret")
        self.settings = Settings(
            "",
            accounts=[self.account],
            rules=rules if rules is not None else [Rule("All", str(self.root / "Archive"))],
        )
        return mailbox

    def run_http(self, steps, *, manual=False, force_retry=False):
        http = ScriptedHttp(steps)
        registry = MessageSourceRegistry(self.credentials, http=http)
        registry.sources[self.account.provider].oauth = FakeOAuth()
        service = ArchiveService(self.credentials, self.state, source_registry=registry)
        if manual:
            result = service.run_range(self.settings, {self.account.mailboxes[0].id})[0]
        else:
            result = service.run_once(self.settings, force_retry=force_retry)[0]
        self.assertEqual(http.steps, [])
        return result, http

    def cursor(self, folder=None):
        mailbox = self.account.mailboxes[0]
        key = (
            "gmail-mailbox"
            if self.account.provider == MailProvider.GMAIL_API
            else folder or mailbox.folders[0]
        )
        scope = self.state.scope(mailbox.id, key)
        return scope["cursor"] if scope else None

    def test_gmail_baseline_skips_existing_then_history_archives_new_id(self):
        self.configure(MailProvider.GMAIL_API)
        baseline, _ = self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "old"}]}),
            ]
        )
        self.assertEqual((baseline.skipped_existing, baseline.archived), (1, 0))
        self.assertEqual(self.cursor(), "100")
        changed, http = self.run_http(
            [
                gmail_history("100", history=[{"messagesAdded": [{"message": {"id": "new"}}]}]),
                gmail_metadata("new"),
                gmail_raw("new"),
            ]
        )
        self.assertEqual((changed.archived, changed.failed), (1, 0))
        self.assertEqual(self.cursor(), "101")
        self.assertEqual(len(list((self.root / "Archive").glob("*.eml"))), 1)
        self.assertFalse(any("/messages?labelIds" in url for _, url, _ in http.calls))

    def test_malformed_gmail_message_does_not_block_later_history_ids(self):
        self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        history = [
            {
                "messagesAdded": [
                    {"message": {"id": "bad"}},
                    {"message": {"id": "good"}},
                ]
            }
        ]
        raw = base64.urlsafe_b64encode(sample_mail()).decode()

        result, _ = self.run_http(
            [
                gmail_history("100", history=history),
                gmail_metadata("bad"),
                ("json", "/messages/bad?format=raw", {"raw": raw, "labelIds": ["INBOX"]}),
                gmail_metadata("good"),
                gmail_raw("good"),
            ]
        )

        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.cursor(), "101")
        self.assertIn("internalDate", self.state.intake_errors()[0]["error"])
        self.assertEqual(len(list((self.root / "Archive").glob("*.eml"))), 1)

    def test_gmail_message_http_failure_does_not_block_later_history_ids(self):
        self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        history = [
            {
                "messagesAdded": [
                    {"message": {"id": "broken"}},
                    {"message": {"id": "later"}},
                ]
            }
        ]

        result, _ = self.run_http(
            [
                gmail_history("100", history=history),
                (
                    "json",
                    "/messages/broken?format=minimal",
                    ProviderHttpError(500, "message-specific failure"),
                ),
                gmail_metadata("later"),
                gmail_raw("later"),
            ]
        )

        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.cursor(), "101")
        self.assertIn("HTTP 500", self.state.intake_errors()[0]["error"])

    def test_gmail_throttling_remains_a_scan_failure(self):
        self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        history = [{"messagesAdded": [{"message": {"id": "blocked"}}]}]

        result, _ = self.run_http(
            [
                gmail_history("100", history=history),
                (
                    "json",
                    "/messages/blocked?format=minimal",
                    ProviderHttpError(429, "rate limited"),
                ),
            ]
        )

        self.assertEqual((result.archived, result.failed), (0, 1))
        self.assertEqual(self.cursor(), "100")
        self.assertIn("scan stopped", self.state.intake_errors()[0]["error"])

    def test_changed_gmail_labels_do_not_discard_an_unfinished_intake(self):
        mailbox = self.configure(MailProvider.GMAIL_API, folders=["A"])
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=A", {"messages": []}),
            ]
        )
        history = [{"messagesAdded": [{"message": {"id": "pending"}}]}]
        failed, _ = self.run_http(
            [
                gmail_history("100", history=history),
                gmail_metadata("pending", ["A"]),
                (
                    "json",
                    "/messages/pending?format=raw",
                    ProviderHttpError(503, "temporarily unavailable"),
                ),
            ]
        )
        self.assertEqual(failed.failed, 1)
        self.assertEqual(len(self.state.intake_errors()), 1)
        mailbox.folders = ["B"]

        recovered, _ = self.run_http(
            [
                gmail_raw("pending"),
                ("json", "/profile?fields=historyId", {"historyId": "101"}),
                ("json", "/messages?labelIds=B", {"messages": []}),
                gmail_history("101", history=[], next_cursor="101"),
            ],
            force_retry=True,
        )

        self.assertEqual((recovered.archived, recovered.failed), (1, 0))
        self.assertEqual(self.state.intake_errors(), [])
        self.assertEqual(self.state.processing_history()[0]["status"], "complete")

    def test_new_gmail_label_baselines_its_history_and_keeps_mailbox_cursor(self):
        mailbox = self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "old"}]}),
            ]
        )
        mailbox.folders.append("Project")
        changed, http = self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "101"}),
                ("json", "/messages?labelIds=Project", {"messages": [{"id": "project-old"}]}),
                gmail_history("100", history=[{"messagesAdded": [{"message": {"id": "new"}}]}]),
                gmail_metadata("new"),
                gmail_raw("new"),
            ]
        )
        self.assertEqual((changed.archived, changed.failed), (1, 0))
        self.assertEqual(self.cursor(), "101")
        self.assertIsNotNone(self.state.scope(mailbox.id, "gmail-label:Project"))
        self.assertEqual(len(list((self.root / "Archive").glob("*.eml"))), 1)
        self.assertTrue(any("/messages?labelIds=Project" in url for _, url, _ in http.calls))
        next_run, http = self.run_http([gmail_history("101", history=[])])
        self.assertEqual((next_run.archived, next_run.failed), (0, 0))
        self.assertFalse(any("/messages?labelIds" in url for _, url, _ in http.calls))

    def test_failed_new_gmail_label_baseline_stops_before_history(self):
        mailbox = self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        mailbox.folders.append("Project")
        result, http = self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "101"}),
                (
                    "json",
                    "/messages?labelIds=Project",
                    ProviderHttpError(503, "baseline unavailable"),
                ),
            ]
        )
        self.assertEqual((result.archived, result.failed), (0, 1))
        self.assertEqual(self.cursor(), "100")
        self.assertIsNone(self.state.scope(mailbox.id, "gmail-label:Project"))
        self.assertFalse(any("/history?" in url for _, url, _ in http.calls))

    def test_readded_gmail_label_gets_a_new_baseline(self):
        mailbox = self.configure(MailProvider.GMAIL_API, folders=["INBOX", "Project"])
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
                ("json", "/messages?labelIds=Project", {"messages": []}),
            ]
        )
        mailbox.folders = ["INBOX"]
        self.run_http([gmail_history("100", history=[])])
        self.assertIsNone(self.state.scope(mailbox.id, "gmail-label:Project"))
        mailbox.folders.append("Project")
        result, _ = self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "101"}),
                ("json", "/messages?labelIds=Project", {"messages": [{"id": "old"}]}),
                gmail_history("101", history=[]),
            ]
        )
        self.assertEqual((result.skipped_existing, result.failed), (1, 0))
        self.assertEqual(self.cursor(), "101")

    def test_gmail_duplicate_history_events_save_one_output(self):
        self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        history = [{"messagesAdded": [{"message": {"id": "new"}}, {"message": {"id": "new"}}]}]
        result, _ = self.run_http(
            [gmail_history("100", history=history), gmail_metadata("new"), gmail_raw("new")]
        )
        self.assertEqual(result.archived, 1)
        self.assertEqual(len(list((self.root / "Archive").glob("*.eml"))), 1)

    def test_interrupted_gmail_history_keeps_cursor_and_finished_receipt(self):
        self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": []}),
            ]
        )
        history = [{"messagesAdded": [{"message": {"id": "new"}}]}]
        result, _ = self.run_http(
            [
                gmail_history("100", history=history, next_page="p2"),
                gmail_metadata("new"),
                gmail_raw("new"),
                ("json", "pageToken=p2", ProviderHttpError(503, "unavailable")),
            ]
        )
        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.cursor(), "100")
        result, _ = self.run_http([gmail_history("100", history=history)])
        self.assertEqual(result.archived, 0)
        self.assertEqual(self.cursor(), "101")
        self.assertEqual(len(list((self.root / "Archive").glob("*.eml"))), 1)

    def test_manual_gmail_range_does_not_change_automatic_history_cursor(self):
        self.configure(MailProvider.GMAIL_API)
        self.run_http(
            [
                ("json", "/profile?fields=historyId", {"historyId": "100"}),
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "old"}]}),
            ]
        )
        result, _ = self.run_http(
            [
                ("json", "/messages?labelIds=INBOX", {"messages": [{"id": "old"}]}),
                gmail_raw("old"),
            ],
            manual=True,
        )
        self.assertEqual(result.archived, 1)
        self.assertEqual(self.cursor(), "100")

    def test_graph_delta_resumes_after_baseline(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="saved")])
        self.assertEqual(self.cursor(), graph_cursor("saved"))
        result, _ = self.run_http(
            [
                graph_delta("/saved", ["first"], next_cursor="next"),
                graph_folder(),
                graph_message("first"),
                graph_raw("first"),
            ]
        )
        self.assertEqual((result.archived, result.failed), (1, 0))
        self.assertEqual(self.cursor(), graph_cursor("next"))

    def test_malformed_graph_message_does_not_block_later_delta_ids(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="saved")])

        result, _ = self.run_http(
            [
                graph_delta("/saved", ["bad", "good"], next_cursor="next"),
                graph_folder(),
                (
                    "json",
                    "/messages/bad?$select=parentFolderId,receivedDateTime",
                    {"parentFolderId": "folder-id"},
                ),
                graph_message("good"),
                graph_raw("good"),
            ]
        )

        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.cursor(), graph_cursor("next"))
        self.assertIn("receivedDateTime", self.state.intake_errors()[0]["error"])

    def test_graph_message_http_failure_does_not_block_later_delta_ids(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="saved")])

        result, _ = self.run_http(
            [
                graph_delta("/saved", ["broken", "later"], next_cursor="next"),
                graph_folder(),
                (
                    "json",
                    "/messages/broken?$select=parentFolderId,receivedDateTime",
                    ProviderHttpError(500, "message-specific failure"),
                ),
                graph_message("later"),
                graph_raw("later"),
            ]
        )

        self.assertEqual((result.archived, result.failed), (1, 1))
        self.assertEqual(self.cursor(), graph_cursor("next"))
        self.assertIn("HTTP 500", self.state.intake_errors()[0]["error"])

    def test_graph_incomplete_delta_does_not_advance_cursor(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="saved")])
        result, _ = self.run_http([("json", graph_cursor("saved"), {"value": []})])
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.cursor(), graph_cursor("saved"))

    def test_graph_message_moved_out_of_selected_folder_is_discarded_cleanly(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="saved")])
        result, http = self.run_http(
            [
                graph_delta("/saved", ["moved"], next_cursor="next"),
                graph_folder(),
                graph_message("moved", folder="other-folder"),
            ]
        )
        self.assertEqual((result.archived, result.failed), (0, 0))
        self.assertEqual(self.cursor(), graph_cursor("next"))
        self.assertEqual(self.state.intake_errors(), [])
        self.assertFalse(any(kind == "bytes" for kind, _, _ in http.calls))

    def test_graph_pending_recheck_moved_out_releases_old_intake(self):
        self.configure(MailProvider.MICROSOFT_GRAPH)
        self.run_http([graph_delta("/messages/delta?", next_cursor="saved")])
        failed, _ = self.run_http(
            [
                graph_delta("/saved", ["moved"], next_cursor="not-committed"),
                graph_folder(),
                graph_message("moved"),
                ("bytes", "/messages/moved/$value", ProviderHttpError(503, "unavailable")),
            ]
        )
        self.assertEqual(failed.failed, 1)
        self.assertEqual(len(self.state.intake_errors()), 1)

        recovered, _ = self.run_http(
            [
                graph_delta("/not-committed", next_cursor="next"),
                graph_folder(),
                graph_message("moved", folder="other-folder"),
            ],
            force_retry=True,
        )
        self.assertEqual((recovered.archived, recovered.failed), (0, 0))
        self.assertEqual(self.state.intake_errors(), [])
        history = self.state.processing_history()
        self.assertEqual(history[0]["status"], "filtered")

    def test_new_graph_folder_gets_its_own_baseline(self):
        mailbox = self.configure(MailProvider.MICROSOFT_GRAPH, folders=["one", "two"])
        self.run_http(
            [
                graph_delta(
                    "/mailFolders/one/messages/delta?", next_cursor="one-saved", folder="one"
                ),
                graph_delta(
                    "/mailFolders/two/messages/delta?", next_cursor="two-saved", folder="two"
                ),
            ]
        )
        mailbox.folders.append("three")
        result, _ = self.run_http(
            [
                graph_delta("/one-saved", next_cursor="one-next", folder="one"),
                graph_delta("/two-saved", next_cursor="two-next", folder="two"),
                graph_delta(
                    "/mailFolders/three/messages/delta?",
                    ["old"],
                    next_cursor="three-saved",
                    folder="three",
                ),
            ]
        )
        self.assertEqual(result.skipped_existing, 1)
        self.assertEqual(self.cursor("one"), graph_cursor("one-next", folder="one"))
        self.assertEqual(self.cursor("two"), graph_cursor("two-next", folder="two"))
        self.assertEqual(self.cursor("three"), graph_cursor("three-saved", folder="three"))

    def test_readded_graph_folder_gets_a_fresh_baseline(self):
        mailbox = self.configure(MailProvider.MICROSOFT_GRAPH, folders=["one", "two"])
        self.run_http(
            [
                graph_delta(
                    "/mailFolders/one/messages/delta?", next_cursor="one-saved", folder="one"
                ),
                graph_delta(
                    "/mailFolders/two/messages/delta?", next_cursor="two-saved", folder="two"
                ),
            ]
        )

        mailbox.folders = ["one"]
        self.run_http([graph_delta("/one-saved", next_cursor="one-next", folder="one")])
        self.assertIsNone(self.state.scope(mailbox.id, "two"))

        mailbox.folders.append("two")
        result, http = self.run_http(
            [
                graph_delta("/one-next", next_cursor="one-final", folder="one"),
                graph_delta(
                    "/mailFolders/two/messages/delta?",
                    ["existing"],
                    next_cursor="two-rebased",
                    folder="two",
                ),
            ]
        )

        self.assertEqual((result.skipped_existing, result.archived, result.failed), (1, 0, 0))
        self.assertEqual(self.cursor("two"), graph_cursor("two-rebased", folder="two"))
        self.assertFalse(any(kind == "bytes" for kind, _, _ in http.calls))


if __name__ == "__main__":
    unittest.main()
