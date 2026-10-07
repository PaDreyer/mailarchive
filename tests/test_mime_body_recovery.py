"""Recovered MIME payloads retain their bytes through real archive publication."""

import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

from mailarchive.domain.configuration import RuleTarget, SaveMode
from mailarchive.domain.mail_parser import parse_mail
from tests import test_restart_core as restart
from tests.workspace_fixture import WorkspaceStore, make_service


def attachment_mail(headers: bytes, payload: bytes, *, newline: bytes = b"\r\n") -> bytes:
    outer = (
        b"From: sender@example.org\nTo: owner@example.org\nSubject: Recovery\n"
        b"MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=x\n\n"
    ).replace(b"\n", newline)
    return outer + b"--x" + newline + headers + payload + newline + b"--x--" + newline


class MimeBodyRecoveryTests(unittest.TestCase):
    def test_restored_envelope_survives_attached_mail_and_inline_mime_containers(self):
        envelope = b"From restored@example.org\r\n"
        inner = (
            b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=y\r\n"
            b'Content-Disposition: attachment; filename="inner.mime"\r\n\r\n'
            b"--y\r\nContent-Type: text/plain\r\n\r\nbody\r\n--y--"
        )
        for disposition in (b'attachment; filename="forwarded.eml"', b"inline"):
            with self.subTest(disposition=disposition):
                headers = (
                    b"Content-Type: message/rfc822\r\nContent-Disposition: "
                    + disposition
                    + b"\r\n"
                    + envelope
                    + b"\r\n"
                )
                raw = attachment_mail(headers, inner)
                native = BytesParser(policy=policy.default).parsebytes(raw)
                self.assertEqual(
                    native.get_payload()[0].get_payload()[0].get_unixfrom(),
                    envelope.decode().strip(),
                )
                parsed = parse_mail(raw)
                self.assertEqual(len(parsed.attachments), 1)
                self.assertEqual(parsed.attachments[0].content, envelope + inner)
                self.assertEqual(parsed.raw, raw)

    def test_original_payload_matches_native_envelope_recovery_and_separators(self):
        for newline in (b"\r\n", b"\n", b"\r"):
            headers = (
                b"Content-Type: application/octet-stream\n"
                b'Content-Disposition: attachment; filename="file.txt"\n'
                b"Content-Transfer-Encoding: 8bit\n"
            ).replace(b"\n", newline)
            envelope = b"From recovered payload" + newline
            body = b"second line\x00\xff"
            cases = {
                "ordinary_separator": (headers + newline, envelope + body),
                "missing_separator": (headers, envelope + body),
                "restored_before_separator": (headers + envelope + newline, body),
                "multiple_envelopes": (headers, envelope + envelope + body),
                "terminal_envelope": (headers, b"From final payload"),
                "real_envelope": (envelope + headers + newline, body),
                "misplaced_envelope": (
                    headers.replace(
                        b"Content-Transfer-Encoding:", envelope + b"Content-Transfer-Encoding:"
                    )
                    + newline,
                    body,
                ),
                "header_named_from": (headers + b"From: sender@example.org" + newline, body),
                "continuation": (headers + b" From continued header" + newline, body),
            }
            for name, (prefix, payload) in cases.items():
                with self.subTest(newline=newline, case=name):
                    raw = attachment_mail(prefix, payload, newline=newline)
                    native = BytesParser(policy=policy.default).parsebytes(raw).get_payload()[0]
                    parsed = parse_mail(raw)
                    self.assertEqual(parsed.raw, raw)
                    self.assertEqual(len(parsed.attachments), 1)
                    self.assertEqual(parsed.attachments[0].filename, "file.txt")
                    self.assertEqual(parsed.attachments[0].content, native.get_payload(decode=True))
                    if name == "missing_separator":
                        self.assertEqual(parsed.attachments[0].content, envelope + body)
                    elif name == "restored_before_separator":
                        self.assertEqual(parsed.attachments[0].content, envelope + body)

    def test_attachments_only_archive_and_restart_keep_complete_recovered_bytes(self):
        for separated in (False, True):
            with self.subTest(separated=separated):
                case = restart.RestartCoreTests()
                case.setUp()
                try:
                    destination = case.root / "recovered-attachments"
                    case.mailbox.archive_existing_messages = True
                    case.rule.targets = [
                        RuleTarget(
                            str(destination),
                            SaveMode.ATTACHMENTS_ONLY,
                            attachments_in_destination=True,
                        )
                    ]
                    headers = (
                        b"Content-Type: application/octet-stream\r\n"
                        b'Content-Disposition: attachment; filename="file.txt"\r\n'
                        b"Content-Transfer-Encoding: 8bit\r\n"
                    )
                    expected = b"From original payload\r\nsecond line\x00\xff"
                    raw = attachment_mail(headers + (b"\r\n" if separated else b""), expected)
                    case.source.messages["1"].raw = raw
                    result = case.service.run_once(case.settings)[0]
                    self.assertEqual((result.archived, result.failed), (1, 0))
                    files = list(destination.iterdir())
                    self.assertEqual(len(files), 1)
                    self.assertEqual(files[0].read_bytes(), expected)
                    with case.state.connection() as db:
                        plan = dict(db.execute("SELECT * FROM plan").fetchone())
                        receipt = dict(db.execute("SELECT * FROM receipt").fetchone())
                    self.assertEqual(plan["status"], "complete")
                    self.assertFalse(Path(plan["raw_path"]).exists())
                    reopened = WorkspaceStore(case.state.database_path, recover=True)
                    service = make_service(reopened, restart.Registry(case.source))
                    repeated = service.run_once(case.settings)[0]
                    self.assertEqual((repeated.archived, repeated.failed), (0, 0))
                    self.assertEqual(list(destination.iterdir()), files)
                    self.assertEqual(files[0].read_bytes(), expected)
                    with reopened.connection() as db:
                        self.assertEqual(
                            dict(db.execute("SELECT * FROM receipt").fetchone()), receipt
                        )
                finally:
                    case.tearDown()
