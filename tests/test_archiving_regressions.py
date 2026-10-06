"""MIME boundaries, preparation retries, and portable physical archive names."""

import base64
import os
import quopri
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import patch

from mailarchive.application.activity import ActivityQueries
from mailarchive.application.source_port import RemoteMessage
from mailarchive.domain.archive_paths import bounded_filename
from mailarchive.domain.configuration import Attachment, Condition, MailField, Rule, SaveMode
from mailarchive.domain.mail_parser import parse_mail
from mailarchive.domain.rules import rule_matches
from mailarchive.infrastructure.activity_repository import SqliteActivityRepository
from tests import test_restart_core as restart
from tests.helpers import sample_mail
from tests.workspace_fixture import WorkspaceStore, make_service


class ArchivingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = restart.RestartCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.mailbox.archive_existing_messages = True
        self.original = self.fixture.source.messages["1"]

    def message(self, raw, number=1):
        self.fixture.source.messages = {
            str(number): RemoteMessage(
                str(number), raw, self.original.received_at, self.original.received_origin
            )
        }

    def test_attached_message_is_saved_whole_without_affecting_outer_body_rules(self):
        inner = EmailMessage()
        inner["Subject"] = "Attached original"
        inner.set_content("INNER-ONLY")
        inner.add_attachment(
            b"inner pdf", maintype="application", subtype="pdf", filename="inner.pdf"
        )
        outer = EmailMessage()
        outer["Subject"] = "Outer mail"
        outer.set_content("Outer body")
        outer.add_attachment(inner, filename="original.eml")
        self.message(outer.as_bytes())
        parsed = parse_mail(outer.as_bytes())
        self.assertEqual([part.filename for part in parsed.attachments], ["original.eml"])
        self.assertNotIn("INNER-ONLY", parsed.body)
        self.assertTrue(
            rule_matches(
                Rule("Attachments", conditions=[Condition(MailField.HAS_ATTACHMENT, value="yes")]),
                parsed,
            )
        )
        self.assertFalse(
            rule_matches(
                Rule("Inner body", conditions=[Condition(MailField.BODY, value="INNER-ONLY")]),
                parsed,
            )
        )
        self.fixture.rule.targets[0].save_mode = SaveMode.ATTACHMENTS_ONLY
        result = self.fixture.service.run_once(self.fixture.settings)[0]
        self.assertEqual((result.archived, result.skipped_no_attachments, result.failed), (1, 0, 0))
        files = list((self.fixture.root / "A").rglob("*.eml"))
        self.assertEqual(len(files), 1)
        saved = BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
        self.assertEqual(saved["Subject"], "Attached original")
        self.assertIn("INNER-ONLY", saved.get_body().get_content())
        self.assertEqual(next(saved.iter_attachments()).get_payload(decode=True), b"inner pdf")
        self.assertEqual(list(self.fixture.state.spool_dir.iterdir()), [])

    def test_unnamed_message_and_named_multipart_attachments_are_not_flattened(self):
        for kind in ("message", "multipart"):
            with self.subTest(kind=kind):
                inner = EmailMessage()
                inner.set_content("Hidden attachment text")
                inner.add_attachment(
                    b"pdf", maintype="application", subtype="pdf", filename="inner.pdf"
                )
                outer = EmailMessage()
                outer.set_content("Outer body")
                if kind == "message":
                    outer.add_attachment(inner)
                else:
                    inner.add_header("Content-Disposition", "attachment", filename="bundle.mime")
                    outer.make_mixed()
                    outer.attach(inner)
                parsed = parse_mail(outer.as_bytes())
                self.assertEqual(len(parsed.attachments), 1)
                self.assertNotIn("Hidden attachment text", parsed.body)
                saved = BytesParser(policy=policy.default).parsebytes(parsed.attachments[0].content)
                self.assertTrue(saved.is_multipart())
                self.assertEqual(next(saved.iter_attachments()).get_payload(decode=True), b"pdf")

    def test_named_inline_text_attachment_is_excluded_from_outer_body(self):
        mail = EmailMessage()
        mail.set_content("Outer body")
        mail.add_attachment(
            "Attachment text", subtype="plain", filename="note.txt", disposition="inline"
        )
        parsed = parse_mail(mail.as_bytes())
        self.assertEqual(len(parsed.attachments), 1)
        self.assertNotIn("Attachment text", parsed.body)

    def test_international_message_encodings_preserve_original_headers_body_and_attachments(self):
        self.fixture.rule.targets[0].save_mode = SaveMode.ATTACHMENTS_ONLY
        inner = EmailMessage(policy=policy.SMTPUTF8)
        inner["From"] = "inner@example.org"
        inner["Subject"] = "Original ä"
        inner["X-Long"] = "x" * 170
        inner.set_content("Original body ä\nSecond line", cte="8bit")
        inner.add_attachment(
            b"pdf\x00\xff", maintype="application", subtype="pdf", filename="inner.pdf"
        )
        original = inner.as_bytes()
        for number, encoding in enumerate(("base64", "quoted-printable", "8bit"), 1):
            with self.subTest(encoding=encoding):
                wire = {
                    "base64": base64.b64encode(original),
                    "quoted-printable": quopri.encodestring(original),
                    "8bit": original,
                }[encoding]
                raw = (
                    b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
                    b"--x\r\nContent-Type: text/plain\r\n\r\nOuter body\r\n"
                    b"--x\r\nContent-Type: message/global\r\n"
                    b"Content-Disposition: attachment; filename=original.eml\r\n"
                    + f"Content-Transfer-Encoding: {encoding}\r\n\r\n".encode()
                    + wire
                    + b"\r\n--x--\r\n"
                )
                parsed = parse_mail(raw)
                self.assertEqual(parsed.attachments[0].content, original)
                self.assertNotIn("Original body", parsed.body)
                self.message(raw, number)
                before = set((self.fixture.root / "A").rglob("*.eml"))
                result = self.fixture.service.run_once(self.fixture.settings)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))
                after = set((self.fixture.root / "A").rglob("*.eml"))
                saved = (after - before).pop()
                self.assertEqual(saved.read_bytes(), original)

    def test_old_flattened_message_outputs_resume_without_losing_their_manifest(self):
        inner = EmailMessage()
        inner["Subject"] = "Attached original"
        inner.set_content("Inner text")
        inner.add_attachment(b"pdf", maintype="application", subtype="pdf", filename="inner.pdf")
        outer = EmailMessage()
        outer.set_content("Outer text")
        outer.add_attachment(inner, filename="original.eml")
        raw = outer.as_bytes()
        legacy = replace(parse_mail(raw), attachments=[Attachment("inner.pdf", b"pdf")])
        self.message(raw)
        self.fixture.rule.targets[0].save_mode = SaveMode.ATTACHMENTS_ONLY
        with (
            patch("mailarchive.domain.archive_plan.parse_mail", return_value=legacy),
            patch.object(
                self.fixture.service.engine.output_files,
                "publish",
                side_effect=OSError("Destination offline"),
            ),
        ):
            self.assertEqual(self.fixture.service.run_once(self.fixture.settings)[0].failed, 1)
        plan = self.fixture.state.open_plans()[0]
        original_output = dict(self.fixture.state.outputs(plan["id"])[0])
        restarted = WorkspaceStore(self.fixture.state.database_path)
        service = make_service(restarted, restart.Registry(self.fixture.source))
        self.assertEqual(service.retry_activity("mail:" + plan["id"]), (2, 0))
        outputs = restarted.outputs(plan["id"])
        self.assertEqual(outputs[0]["id"], original_output["id"])
        self.assertEqual(outputs[0]["requested_path"], original_output["requested_path"])
        self.assertTrue(all(output["status"] == "done" for output in outputs))
        self.assertEqual(restarted.open_plans(), [])
        self.assertEqual(len(list((self.fixture.root / "A").rglob("*.pdf"))), 1)
        self.assertEqual(len(list((self.fixture.root / "A").rglob("*.eml"))), 1)

    def test_nested_mime_wire_boundaries_preserve_original_message_bytes(self):
        for newline in (b"\r\n", b"\n", b"\r"):
            for closed in (True, False):
                with self.subTest(newline=newline, closed=closed):
                    original = newline.join(
                        [
                            b"Subject: Original",
                            b"X-Folded: first",
                            b" second",
                            b"",
                            b"Original body",
                            b"",
                            b"",
                        ]
                    )
                    outer = newline.join(
                        [
                            b"Content-Type: multipart/mixed; boundary=outer",
                            b"",
                            b"Preamble",
                            b"--outer \t",
                            b"Content-Type: multipart/alternative; boundary=inner",
                            b"",
                            b"--inner",
                            b"Content-Type: text/plain",
                            b"",
                            b"Outer body",
                            b"--inner",
                            b"Content-Type: text/html",
                            b"",
                            b"<b>Outer body</b>",
                            b"--inner--",
                            b"--outer\t",
                            b"Content-Type: message/rfc822",
                            b"Content-Disposition: attachment; filename=original.eml",
                            b"",
                            b"",
                        ]
                    )
                    suffix = newline + b"--outer-- \t" + newline + b"Epilogue" if closed else b""
                    parsed = parse_mail(outer + original + suffix)
                    self.assertEqual(parsed.attachments[0].content, original)
                    self.assertEqual(parsed.body.strip(), "Outer body")

    def fail_preparation(self):
        with patch.object(
            self.fixture.service.engine.output_files,
            "occupied",
            side_effect=PermissionError("Destination unavailable"),
        ):
            result = self.fixture.service.run_once(self.fixture.settings)[0]
        self.assertEqual(result.failed, 1)
        plan = self.fixture.state.open_plans()[0]
        self.assertEqual(self.fixture.state.outputs(plan["id"]), [])
        queries = ActivityQueries(SqliteActivityRepository(self.fixture.state.connection))
        self.assertTrue(queries.current()[0].can_retry)
        with self.fixture.state.connection() as db:
            intake = dict(db.execute("SELECT * FROM intake WHERE status='accepted'").fetchone())
        self.assertEqual(intake["attempts"], 0)
        self.assertIsNotNone(intake["retry_after"])
        return plan, intake

    def test_headerless_digest_members_keep_their_body_and_attachments(self):
        inner = EmailMessage(policy=policy.SMTP)
        inner["From"] = "inner@example.org"
        inner["Subject"] = "Digest member"
        inner.set_content("Member body")
        inner.add_attachment(
            b"PDF DATA", maintype="application", subtype="pdf", filename="member.pdf"
        )
        number = 0
        for newline in (b"\r\n", b"\n", b"\r"):
            for mode in (SaveMode.EMAIL_ONLY, SaveMode.ATTACHMENTS_ONLY):
                with self.subTest(newline=newline, mode=mode):
                    number += 1
                    raw = (
                        newline.join(
                            [
                                b"Content-Type: multipart/digest; boundary=digest",
                                b"",
                                b"--digest",
                                b"",
                                b"",
                            ]
                        )
                        + inner.as_bytes().replace(b"\r\n", newline)
                        + newline
                        + b"--digest--"
                        + newline
                    )
                    parsed = parse_mail(raw)
                    self.assertIn("Member body", parsed.body)
                    self.assertEqual(parsed.attachments, [Attachment("member.pdf", b"PDF DATA")])
                    self.message(raw, number)
                    self.fixture.rule.targets[0].save_mode = mode
                    before = {p for p in (self.fixture.root / "A").rglob("*") if p.is_file()}
                    result = self.fixture.service.run_once(self.fixture.settings)[0]
                    self.assertEqual((result.archived, result.failed), (1, 0))
                    after = {p for p in (self.fixture.root / "A").rglob("*") if p.is_file()}
                    (saved,) = after - before
                    self.assertEqual(
                        saved.read_bytes(), raw if mode == SaveMode.EMAIL_ONLY else b"PDF DATA"
                    )

    def test_missing_header_separator_preserves_recovered_attachment_bytes(self):
        original = b"BODY-WITHOUT-BLANK-LINE\n\x00\xff"
        for newline in (b"\r\n", b"\n", b"\r"):
            for encoding in ("base64", "quoted-printable", "8bit"):
                with self.subTest(newline=newline, encoding=encoding):
                    payload = {
                        "base64": base64.b64encode(original),
                        "quoted-printable": quopri.encodestring(original),
                        "8bit": original,
                    }[encoding]
                    raw = (
                        newline.join(
                            [
                                b"Content-Type: multipart/mixed; boundary=x",
                                b"",
                                b"--x",
                                b"Content-Type: application/octet-stream",
                                b"Content-Disposition: attachment; filename=data.bin",
                                f"Content-Transfer-Encoding: {encoding}".encode(),
                                b"",
                            ]
                        )
                        + payload
                        + newline
                        + b"--x--"
                        + newline
                    )
                    self.assertEqual(
                        parse_mail(raw).attachments, [Attachment("data.bin", original)]
                    )

    def test_preparation_failure_can_be_retried_locally_after_account_pause(self):
        plan, intake = self.fail_preparation()
        self.fixture.account.enabled = False
        self.fixture.state.save_settings(self.fixture.settings)
        self.assertFalse(self.fixture.service.has_automatic_work(self.fixture.settings))
        deadline = datetime.fromisoformat(intake["retry_after"])
        self.assertTrue(self.fixture.state.automatic_work_due(deadline + timedelta(seconds=1)))
        self.assertEqual(self.fixture.service.engine.resume_all(), (0, 0))
        downloads = self.fixture.source.fetch_count
        self.assertEqual(self.fixture.service.retry_activity("mail:" + plan["id"]), (1, 0))
        self.assertEqual(self.fixture.source.fetch_count, downloads)
        self.assertEqual(len(list((self.fixture.root / "A").glob("*.eml"))), 1)

    def test_preparation_retry_deadline_survives_restart_and_is_used_by_automatic_work(self):
        plan, intake = self.fail_preparation()
        self.fixture.account.enabled = False
        self.fixture.state.save_settings(self.fixture.settings)
        restarted = WorkspaceStore(self.fixture.state.database_path)
        service = make_service(restarted, restart.Registry(self.fixture.source))
        self.assertFalse(service.has_automatic_work(self.fixture.settings))
        deadline = datetime.fromisoformat(intake["retry_after"])
        with patch(
            "mailarchive.infrastructure.delivery_repository.datetime", wraps=datetime
        ) as clock:
            clock.now.return_value = deadline + timedelta(seconds=1)
            self.assertTrue(service.has_automatic_work(self.fixture.settings))
            self.assertEqual(service.run_once(self.fixture.settings, set()), [])
        self.assertEqual(self.fixture.source.fetch_count, 1)
        self.assertEqual(restarted.open_plans(), [])
        self.assertEqual(len(list((self.fixture.root / "A").glob("*.eml"))), 1)

    def test_unicode_subject_and_attachment_names_publish_within_filesystem_limit(self):
        for number, mode in enumerate((SaveMode.EMAIL_ONLY, SaveMode.ATTACHMENTS_ONLY), 1):
            with self.subTest(mode=mode):
                raw = sample_mail(subject="😀" * 60, attachments=[("領" * 90 + ".pdf", b"pdf")])
                self.message(raw, number)
                self.fixture.rule.targets[0].save_mode = mode
                result = self.fixture.service.run_once(self.fixture.settings)[0]
                self.assertEqual((result.archived, result.failed), (1, 0))
        files = [p for p in (self.fixture.root / "A").rglob("*") if p.is_file()]
        self.assertEqual(len(files), 2)
        self.assertTrue(all(len(p.name.encode("utf-8")) <= 255 for p in files))
        self.assertEqual({p.suffix for p in files}, {".eml", ".pdf"})

    def test_long_attachment_collisions_keep_extensions_and_receipt_identity(self):
        self.fixture.rule.targets[0].save_mode = SaveMode.ATTACHMENTS_ONLY
        self.fixture.rule.targets[0].attachments_in_destination = True
        raw = sample_mail(attachments=[("領" * 90 + ".pdf", b"pdf")])
        for number in (1, 2):
            self.message(raw, number)
            self.assertEqual(self.fixture.service.run_once(self.fixture.settings)[0].archived, 1)
        files = list((self.fixture.root / "A").glob("*.pdf"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(len(p.name.encode("utf-8")) <= 255 for p in files))
        self.assertTrue(any(p.name.endswith("-2.pdf") for p in files))
        result = self.fixture.run_range()
        self.assertEqual((result.already_processed, result.failed), (1, 0))
        self.assertEqual(set((self.fixture.root / "A").glob("*.pdf")), set(files))

    def test_shorter_filesystem_limit_keeps_different_long_names_and_collision_suffixes(self):
        self.fixture.rule.targets[0].save_mode = SaveMode.ATTACHMENTS_ONLY
        self.fixture.rule.targets[0].attachments_in_destination = True
        names = ["領" * 80 + tail + ".pdf" for tail in ("A", "B")]
        self.message(sample_mail(attachments=[(name, b"pdf") for name in names]))
        with patch.object(
            self.fixture.service.engine.output_files, "filename_limit", return_value=143
        ):
            result = self.fixture.service.run_once(self.fixture.settings)[0]
            self.assertEqual((result.archived, result.failed), (1, 0))
        files = list((self.fixture.root / "A").glob("*.pdf"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(len(path.name.encode("utf-8")) <= 143 for path in files))
        for number in (1, 2, 10, 100):
            with self.subTest(number=number):
                first, second = [
                    bounded_filename(name, max_bytes=143, number=number) for name in names
                ]
                self.assertNotEqual(first, second)
                self.assertLessEqual(len(first.encode("utf-8")), 143)
                self.assertTrue(first.endswith(".pdf" if number == 1 else f"-{number}.pdf"))
        self.assertEqual(bounded_filename("Mail.eml", number=2), "Mail-2.eml")

    @unittest.skipUnless(hasattr(os, "pathconf"), "Legacy oversized UTF-8 names require POSIX")
    def test_existing_oversized_output_recovers_after_restart_without_duplicate_outputs(self):
        self.message(sample_mail(subject="😀" * 60))
        with patch.object(
            self.fixture.service.engine, "_free_path", side_effect=lambda requested: requested
        ):
            self.assertEqual(self.fixture.service.run_once(self.fixture.settings)[0].failed, 1)
        plan = self.fixture.state.open_plans()[0]
        before = dict(self.fixture.state.outputs(plan["id"])[0])
        restarted = WorkspaceStore(self.fixture.state.database_path)
        service = make_service(restarted, restart.Registry(self.fixture.source))
        self.assertEqual(service.retry_activity("mail:" + plan["id"]), (1, 0))
        after = dict(restarted.outputs(plan["id"])[0])
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["requested_path"], before["requested_path"])
        self.assertEqual(after["artifact_key"], before["artifact_key"])
        self.assertEqual(after["status"], "done")
        self.assertEqual(len(list((self.fixture.root / "A").glob("*.eml"))), 1)
