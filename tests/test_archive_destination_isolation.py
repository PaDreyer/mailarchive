"""Archive destinations cannot consume the active profile's reserved storage."""

import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mailarchive.application.errors import ProfileUnavailableError
from mailarchive.application.source_port import MailboxError
from mailarchive.bootstrap import create_application
from mailarchive.domain.configuration import Rule, RuleTarget, SaveMode
from mailarchive.infrastructure.credentials import MemoryCredentialStore
from mailarchive.infrastructure.profile_database import ProfileDatabase
from mailarchive.infrastructure.profile_location import ConfigStore
from mailarchive.infrastructure.sqlite_core import SqliteDatabase
from tests import test_execution_outcomes as execution_fixture
from tests.test_restart_core import Registry


class ArchiveDestinationIsolationTests(unittest.TestCase):
    def fixture(self):
        fixture = execution_fixture.ExecutionOutcomeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def reopen(self, fixture):
        self.assertTrue(fixture.app.close())
        with patch("mailarchive.bootstrap.set_start_at_login"):
            reopened = create_application(
                ConfigStore(fixture.root / "profile"), MemoryCredentialStore()
            )
        self.addCleanup(reopened.close)
        return reopened

    @staticmethod
    def attachment_mail(filename, content):
        message = EmailMessage()
        message["From"] = "sender@example.org"
        message["To"] = "fake@example.org"
        message["Subject"] = "Preserved archive document"
        message.set_content("Attached archive document")
        message.add_attachment(
            content, maintype="application", subtype="octet-stream", filename=filename
        )
        return message.as_bytes()

    def legacy_archive(self, fixture, destination, mode, *, raw=None):
        """Create genuine old-version state through SaveRules and the native worker.

        The historical admission guard is the only boundary disabled when the
        current version rejects a destination accepted by its earlier version.
        Recovery and archive queries always run with the current implementation.
        """
        if raw is not None:
            fixture.source.messages["1"].raw = raw
        with self.historical_admission():
            fixture.app.save_rules(
                [
                    Rule(
                        "Historical archive destination",
                        targets=[
                            RuleTarget(str(destination), mode, attachments_in_destination=True)
                        ],
                    )
                ]
            )
            self.assertEqual(fixture.check().state.value, "completed")
        item = fixture.app.activity_page().items[0]
        detail = fixture.app.activity_detail(item.key)
        files = {
            output.final_path: Path(output.final_path).read_bytes()
            for output in detail.mail[0].outputs
        }
        self.assertTrue(files)
        self.assertTrue(all(output.status == "done" for output in detail.mail[0].outputs))
        return item.key, files

    @staticmethod
    def historical_admission():
        """Model the former destination acceptance only while producing old state."""
        stack = ExitStack()
        for method in ("require_rule", "require_path"):
            stack.enter_context(
                patch(
                    "mailarchive.application.archive_destinations.ArchiveDestinationPolicy."
                    + method
                )
            )
        return stack

    def assert_archives_preserved(self, reopened, key, files):
        detail = reopened.activity_detail(key)
        self.assertEqual(detail.item.status, "complete")
        self.assertEqual({output.final_path for output in detail.mail[0].outputs}, set(files))
        for output in detail.mail[0].outputs:
            self.assertEqual(output.status, "done")
            self.assertEqual(Path(output.final_path).read_bytes(), files[output.final_path])

    def test_reopen_preserves_previously_published_email_inside_work(self):
        fixture = self.fixture()
        key, files = self.legacy_archive(
            fixture, fixture.app.database_path.parent / "work", SaveMode.EMAIL_ONLY
        )
        reopened = self.reopen(fixture)
        self.assert_archives_preserved(reopened, key, files)

    def test_reopen_preserves_previously_published_eml_and_tmp_attachments_inside_work(self):
        for extension in ("eml", "tmp"):
            with self.subTest(extension=extension):
                fixture = self.fixture()
                key, files = self.legacy_archive(
                    fixture,
                    fixture.app.database_path.parent / "work",
                    SaveMode.ATTACHMENTS_ONLY,
                    raw=self.attachment_mail("report." + extension, b"Complete attachment bytes"),
                )
                reopened = self.reopen(fixture)
                self.assert_archives_preserved(reopened, key, files)

    def test_reopen_preserves_previously_published_sqlite_report_in_profile_root(self):
        fixture = self.fixture()
        document = fixture.root / "original-report.sqlite3"
        with closing(sqlite3.connect(document)) as database, database:
            database.execute("CREATE TABLE invoice(number TEXT, amount INTEGER)")
            database.execute("INSERT INTO invoice VALUES (?, ?)", ("INV-2026", 1200))
        key, files = self.legacy_archive(
            fixture,
            fixture.app.database_path.parent,
            SaveMode.ATTACHMENTS_ONLY,
            raw=self.attachment_mail("invoice-report.sqlite3", document.read_bytes()),
        )
        reopened = self.reopen(fixture)
        self.assert_archives_preserved(reopened, key, files)

    def test_own_published_profile_backup_is_an_archive_but_cannot_claim_the_shared_work(self):
        fixture = self.fixture()
        document = fixture.root / "original-profile-backup.sqlite3"
        with (
            closing(sqlite3.connect(fixture.app.database_path)) as source,
            closing(sqlite3.connect(document)) as backup,
        ):
            source.backup(backup)
        key, files = self.legacy_archive(
            fixture,
            fixture.app.database_path.parent,
            SaveMode.ATTACHMENTS_ONLY,
            raw=self.attachment_mail("mailarchive-backup.sqlite3", document.read_bytes()),
        )
        reopened = self.reopen(fixture)
        self.assert_archives_preserved(reopened, key, files)
        archived = Path(next(iter(files)))
        with self.assertRaisesRegex(RuntimeError, "different folder"):
            ProfileDatabase(archived, recover=True)
        self.assert_archives_preserved(reopened, key, files)

    def test_locked_primary_with_published_sqlite_is_retryable_and_reopens_after_unlock(self):
        fixture = self.fixture()
        document = fixture.root / "original-report.sqlite3"
        with closing(sqlite3.connect(document)) as database, database:
            database.execute("CREATE TABLE report(value TEXT)")
        key, files = self.legacy_archive(
            fixture,
            fixture.app.database_path.parent,
            SaveMode.ATTACHMENTS_ONLY,
            raw=self.attachment_mail("report.sqlite3", document.read_bytes()),
        )
        spool = fixture.app._context.execution.service.delivery.spool
        orphan, _digest = spool.stage([b"Owned orphan not cleaned while profile is locked"])
        self.assertTrue(fixture.app.close())
        native_connect = sqlite3.connect

        def short_connection(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return native_connect(*args, **kwargs)

        with closing(native_connect(fixture.app.database_path)) as locked:
            locked.execute("BEGIN EXCLUSIVE")
            with (
                patch(
                    "mailarchive.infrastructure.profile_ownership.sqlite3.connect", short_connection
                ),
                patch("mailarchive.bootstrap.set_start_at_login"),
            ):
                with self.assertRaises(ProfileUnavailableError) as caught:
                    create_application(
                        ConfigStore(fixture.root / "profile"), MemoryCredentialStore()
                    )
            self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
            self.assertIn("locked", str(caught.exception).lower())
            self.assertTrue(orphan.is_file())
            for path, content in files.items():
                self.assertEqual(Path(path).read_bytes(), content)
            locked.rollback()
        reopened = self.reopen(fixture)
        self.assert_archives_preserved(reopened, key, files)
        self.assertFalse(orphan.exists())

    def test_reopen_recognizes_historical_sqlite_publication_through_parent_path_aliases(self):
        for symbolic in (False, True):
            with self.subTest(symbolic=symbolic):
                fixture = self.fixture()
                profile = fixture.app.database_path.parent
                if symbolic:
                    destination = fixture.root / "historical-alias"
                    try:
                        destination.symlink_to(profile, target_is_directory=True)
                    except (OSError, NotImplementedError):
                        self.skipTest("Directory symlinks are unavailable")
                else:
                    destination = profile / "archives" / ".."
                document = fixture.root / "original-report.sqlite3"
                with closing(sqlite3.connect(document)) as database, database:
                    database.execute("CREATE TABLE report(value TEXT)")
                key, files = self.legacy_archive(
                    fixture,
                    destination,
                    SaveMode.ATTACHMENTS_ONLY,
                    raw=self.attachment_mail("report.sqlite3", document.read_bytes()),
                )
                reopened = self.reopen(fixture)
                self.assert_archives_preserved(reopened, key, files)

    def test_own_archived_sqlite_does_not_authorize_an_unrelated_sibling_database(self):
        fixture = self.fixture()
        document = fixture.root / "original-report.sqlite3"
        with closing(sqlite3.connect(document)) as database, database:
            database.execute("CREATE TABLE report(value TEXT)")
        _key, files = self.legacy_archive(
            fixture,
            fixture.app.database_path.parent,
            SaveMode.ATTACHMENTS_ONLY,
            raw=self.attachment_mail("report.sqlite3", document.read_bytes()),
        )
        self.assertTrue(fixture.app.close())
        sibling = fixture.app.database_path.with_name("unrelated-profile.data")
        SqliteDatabase(sibling)
        sibling_bytes = sibling.read_bytes()
        with patch("mailarchive.bootstrap.set_start_at_login"):
            with self.assertRaisesRegex(RuntimeError, "different folder"):
                create_application(ConfigStore(fixture.root / "profile"), MemoryCredentialStore())
        self.assertEqual(sibling.read_bytes(), sibling_bytes)
        for path, content in files.items():
            self.assertEqual(Path(path).read_bytes(), content)

    def test_replaced_archive_file_cannot_authorize_a_foreign_profile_with_a_stale_receipt(self):
        fixture = self.fixture()
        document = fixture.root / "original-report.sqlite3"
        with closing(sqlite3.connect(document)) as database, database:
            database.execute("CREATE TABLE report(value TEXT)")
        _key, files = self.legacy_archive(
            fixture,
            fixture.app.database_path.parent,
            SaveMode.ATTACHMENTS_ONLY,
            raw=self.attachment_mail("report.sqlite3", document.read_bytes()),
        )
        self.assertTrue(fixture.app.close())
        foreign = fixture.root / "another" / "profile.data"
        SqliteDatabase(foreign)
        archived = Path(next(iter(files)))
        shutil.copyfile(foreign, archived)
        replacement_bytes = archived.read_bytes()
        with patch("mailarchive.bootstrap.set_start_at_login"):
            with self.assertRaisesRegex(RuntimeError, "different folder"):
                create_application(ConfigStore(fixture.root / "profile"), MemoryCredentialStore())
        self.assertEqual(archived.read_bytes(), replacement_bytes)

    def test_historical_unsafe_rule_is_blocked_before_remote_message_admission(self):
        fixture = self.fixture()
        with self.historical_admission():
            fixture.app.save_rules(
                [
                    Rule(
                        "Historical unsafe rule",
                        targets=[RuleTarget(str(fixture.app.database_path.parent / "work"))],
                    )
                ]
            )
        self.assertNotEqual(fixture.check().state.value, "completed")
        self.assertEqual(fixture.source.fetch_count, 0)
        self.assertEqual(fixture.app.current_jobs(), ())
        self.assertEqual(list((fixture.app.database_path.parent / "work").iterdir()), [])

    def test_current_safe_discovery_cannot_download_an_older_unsafe_intake(self):
        fixture = self.fixture()
        message = fixture.source.messages["1"]
        message.error = MailboxError("Temporary download failure")
        with self.historical_admission():
            fixture.app.save_rules(
                [
                    Rule(
                        "Historical unsafe reservation",
                        targets=[RuleTarget(str(fixture.app.database_path.parent / "work"))],
                    )
                ]
            )
            self.assertEqual(fixture.check().state.value, "failed")
        service = fixture.app._context.execution.service
        source_id = fixture.account.mailboxes[0].id
        scope = dict(service.discovery.scope(source_id, "INBOX"))
        intake = dict(service.discovery.pending_automatic_intakes()[0])
        message.error = None
        raw = message.raw
        bodies = []

        def chunks():
            bodies.append(message.id)
            yield raw

        message.raw = None
        message.raw_chunks = chunks
        fixture.app.save_rules(
            [Rule("Current safe rule", targets=[RuleTarget(str(fixture.root / "safe"))])]
        )
        downloads = fixture.source.fetch_count
        self.assertEqual(fixture.check().state.value, "failed")
        self.assertEqual(bodies, [])
        self.assertEqual(fixture.source.fetch_count, downloads)
        retained = dict(service.discovery.pending_automatic_intakes()[0])
        self.assertEqual(retained["id"], intake["id"])
        self.assertEqual(retained["status"], "error")
        self.assertIn("reserved", retained["error"].lower())
        self.assertEqual(
            dict(service.discovery.scope(source_id, "INBOX"))["cursor"], scope["cursor"]
        )
        self.assertFalse((fixture.root / "safe").exists())
        self.assertEqual(list((fixture.app.database_path.parent / "work").iterdir()), [])

    def test_selected_safe_manual_rule_ignores_other_historical_unsafe_rules(self):
        fixture = self.fixture()
        safe = Rule(
            "Explicit safe rule",
            targets=[RuleTarget(str(fixture.root / "safe"), SaveMode.EMAIL_ONLY)],
        )
        unsafe = Rule(
            "Historical higher priority unsafe rule",
            targets=[RuleTarget(str(fixture.app.database_path.parent / "work"))],
        )
        with self.historical_admission():
            fixture.app.save_rules([unsafe, safe])
        start = len(fixture.progress)
        operation = fixture.app.apply_rule_to_past_mail(safe.id, None, None, "UTC")
        self.assertEqual(fixture.terminal(start, "operation").state.value, "completed")
        detail = fixture.app.activity_detail("operation:" + operation)
        self.assertEqual(detail.item.status, "completed")
        self.assertEqual(detail.mail[0].rule_name, safe.name)
        outputs = detail.mail[0].outputs
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].status, "done")
        self.assertEqual(Path(outputs[0].final_path).parent, fixture.root / "safe")
        self.assertEqual(Path(outputs[0].final_path).read_bytes(), fixture.source.messages["1"].raw)
        self.assertEqual(list((fixture.app.database_path.parent / "work").iterdir()), [])

    def test_reopen_retains_partial_legacy_archive_and_accepted_raw_copy(self):
        fixture = self.fixture()
        work = fixture.app.database_path.parent / "work"
        obstruction = fixture.root / "offline"
        obstruction.write_bytes(b"Destination is offline")
        with self.historical_admission():
            fixture.app.save_rules(
                [
                    Rule(
                        "Partially completed historical rule",
                        targets=[
                            RuleTarget(str(work), SaveMode.EMAIL_ONLY),
                            RuleTarget(str(obstruction / "archive"), SaveMode.EMAIL_ONLY),
                        ],
                    )
                ]
            )
            self.assertEqual(fixture.check().state.value, "failed")
        item = fixture.app.current_jobs()[0]
        before = fixture.app.activity_detail(item.key)
        outputs = before.mail[0].outputs
        self.assertEqual(sorted(output.status for output in outputs), ["done", "error"])
        archived = Path(next(output.final_path for output in outputs if output.status == "done"))
        archived_bytes = archived.read_bytes()
        state = fixture.app._context.execution.service.delivery
        with state.connection() as database:
            raw = Path(database.execute("SELECT raw_path FROM plan").fetchone()[0])
        raw_bytes = raw.read_bytes()
        reopened = self.reopen(fixture)
        after = reopened.activity_detail(item.key)
        self.assertEqual(after, before)
        self.assertEqual(archived.read_bytes(), archived_bytes)
        self.assertEqual(raw.read_bytes(), raw_bytes)

    def test_recovery_retains_open_and_stopped_manual_raw_and_removes_only_own_orphans(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                fixture = self.fixture()
                obstruction = fixture.block_output()
                if manual:
                    start = len(fixture.progress)
                    operation = fixture.app.apply_rule_to_past_mail(
                        fixture.rule.id, None, None, "UTC"
                    )
                    fixture.terminal(start, "operation")
                    fixture.app.stop_operation("operation:" + operation)
                else:
                    fixture.check()
                state = fixture.app._context.execution.service.delivery
                with state.connection() as database:
                    plan = database.execute("SELECT * FROM plan").fetchone()
                raw = Path(plan["raw_path"])
                retained_bytes = raw.read_bytes()
                spool = state.spool
                orphan, _digest = spool.stage([b"Unreferenced owned work"])
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix="intake-", suffix=".tmp", dir=spool.path
                )
                os.close(descriptor)
                unowned = {
                    spool.path / "personal.eml": b"Personal mail",
                    spool.path / "notes.tmp": b"Notes",
                }
                for path, content in unowned.items():
                    path.write_bytes(content)
                self.assertTrue(fixture.app.close())
                reopened = ProfileDatabase(fixture.app.database_path, recover=True)
                with reopened.connection() as database:
                    status = database.execute(
                        "SELECT status FROM plan WHERE id=?", (plan["id"],)
                    ).fetchone()[0]
                self.assertEqual(status, "paused" if manual else "open")
                self.assertEqual(raw.read_bytes(), retained_bytes)
                self.assertFalse(orphan.exists())
                self.assertFalse(Path(temporary_name).exists())
                for path, content in unowned.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertTrue(obstruction.is_file())

    def assert_rejected_without_persistence_or_admission(self, fixture, destination):
        before = fixture.app.settings.to_dict()
        revision = fixture.app.settings.config_revision
        downloads = fixture.source.fetch_count
        with self.assertRaisesRegex(ValueError, "(?i)(profile|work|reserved)"):
            fixture.app.save_rules(
                [Rule("Reserved destination", targets=[RuleTarget(destination)])]
            )
        self.assertEqual(fixture.app.settings.to_dict(), before)
        self.assertEqual(fixture.app.settings.config_revision, revision)
        self.assertEqual(fixture.source.fetch_count, downloads)
        self.assertEqual(fixture.app.current_jobs(), ())

    def test_public_save_rules_rejects_profile_and_work_destinations(self):
        fixture = self.fixture()
        profile = fixture.app.database_path.parent
        for destination in (profile, profile / "work", profile / "work" / "archives"):
            with self.subTest(destination=destination):
                self.assert_rejected_without_persistence_or_admission(fixture, str(destination))

    def test_parent_components_and_dynamic_templates_cannot_resolve_to_reserved_storage(self):
        fixture = self.fixture()
        profile = fixture.app.database_path.parent
        destinations = (
            profile / "archive" / "..",
            profile / "work" / "temporary" / "..",
            profile / "{year}" / "..",
            profile / "work" / "{year}" / "{month}",
            profile / "work" / "{year}" / "..",
        )
        for destination in destinations:
            with self.subTest(destination=destination):
                self.assert_rejected_without_persistence_or_admission(fixture, str(destination))

    def test_physical_alias_and_escaped_braces_cannot_hide_reserved_destination(self):
        fixture = self.fixture()
        alias = fixture.root / "reserved{profile}"
        try:
            alias.symlink_to(fixture.app.database_path.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are unavailable")
        for path in (alias, alias / "work", alias / "work" / "{year}"):
            destination = str(path).replace("{profile}", "{{profile}}")
            with self.subTest(destination=destination):
                self.assert_rejected_without_persistence_or_admission(fixture, destination)

    def test_dynamic_date_components_cannot_select_the_actual_numeric_profile_folder(self):
        fixture = self.fixture()
        destination = fixture.root / "2026" / "01" / "workspace.sqlite3"
        ConfigStore(destination.parent).save(fixture.app.settings)
        with patch(
            "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(fixture.source)
        ):
            fixture.app.switch_profile(destination)
        for path in (
            fixture.root / "{year}" / "{month}",
            fixture.root / "{year}" / "{month}" / "work",
        ):
            with self.subTest(destination=path):
                self.assert_rejected_without_persistence_or_admission(fixture, str(path))

    def test_repeated_date_field_does_not_reject_a_path_that_cannot_select_profile_root(self):
        fixture = self.fixture()
        destination = fixture.root / "2026" / "2027" / "workspace.sqlite3"
        ConfigStore(destination.parent).save(fixture.app.settings)
        with patch(
            "mailarchive.bootstrap.MessageSourceRegistry", return_value=Registry(fixture.source)
        ):
            fixture.app.switch_profile(destination)
        fixture.app.save_rules(
            [
                Rule(
                    "Repeated reception year",
                    targets=[
                        RuleTarget(str(fixture.root / "{year}" / "{year}"), SaveMode.EMAIL_ONLY)
                    ],
                )
            ]
        )
        self.assertEqual(fixture.check().state.value, "completed")
        item = fixture.app.activity_page().items[0]
        output = fixture.app.activity_detail(item.key).mail[0].outputs[0]
        self.assertEqual(Path(output.final_path).parent, fixture.root / "2026" / "2026")
        self.assertEqual(Path(output.final_path).read_bytes(), fixture.source.messages["1"].raw)

    def test_preview_text_does_not_reject_actual_date_destinations_but_literal_root_is_reserved(
        self,
    ):
        for preview, field, actual in (("YYYY", "year", "2026"), ("MM", "month", "01")):
            with self.subTest(preview=preview, field=field):
                fixture = self.fixture()
                destination = fixture.root / preview / "workspace.sqlite3"
                ConfigStore(destination.parent).save(fixture.app.settings)
                with patch(
                    "mailarchive.bootstrap.MessageSourceRegistry",
                    return_value=Registry(fixture.source),
                ):
                    fixture.app.switch_profile(destination)
                self.assert_rejected_without_persistence_or_admission(
                    fixture, str(destination.parent)
                )
                fixture.app.save_rules(
                    [
                        Rule(
                            "Actual reception date",
                            targets=[
                                RuleTarget(
                                    str(fixture.root / ("{" + field + "}")), SaveMode.EMAIL_ONLY
                                )
                            ],
                        )
                    ]
                )
                self.assertEqual(fixture.check().state.value, "completed")
                item = fixture.app.activity_page().items[0]
                output = fixture.app.activity_detail(item.key).mail[0].outputs[0]
                self.assertEqual(Path(output.final_path).parent, fixture.root / actual)
                self.assertEqual(
                    Path(output.final_path).read_bytes(), fixture.source.messages["1"].raw
                )

    def test_public_check_can_archive_to_safe_profile_subfolder_and_dynamic_destination(self):
        fixture = self.fixture()
        profile = fixture.app.database_path.parent
        destinations = (
            profile / "archives",
            fixture.root / "export{literal}" / "{year}" / "{month}",
        )
        fixture.app.save_rules(
            [
                Rule(
                    "Safe destinations",
                    targets=[
                        RuleTarget(
                            str(path).replace("{literal}", "{{literal}}"), SaveMode.EMAIL_ONLY
                        )
                        for path in destinations
                    ],
                )
            ]
        )
        self.assertEqual(fixture.check().state.value, "completed")
        item = fixture.app.activity_page().items[0]
        outputs = fixture.app.activity_detail(item.key).mail[0].outputs
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all(output.status == "done" for output in outputs))
        self.assertEqual(
            {Path(output.final_path).parent for output in outputs},
            {profile / "archives", fixture.root / "export{literal}" / "2026" / "01"},
        )
        for output in outputs:
            self.assertEqual(Path(output.final_path).read_bytes(), fixture.source.messages["1"].raw)
