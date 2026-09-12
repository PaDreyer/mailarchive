import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mailarchive.activity_log import ActivityLog
from mailarchive.config import ConfigStore
from mailarchive.mail_parser import parse_mail
from mailarchive.models import Rule
from mailarchive.service import EventLevel, ServiceEvent
from mailarchive.storage import ArchiveState, ArchiveStorage
from tests.helpers import sample_mail
from tests.test_app import make_desktop


class ActivityLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "activity-log.sqlite3"
        self.log = ActivityLog(self.database)

    def test_restart_preserves_every_event_field_and_timestamp(self) -> None:
        instant = datetime(2026, 9, 12, 8, 30, tzinfo=timezone(timedelta(hours=2)))
        for level in EventLevel:
            self.log.record(ServiceEvent(level, "Work: Grüß dich", "account", instant))

        page = ActivityLog(self.database).page()

        self.assertEqual(page.total, 4)
        self.assertEqual([event.level for event in page.events], list(reversed(EventLevel)))
        for event in page.events:
            self.assertEqual(event.message, "Work: Grüß dich")
            self.assertEqual(event.account_id, "account")
            self.assertEqual(event.created_at.timestamp(), instant.timestamp())

    def test_time_filter_includes_boundary_and_paging_reaches_all_saved_entries(self) -> None:
        boundary = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.log.record(ServiceEvent(EventLevel.INFO, "Older", created_at=boundary - timedelta(1)))
        for index in range(120):
            self.log.record(ServiceEvent(EventLevel.INFO, str(index), created_at=boundary))

        pages = [self.log.page(since=boundary, offset=offset) for offset in (0, 50, 100)]

        self.assertEqual([len(page.events) for page in pages], [50, 50, 20])
        self.assertEqual([page.total for page in pages], [120, 120, 120])
        self.assertEqual(
            [event.message for page in pages for event in page.events],
            [str(index) for index in reversed(range(120))],
        )
        self.assertEqual(self.log.page(offset=1000).offset, 100)
        self.assertEqual(self.log.page(since=boundary + timedelta(1)).total, 0)
        self.assertEqual(self.log.page().total, 121)

    def test_worker_threads_can_write_without_dropping_events(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(
                executor.map(
                    lambda index: self.log.record(ServiceEvent(EventLevel.SUCCESS, str(index))),
                    range(40),
                )
            )
        self.assertEqual(ActivityLog(self.database).page().total, 40)

    def test_clear_survives_restart_and_leaves_archive_index_and_files_intact(self) -> None:
        state = ArchiveState(self.root / "archive-state.sqlite3")
        mail = parse_mail(sample_mail())
        rule = Rule("All", "Inbox")
        result = ArchiveStorage(self.root / "Archive").archive(mail, rule)
        state.record("account", "imap:1", "7", mail, rule, result)
        state.complete_initial_scan("account", "imap:1", {"8"})
        self.log.record(ServiceEvent(EventLevel.SUCCESS, "Archived"))

        self.log.clear()

        self.assertEqual(ActivityLog(self.database).page().total, 0)
        self.assertTrue(state.was_processed("account", "imap:1", "7"))
        self.assertTrue(state.has_completed_initial_scan("account", "imap:1"))
        self.assertEqual(
            state.processed_message_ids("account", "imap:1", include_skipped=True), {"7", "8"}
        )
        self.assertEqual(result.files[0].read_bytes(), mail.raw)
        self.log.record(ServiceEvent(EventLevel.INFO, "After clear"))
        self.assertEqual(self.log.page().events[0].message, "After clear")

    def test_invalid_page_arguments_are_rejected(self) -> None:
        for arguments in ({"offset": -1}, {"limit": 0}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.log.page(**arguments)


class ActivityLogControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ConfigStore(Path(self.temporary.name))
        self.desktop = make_desktop()
        self.desktop.activity_log = ActivityLog(self.store.data_dir / "activity-log.sqlite3")

    def test_log_is_saved_before_ui_work_and_even_when_closing(self) -> None:
        event = ServiceEvent(EventLevel.ERROR, "Check failed")
        self.desktop.on_service_event(event)
        self.assertEqual(self.desktop.activity_log.page().total, 1)
        self.assertEqual(self.desktop.log_tree.rows, [])
        self.desktop._drain_ui_queue()
        self.assertEqual(self.desktop.log_tree.rows[0]["values"][2], "Check failed")
        self.desktop._closing = True
        self.desktop.on_service_event(ServiceEvent(EventLevel.SUCCESS, "Finished at shutdown"))
        self.assertTrue(self.desktop.ui_queue.empty())
        self.assertEqual(ActivityLog(self.desktop.activity_log.database_path).page().total, 2)

    def test_last_50_is_default_and_all_time_can_page_without_replaying_notifications(self) -> None:
        for index in range(65):
            self.desktop.activity_log.record(ServiceEvent(EventLevel.ERROR, str(index)))
        self.desktop.refresh_log()
        self.assertEqual(len(self.desktop.log_tree.rows), 50)
        self.assertEqual(self.desktop.log_next_button.options["state"], "disabled")
        self.assertEqual(self.desktop.log_summary_var.get(), "Showing 1-50 of 50 entries")

        self.desktop.log_filter_var.set("All time")
        self.desktop.refresh_log(reset_page=True)
        self.assertEqual(self.desktop.log_next_button.options["state"], "normal")
        self.desktop.change_log_page(1)
        self.assertEqual(len(self.desktop.log_tree.rows), 15)
        self.assertEqual(self.desktop.log_summary_var.get(), "Showing 51-65 of 65 entries")
        old_rows = self.desktop.log_tree.rows.copy()
        self.desktop.on_service_event(ServiceEvent(EventLevel.INFO, "New check"))
        self.desktop._drain_ui_queue()
        self.assertEqual(self.desktop.log_tree.rows, old_rows)
        self.desktop.change_log_page(-1)
        self.assertEqual(self.desktop.log_tree.rows[0]["values"][2], "New check")
        self.desktop.tray.notify.assert_not_called()
        self.desktop.tray.set_state.assert_not_called()

    def test_every_time_filter_and_reset_to_last_50(self) -> None:
        now = datetime.now().astimezone()
        for days in (0, 2, 10, 40):
            self.desktop.activity_log.record(
                ServiceEvent(EventLevel.INFO, str(days), created_at=now - timedelta(days=days))
            )
        for label, expected in (("Last 24 hours", 1), ("Last 7 days", 2), ("Last 30 days", 3)):
            with self.subTest(label=label):
                self.desktop.log_filter_var.set(label)
                self.desktop.refresh_log(reset_page=True)
                self.assertEqual(len(self.desktop.log_tree.rows), expected)
        self.desktop._log_offset = 50
        self.desktop.log_filter_var.set("Last 50")
        self.desktop.refresh_log()
        self.assertEqual(self.desktop._log_offset, 0)
        self.assertEqual(len(self.desktop.log_tree.rows), 4)

    @patch("mailarchive.desktop.messagebox.askyesno")
    def test_clear_requires_confirmation_and_clears_entries_outside_filter(self, confirm) -> None:
        self.desktop.activity_log.record(
            ServiceEvent(EventLevel.INFO, "Old", created_at=datetime.now() - timedelta(days=40))
        )
        self.desktop.log_filter_var.set("Last 24 hours")
        confirm.return_value = False
        self.desktop.clear_log()
        self.assertEqual(self.desktop.activity_log.page().total, 1)
        confirm.return_value = True
        self.desktop.clear_log()
        self.assertEqual(self.desktop.activity_log.page().total, 0)
        self.assertEqual(self.desktop.log_summary_var.get(), "No activity in this view.")
        self.desktop.service.relocate_state_database.assert_not_called()

    def test_storage_failures_are_reported_and_error_notifications_still_work(self) -> None:
        with patch.object(
            self.desktop.activity_log, "record", side_effect=sqlite3.OperationalError("disk full")
        ):
            self.desktop.on_service_event(ServiceEvent(EventLevel.ERROR, "Mail failed"))
        self.desktop._drain_ui_queue()
        self.assertIn("Could not save activity log: disk full", self.desktop.log_summary_var.get())
        self.desktop.tray.notify.assert_called_once_with("Mail failed")
        with patch.object(
            self.desktop.activity_log, "page", side_effect=sqlite3.OperationalError("locked")
        ):
            self.desktop.refresh_log()
        self.assertIn("Could not load activity log: locked", self.desktop.log_summary_var.get())
        with (
            patch("mailarchive.desktop.messagebox.askyesno", return_value=True),
            patch.object(
                self.desktop.activity_log,
                "clear",
                side_effect=sqlite3.OperationalError("read only"),
            ),
            patch("mailarchive.desktop.messagebox.showerror") as showerror,
        ):
            self.desktop.clear_log()
        showerror.assert_called_once_with(
            "Activity log not cleared", "read only", parent=self.desktop.root
        )
