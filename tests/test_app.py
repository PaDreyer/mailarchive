from __future__ import annotations

import queue
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, call, patch

import mailarchive.app as app_module
import mailarchive.tray as tray_module
from mailarchive import __version__
from mailarchive.account_form import AccountSubmission
from mailarchive.activity_log import ActivityPage
from mailarchive.app import (
    AccountDialog,
    DesktopApp,
    RuleDialog,
    TrayController,
    _auth_label_for,
    _condition_summary,
    _label_for,
)
from mailarchive.config import ConfigStore
from mailarchive.credential_data import load_credential_data, update_credential_data
from mailarchive.credentials import MemoryCredentialStore
from mailarchive.imap_client import RemoteMessage
from mailarchive.mail_identity import imap_scope
from mailarchive.migrations import DatabaseMigrationError
from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    DateFolderPosition,
    Mailbox,
    MailField,
    MailProvider,
    MatchMode,
    MatchOperator,
    Rule,
    SaveMode,
    Settings,
)
from mailarchive.runner import BackgroundRunner
from mailarchive.service import ArchiveService, EventLevel, RunProgress, ServiceEvent
from mailarchive.storage import ArchiveState
from mailarchive.updates import Release, UpdateError
from tests.helpers import imap_namespace, sample_mail


class FakeVariable:
    def __init__(self, value=None) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class FakeWidget:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}
        self.grid_calls: list[dict[str, object]] = []
        self.removed = False

    def configure(self, **options) -> None:
        self.options.update(options)

    def grid(self, **options) -> None:
        self.grid_calls.append(options)
        self.removed = False

    def grid_remove(self) -> None:
        self.removed = True


class FakeTree:
    def __init__(self, selection: tuple[str, ...] = ()) -> None:
        self.rows: list[dict[str, object]] = []
        self.selected = selection
        self.deleted: list[object] = []

    def insert(self, parent, index, **options) -> None:
        row = {"parent": parent, "index": index, **options}
        if isinstance(index, int):
            self.rows.insert(index, row)
        else:
            self.rows.append(row)

    def get_children(self):
        return tuple(row.get("iid", index) for index, row in enumerate(self.rows))

    def delete(self, *items) -> None:
        self.deleted.extend(items)
        if items:
            item_set = set(items)
            self.rows = [
                row for index, row in enumerate(self.rows) if row.get("iid", index) not in item_set
            ]

    def selection(self):
        return self.selected

    def selection_set(self, item) -> None:
        self.selected = (item,)


class ImmediateThread:
    created: list[ImmediateThread] = []

    def __init__(self, *, target, name, daemon) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self) -> None:
        self.started = True
        self.target()


def make_account_dialog(
    *,
    provider: str = "Generic IMAP",
    auth: str = "Password",
    account: Account | None = None,
) -> AccountDialog:
    dialog = object.__new__(AccountDialog)
    dialog.account = account
    dialog.mailboxes = (
        account.mailboxes if account else [Mailbox("mail@example.com", folders=["INBOX"])]
    )
    dialog.variables = {
        "label": FakeVariable("Work"),
        "provider": FakeVariable(provider),
        "auth": FakeVariable(auth),
        "host": FakeVariable("imap.example.com"),
        "port": FakeVariable("993"),
        "username": FakeVariable("mail@example.com"),
        "secret": FakeVariable("secret"),
        "folder": FakeVariable("INBOX"),
        "client_id": FakeVariable("client-id"),
        "tenant_id": FakeVariable("tenant-id"),
        "service_account_file": FakeVariable("service-account.json"),
        "poll": FakeVariable(""),
        "ssl": FakeVariable(True),
        "enabled": FakeVariable(True),
        "archive_existing": FakeVariable(
            account.mailboxes[0].archive_existing_messages if account else False
        ),
    }
    dialog.destroy = MagicMock()
    return dialog


def make_rule_dialog() -> RuleDialog:
    dialog = object.__new__(RuleDialog)
    dialog.name_var = FakeVariable("Invoices")
    dialog.field_var = FakeVariable("Subject")
    dialog.operator_var = FakeVariable("contains")
    dialog.value_var = FakeVariable("invoice")
    dialog.sender_value_vars = [FakeVariable("")]
    dialog.destination_var = FakeVariable("Finance")
    dialog.date_folder_var = FakeVariable("No date folders")
    dialog.destination_preview_var = FakeVariable()
    dialog.save_var = FakeVariable("Email only (.eml)")
    dialog.enabled_var = FakeVariable(True)
    dialog.account_scope_var = FakeVariable("all")
    dialog.account_options = []
    dialog.account_list = MagicMock()
    dialog.account_list.curselection.return_value = ()
    dialog.archive_root = "/archive"
    dialog.rule = None
    dialog.result = None
    dialog.destroy = MagicMock()
    return dialog


def make_desktop(settings: Settings | None = None) -> DesktopApp:
    desktop = object.__new__(DesktopApp)
    desktop.settings = settings or Settings(archive_root="/archive")
    desktop.root = MagicMock()
    desktop.config_store = MagicMock()
    desktop.credential_store = MagicMock()
    desktop.account_tree = FakeTree()
    desktop.rule_tree = FakeTree()
    desktop.account_summary = FakeVariable()
    desktop.rule_summary = FakeVariable()
    desktop.archive_summary = FakeVariable()
    desktop.progress_var = FakeVariable()
    desktop.elapsed_var = FakeVariable()
    desktop.progress_bar = MagicMock()
    desktop.archive_button = MagicMock()
    desktop._archive_running = False
    desktop._run_event_level = EventLevel.INFO
    desktop._run_started_at = 0.0
    desktop._progress_timer = None
    desktop.archive_var = FakeVariable(desktop.settings.archive_root)
    desktop.poll_var = FakeVariable(str(desktop.settings.default_poll_minutes))
    desktop.database_var = FakeVariable("/state.sqlite3")
    desktop.startup_var = FakeVariable(desktop.settings.start_at_login)
    desktop.minimize_var = FakeVariable(desktop.settings.minimize_to_tray)
    desktop.warning_var = FakeVariable(desktop.settings.warn_on_error)
    desktop.state = SimpleNamespace(database_path=Path("/state.sqlite3"))
    desktop.service = MagicMock()
    desktop.service.state_database_change.return_value.__enter__.return_value = (
        desktop.service.relocate_state_database
    )
    desktop.runner = MagicMock()
    desktop.tray = MagicMock()
    desktop.log_tree = FakeTree()
    desktop.activity_log = MagicMock()
    desktop.activity_log.page.return_value = ActivityPage([], 0, 0)
    desktop.log_filter_var = FakeVariable("Last 50")
    desktop.log_summary_var = FakeVariable()
    desktop.log_previous_button = FakeWidget()
    desktop.log_next_button = FakeWidget()
    desktop._log_offset = 0
    desktop.ui_queue = queue.Queue()
    desktop._closing = False
    desktop._saving_settings = False
    desktop._setting_entry_fields = {}
    desktop._checking_for_updates = False
    desktop.update_button = MagicMock()
    desktop._authorizing_account_ids = set()
    desktop._authorization_attempts = {}
    desktop._authorization_attempts_lock = threading.Lock()
    desktop.desktop_integration = None
    return desktop


class AppHelperTests(unittest.TestCase):
    def test_label_helpers_cover_known_unknown_and_provider_specific_auth(self) -> None:
        self.assertEqual(_label_for({"A": 1}, 1), "A")
        self.assertEqual(_label_for({"A": 1}, 2), "2")
        self.assertEqual(
            _auth_label_for(MailProvider.GMAIL_API, AuthMode.OAUTH_APPLICATION),
            "Google Workspace - domain-wide delegation",
        )
        self.assertEqual(
            _auth_label_for(MailProvider.GMAIL_API, AuthMode.OAUTH_USER),
            "Google OAuth - user sign-in",
        )
        self.assertEqual(
            _auth_label_for(MailProvider.MICROSOFT_GRAPH, AuthMode.OAUTH_APPLICATION),
            "Microsoft OAuth - application access",
        )
        self.assertEqual(
            _auth_label_for(MailProvider.MICROSOFT_GRAPH, AuthMode.OAUTH_USER),
            "Microsoft OAuth - delegated user access",
        )
        self.assertEqual(
            _auth_label_for(MailProvider.GENERIC_IMAP, AuthMode.PASSWORD),
            "Password",
        )
        self.assertEqual(
            _auth_label_for(MailProvider.GENERIC_IMAP, AuthMode.OAUTH_USER),
            "Microsoft OAuth (XOAUTH2)",
        )

    def test_condition_summary_covers_catch_all_attachment_and_text(self) -> None:
        self.assertEqual(_condition_summary(Rule("All", "Inbox")), "All emails")
        self.assertEqual(
            _condition_summary(
                Rule(
                    "With files",
                    "Files",
                    [Condition(MailField.HAS_ATTACHMENT, value="yes")],
                )
            ),
            "Has attachments: Yes",
        )
        self.assertEqual(
            _condition_summary(
                Rule(
                    "Without files",
                    "NoFiles",
                    [Condition(MailField.HAS_ATTACHMENT, value="false")],
                )
            ),
            "Has attachments: No",
        )
        self.assertEqual(
            _condition_summary(
                Rule(
                    "Invoices",
                    "Finance",
                    [Condition(MailField.SUBJECT, MatchOperator.STARTS_WITH, "Invoice")],
                )
            ),
            'Subject starts with "Invoice"',
        )
        self.assertEqual(
            _condition_summary(
                Rule(
                    "Senders",
                    "Known",
                    [
                        Condition(MailField.SENDER, MatchOperator.EQUALS, "one@example.com"),
                        Condition(MailField.SENDER, MatchOperator.EQUALS, "two@example.com"),
                    ],
                    match_mode=MatchMode.ANY,
                )
            ),
            'Sender equals any of: "one@example.com", "two@example.com"',
        )


class TrayControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = object.__new__(TrayController)
        self.controller.post_ui = MagicMock()
        self.controller.show_callback = MagicMock()
        self.controller.run_callback = MagicMock()
        self.controller.quit_callback = MagicMock()
        self.controller.available = True
        self.controller.icon = MagicMock()
        self.controller._linux_tray = None

    def test_menu_callbacks_are_marshaled_to_ui_thread(self) -> None:
        self.controller._show()
        self.controller._run()
        self.controller._quit()
        self.assertEqual(
            self.controller.post_ui.call_args_list,
            [
                call(self.controller.show_callback),
                call(self.controller.run_callback),
                call(self.controller.quit_callback),
            ],
        )

    def test_state_notification_and_stop_are_safe(self) -> None:
        marker = object()
        with patch.object(TrayController, "_image", return_value=marker) as image:
            self.controller.set_state("warning", "Attention")
        image.assert_called_once_with("warning")
        self.assertIs(self.controller.icon.icon, marker)
        self.assertEqual(self.controller.icon.title, "Attention")

        self.controller.notify("Problem")
        self.controller.icon.notify.assert_called_once_with(
            "Problem", "MailArchive - problem detected"
        )
        self.controller.stop()
        self.controller.icon.stop.assert_called_once_with()

        self.controller.icon.notify.side_effect = RuntimeError("no notifier")
        self.controller.notify("Ignored")

    def test_unavailable_tray_is_a_noop(self) -> None:
        self.controller.available = False
        with patch.object(TrayController, "_image") as image:
            self.controller.set_state("error", "Error")
        image.assert_not_called()
        self.controller.notify("Ignored")
        self.controller.stop()
        self.controller.icon.notify.assert_not_called()
        self.controller.icon.stop.assert_not_called()

    def test_constructor_starts_status_notifier_on_linux(self) -> None:
        linux_tray = MagicMock()
        linux_tray.start.return_value = True
        with (
            patch.object(TrayController, "_create_linux_tray", return_value=linux_tray),
            patch.object(tray_module.os, "name", "posix"),
        ):
            controller = TrayController(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        self.assertTrue(controller.available)
        self.assertTrue(controller.safe_to_hide)
        self.assertIsNone(controller.icon)
        linux_tray.start.assert_called_once_with()

    def test_constructor_disables_linux_tray_when_no_host_is_available(self) -> None:
        linux_tray = MagicMock()
        linux_tray.start.return_value = False
        with (
            patch.object(TrayController, "_create_linux_tray", return_value=linux_tray),
            patch.object(tray_module.os, "name", "posix"),
        ):
            controller = TrayController(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        self.assertFalse(controller.available)
        self.assertFalse(controller.safe_to_hide)
        self.assertIsNone(controller.icon)

    def test_constructor_starts_windows_backend(self) -> None:
        class FakeMenuItem:
            def __init__(self, label, callback, default=False) -> None:
                self.label = label
                self.callback = callback
                self.default = default

        class FakeMenu:
            SEPARATOR = object()

            def __init__(self, *items) -> None:
                self.items = items

        class FakeIcon:
            def __init__(self, name, image, title, menu) -> None:
                self.name = name
                self.icon = image
                self.title = title
                self.menu = menu

            def run_detached(self) -> None:
                pass

        fake_pystray = SimpleNamespace(
            MenuItem=FakeMenuItem,
            Menu=FakeMenu,
            Icon=FakeIcon,
        )
        with (
            patch.dict(sys.modules, {"pystray": fake_pystray}),
            patch.object(TrayController, "_image", return_value="image"),
            patch.object(tray_module.os, "name", "nt"),
        ):
            controller = TrayController(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        self.assertTrue(controller.available)
        self.assertTrue(controller.safe_to_hide)
        self.assertEqual(controller.icon.title, "MailArchive - ready")
        self.assertEqual(controller.icon.menu.items[0].label, "Open MailArchive")

    def test_generated_tray_image_has_expected_size_and_state_color(self) -> None:
        image = TrayController._image("error")
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((6, 25)), (197, 48, 48, 255))


class AccountDialogTests(unittest.TestCase):
    def test_dialog_size_covers_all_provider_layouts_and_restores_selection(self) -> None:
        dialog = make_account_dialog(
            provider="Gmail (Google API)", auth="Google OAuth - user sign-in"
        )
        dialog._update_fields = MagicMock()
        dialog.update_idletasks = MagicMock()
        dialog.winfo_reqwidth = MagicMock(side_effect=[500, 510, 620, 600, 590, 610])
        dialog.winfo_reqheight = MagicMock(side_effect=[420, 450, 430, 480, 440, 470])
        dialog.minsize = MagicMock()
        dialog.geometry = MagicMock()

        dialog._fix_size_for_layouts()

        self.assertEqual(dialog.variables["provider"].get(), "Gmail (Google API)")
        self.assertEqual(dialog.variables["auth"].get(), "Google OAuth - user sign-in")
        dialog.minsize.assert_called_once_with(620, 480)
        dialog.geometry.assert_called_once_with("620x480")
        self.assertEqual(dialog._update_fields.call_count, 7)

    def test_provider_change_sets_compatible_auth_and_folder(self) -> None:
        dialog = make_account_dialog()
        dialog._update_fields = MagicMock()

        dialog.variables["folder"].set("")
        dialog.variables["provider"].set("Gmail (Google API)")
        dialog._provider_changed()
        self.assertEqual(dialog.variables["auth"].get(), "Google OAuth - user sign-in")
        self.assertEqual(dialog.variables["folder"].get(), "")

        dialog.variables["provider"].set("Outlook / Microsoft 365 (Microsoft Graph)")
        dialog._provider_changed()
        self.assertEqual(
            dialog.variables["auth"].get(),
            "Microsoft OAuth - delegated user access",
        )
        self.assertEqual(dialog.variables["folder"].get(), "")

        dialog.variables["folder"].set("")
        dialog.variables["provider"].set("Generic IMAP")
        dialog._provider_changed()
        self.assertEqual(dialog.variables["auth"].get(), "Password")
        self.assertEqual(dialog.variables["folder"].get(), "")
        self.assertEqual(dialog._update_fields.call_count, 3)

    def test_layout_fields_hides_irrelevant_widgets_and_ssl(self) -> None:
        dialog = make_account_dialog()
        dialog.field_order = ["label", "host"]
        dialog.field_labels = {key: FakeWidget() for key in dialog.field_order}
        dialog.field_containers = {key: FakeWidget() for key in dialog.field_order}
        dialog.ssl_check = FakeWidget()
        dialog.enabled_check = FakeWidget()
        dialog.mailboxes_frame = FakeWidget()
        dialog.help_label = FakeWidget()
        dialog.buttons = FakeWidget()

        dialog._layout_fields(frozenset({"label"}), show_ssl=False)

        self.assertFalse(dialog.field_labels["label"].removed)
        self.assertTrue(dialog.field_labels["host"].removed)
        self.assertTrue(dialog.field_containers["host"].removed)
        self.assertTrue(dialog.ssl_check.removed)
        self.assertEqual(dialog.enabled_check.grid_calls[-1]["row"], 1)
        self.assertEqual(dialog.mailboxes_frame.grid_calls[-1]["row"], 2)

    def test_update_fields_covers_every_provider_auth_combination(self) -> None:
        cases = [
            ("Generic IMAP", "Password", True, "Password / app password"),
            ("Generic IMAP", "Microsoft OAuth (XOAUTH2)", False, None),
            (
                "Gmail (Google API)",
                "Google OAuth - user sign-in",
                False,
                "Google OAuth client secret (optional)",
            ),
            (
                "Gmail (Google API)",
                "Google Workspace - domain-wide delegation",
                False,
                None,
            ),
            (
                "Outlook / Microsoft 365 (Microsoft Graph)",
                "Microsoft OAuth - delegated user access",
                False,
                None,
            ),
            (
                "Outlook / Microsoft 365 (Microsoft Graph)",
                "Microsoft OAuth - application access",
                False,
                "Microsoft OAuth client secret",
            ),
        ]
        for provider, auth, show_ssl, expected_secret_label in cases:
            with self.subTest(provider=provider, auth=auth):
                dialog = make_account_dialog(provider=provider, auth=auth)
                keys = [
                    "label",
                    "provider",
                    "auth",
                    "username",
                    "host",
                    "port",
                    "folder",
                    "client_id",
                    "tenant_id",
                    "service_account_file",
                    "secret",
                    "poll",
                ]
                dialog.widgets = {key: FakeWidget() for key in keys}
                dialog.field_labels = {
                    key: FakeWidget() for key in ("secret", "client_id", "tenant_id")
                }
                dialog.service_account_button = FakeWidget()
                dialog.help_label = FakeWidget()
                dialog._layout_fields = MagicMock()

                dialog._update_fields()

                visible = app_module.visible_account_fields(
                    app_module.PROVIDER_LABELS[provider],
                    app_module.AUTH_LABELS[auth],
                )
                dialog._layout_fields.assert_called_once_with(visible, show_ssl=show_ssl)
                self.assertTrue(dialog.help_label.options["text"])
                if expected_secret_label:
                    self.assertEqual(
                        dialog.field_labels["secret"].options["text"],
                        expected_secret_label,
                    )

    def test_update_fields_replaces_incompatible_auth(self) -> None:
        dialog = make_account_dialog(
            provider="Generic IMAP",
            auth="Microsoft OAuth - application access",
        )
        keys = [
            "label",
            "provider",
            "auth",
            "username",
            "host",
            "port",
            "folder",
            "client_id",
            "tenant_id",
            "service_account_file",
            "secret",
            "poll",
        ]
        dialog.widgets = {key: FakeWidget() for key in keys}
        dialog.field_labels = {"secret": FakeWidget()}
        dialog.service_account_button = FakeWidget()
        dialog.help_label = FakeWidget()
        dialog._layout_fields = MagicMock()

        dialog._update_fields()

        self.assertEqual(dialog.variables["auth"].get(), "Password")
        self.assertEqual(
            dialog.widgets["auth"].options["values"],
            ["Password", "Microsoft OAuth (XOAUTH2)"],
        )

    @patch("mailarchive.dialogs.filedialog.askopenfilename")
    def test_choose_service_account_file_only_updates_on_selection(self, ask) -> None:
        dialog = make_account_dialog()
        ask.return_value = "/keys/workspace.json"
        dialog._choose_google_service_account_file()
        self.assertEqual(dialog.variables["service_account_file"].get(), "/keys/workspace.json")
        ask.return_value = ""
        dialog._choose_google_service_account_file()
        self.assertEqual(dialog.variables["service_account_file"].get(), "/keys/workspace.json")

    def test_save_imap_account_and_secret(self) -> None:
        dialog = make_account_dialog()
        dialog.mailboxes[0].archive_existing_messages = True

        dialog._save()

        self.assertEqual(dialog.result.account.provider, MailProvider.GENERIC_IMAP)
        self.assertEqual(dialog.result.account.host, "imap.example.com")
        self.assertTrue(dialog.result.account.mailboxes[0].archive_existing_messages)
        self.assertEqual(dialog.result.credential_updates, {"password": "secret"})
        dialog.destroy.assert_called_once_with()

    def test_save_imap_oauth_account_does_not_store_password_or_client_id(self) -> None:
        dialog = make_account_dialog(
            provider="Generic IMAP",
            auth="Microsoft OAuth (XOAUTH2)",
        )
        dialog.variables["client_id"].set("")

        dialog._save()

        self.assertEqual(dialog.result.account.auth_mode, AuthMode.OAUTH_USER)
        self.assertEqual(dialog.result.account.client_id, "")
        self.assertEqual(dialog.result.account.host, "outlook.office365.com")
        self.assertEqual(dialog.result.account.port, 993)
        self.assertTrue(dialog.result.account.use_ssl)
        self.assertEqual(dialog.result.credential_updates, {})

    @patch("mailarchive.dialogs.parse_google_service_account_file")
    def test_save_google_application_account_uses_parsed_key(self, parse_key) -> None:
        parse_key.return_value = {"type": "service_account"}
        dialog = make_account_dialog(
            provider="Gmail (Google API)",
            auth="Google Workspace - domain-wide delegation",
        )
        dialog.variables["secret"].set("")
        dialog.variables["folder"].set("")

        dialog._save()

        self.assertEqual(dialog.result.account.mailboxes[0].folders[0], "INBOX")
        self.assertEqual(dialog.result.account.client_id, "")
        self.assertEqual(
            dialog.result.credential_updates,
            {"google_service_account": {"type": "service_account"}},
        )
        parse_key.assert_called_once_with("service-account.json")

    @patch("mailarchive.dialogs.messagebox.showerror")
    def test_save_reports_validation_error_without_closing(self, showerror) -> None:
        dialog = make_account_dialog()
        dialog.variables["secret"].set("")

        dialog._save()

        self.assertIsNone(getattr(dialog, "result", None))
        self.assertIn("password", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()

    def test_edit_same_account_can_keep_existing_secret(self) -> None:
        existing = Account(
            id="account-1",
            label="Old",
            host="imap.example.com",
            username="mail@example.com",
            mailboxes=[
                Mailbox("mail@example.com", folders=["INBOX"], archive_existing_messages=True)
            ],
        )
        dialog = make_account_dialog(account=existing)
        dialog.variables["secret"].set("")

        self.assertTrue(dialog.variables["archive_existing"].get())

        dialog._save()

        self.assertEqual(dialog.result.account.id, "account-1")
        self.assertTrue(dialog.result.account.mailboxes[0].archive_existing_messages)
        self.assertEqual(dialog.result.credential_updates, {})
        self.assertFalse(dialog.result.replace_credentials)

    def test_save_google_user_secret_uses_oauth_specific_key(self) -> None:
        dialog = make_account_dialog(
            provider="Gmail (Google API)",
            auth="Google OAuth - user sign-in",
        )

        dialog._save()

        self.assertEqual(dialog.result.account.client_id, "client-id")
        self.assertEqual(
            dialog.result.credential_updates,
            {"oauth_client_secret": "secret"},
        )

    @patch("mailarchive.dialogs.messagebox.showerror")
    def test_new_google_application_requires_service_account_file(self, showerror) -> None:
        dialog = make_account_dialog(
            provider="Gmail (Google API)",
            auth="Google Workspace - domain-wide delegation",
        )
        dialog.variables["secret"].set("")
        dialog.variables["service_account_file"].set("")

        dialog._save()

        self.assertIsNone(getattr(dialog, "result", None))
        self.assertIn("service-account", showerror.call_args.args[1])
        dialog.destroy.assert_not_called()


class RuleDialogTests(unittest.TestCase):
    def test_account_selection_toggles_visibility_and_resizes_dialog(self) -> None:
        dialog = make_rule_dialog()
        dialog.account_selection_frame = FakeWidget()
        dialog._fixed_width = 560
        dialog._fit_content_height = MagicMock()
        dialog._update_account_selection()
        self.assertTrue(dialog.account_selection_frame.removed)
        dialog.account_scope_var.set("selected")
        dialog._update_account_selection()
        self.assertFalse(dialog.account_selection_frame.removed)
        self.assertEqual(dialog._fit_content_height.call_count, 2)

    def test_save_rule_uses_account_ids_for_multiple_selected_mailboxes(self) -> None:
        dialog = make_rule_dialog()
        dialog.account_scope_var.set("selected")
        dialog.account_options = [
            ("first-id", "Work"),
            ("second-id", "Personal"),
            ("third-id", "Work"),
        ]
        dialog.account_list.curselection.return_value = (0, 2)
        dialog._save()
        self.assertEqual(dialog.result.account_ids, ["first-id", "third-id"])

    @patch("mailarchive.dialogs.messagebox.showerror")
    def test_save_rule_requires_account_selection_when_scope_is_restricted(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.account_scope_var.set("selected")
        dialog._save()
        self.assertIsNone(dialog.result)
        self.assertIn("Select at least one email account", showerror.call_args.args[1])
        dialog.destroy.assert_not_called()

    def test_dialog_width_stays_fixed_while_content_height_changes(self) -> None:
        dialog = make_rule_dialog()
        dialog._fixed_width = 560
        dialog._base_height = 360
        dialog.update_idletasks = MagicMock()
        dialog.winfo_reqheight = MagicMock(side_effect=[440, 320])
        dialog.geometry = MagicMock()

        dialog._fit_content_height()
        dialog._fit_content_height()

        self.assertEqual(
            dialog.geometry.call_args_list,
            [call("560x440"), call("560x360")],
        )

    def test_update_fields_handles_all_attachments_and_text(self) -> None:
        dialog = make_rule_dialog()
        dialog.operator_box = FakeWidget()
        dialog.value_entry = FakeWidget()
        dialog.sender_fields_frame = FakeWidget()
        dialog.value_label = FakeWidget()
        dialog.value_hint = FakeWidget()

        dialog.field_var.set("All emails")
        dialog._update_fields()
        self.assertEqual(dialog.operator_box.options["state"], "disabled")
        self.assertIn("every email", dialog.value_hint.options["text"])

        dialog.field_var.set("Has attachments")
        dialog.value_var.set("")
        dialog._update_fields()
        self.assertEqual(dialog.value_var.get(), "Yes")
        self.assertEqual(dialog.operator_box.options["state"], "disabled")

        dialog.field_var.set("Subject")
        dialog._update_fields()
        self.assertEqual(dialog.operator_box.options["state"], "readonly")
        self.assertEqual(dialog.value_entry.options["state"], "normal")

        dialog.field_var.set("Sender")
        dialog._update_fields()
        self.assertEqual(dialog.value_label.options["text"], "Values")
        self.assertTrue(dialog.value_entry.removed)
        self.assertFalse(dialog.sender_fields_frame.removed)
        self.assertIn("one sender value", dialog.value_hint.options["text"])

    @patch("mailarchive.dialogs.tk.StringVar")
    def test_sender_fields_can_be_added_and_removed(self, string_var) -> None:
        dialog = make_rule_dialog()
        first = FakeVariable("first@example.com")
        second = FakeVariable("")
        dialog.sender_value_vars = [first]
        dialog._render_sender_fields = MagicMock()
        string_var.return_value = second

        dialog._add_sender_field()

        self.assertEqual(dialog.sender_value_vars, [first, second])
        string_var.assert_called_once_with(master=dialog)
        dialog._remove_sender_field(0)
        self.assertEqual(dialog.sender_value_vars, [second])
        dialog._remove_sender_field(0)
        self.assertEqual(dialog.sender_value_vars, [second])
        self.assertEqual(dialog._render_sender_fields.call_count, 2)

    @patch("mailarchive.dialogs.filedialog.askdirectory")
    def test_choose_folder_accepts_only_archive_descendants(self, askdirectory) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            inside = archive / "Finance"
            inside.mkdir(parents=True)
            outside = Path(temporary) / "outside"
            outside.mkdir()
            dialog = make_rule_dialog()
            dialog.archive_root = str(archive)

            askdirectory.return_value = str(inside)
            dialog._choose_folder()
            self.assertEqual(dialog.destination_var.get(), "Finance")

            askdirectory.return_value = str(archive)
            dialog._choose_folder()
            self.assertEqual(dialog.destination_var.get(), "")

            askdirectory.return_value = ""
            dialog._choose_folder()
            self.assertEqual(dialog.destination_var.get(), "")

            askdirectory.return_value = str(outside)
            with patch("mailarchive.dialogs.messagebox.showerror") as showerror:
                dialog._choose_folder()
            showerror.assert_called_once()
            self.assertEqual(dialog.destination_var.get(), "")

    def test_rule_destination_preview_and_save_follow_date_order_and_empty_subfolder(self) -> None:
        for label, position, folders in (
            ("No date folders", DateFolderPosition.NONE, ("Finance",)),
            (
                "Year/month before subfolder",
                DateFolderPosition.BEFORE_SUBFOLDER,
                ("YYYY", "MM", "Finance"),
            ),
            (
                "Year/month after subfolder",
                DateFolderPosition.AFTER_SUBFOLDER,
                ("Finance", "YYYY", "MM"),
            ),
        ):
            with self.subTest(label=label):
                dialog = make_rule_dialog()
                dialog.date_folder_var.set(label)
                dialog._fixed_width = 500
                dialog._fit_content_height = MagicMock()
                dialog._update_destination_preview()
                self.assertEqual(
                    dialog.destination_preview_var.get(), str(Path("/archive").joinpath(*folders))
                )
                dialog._fit_content_height.assert_called_once_with()
                dialog._save()
                self.assertEqual(dialog.result.date_folder_position, position)
                dialog.destination_var.set("")
                dialog._update_destination_preview()
                target = (
                    Path("/archive")
                    if position == DateFolderPosition.NONE
                    else Path("/archive") / "YYYY" / "MM"
                )
                self.assertEqual(dialog.destination_preview_var.get(), str(target))
                dialog._save()
                self.assertEqual(dialog.result.destination, "")

    def test_invalid_destination_is_visible_in_preview_and_blocks_save(self) -> None:
        dialog = make_rule_dialog()
        dialog.destination_var.set("../outside")
        dialog._update_destination_preview()
        self.assertIn("inside the archive", dialog.destination_preview_var.get())
        with patch("mailarchive.dialogs.messagebox.showerror") as showerror:
            dialog._save()
        showerror.assert_called_once()
        dialog.destroy.assert_not_called()

    @patch("mailarchive.rule_form.destination_path")
    def test_save_rule_preserves_id_and_builds_condition(self, destination) -> None:
        dialog = make_rule_dialog()
        dialog.rule = Rule("Old", "Old", id="rule-1")

        dialog._save()

        self.assertEqual(dialog.result.id, "rule-1")
        self.assertEqual(dialog.result.save_mode, SaveMode.EMAIL_ONLY)
        self.assertEqual(dialog.result.conditions[0].field, MailField.SUBJECT)
        self.assertEqual(dialog.result.conditions[0].value, "invoice")
        destination.assert_called_once_with(Path("/archive"), "Finance", DateFolderPosition.NONE)
        dialog.destroy.assert_called_once_with()

    @patch("mailarchive.rule_form.destination_path")
    def test_save_rule_builds_any_condition_for_each_sender(self, destination) -> None:
        dialog = make_rule_dialog()
        dialog.field_var.set("Sender")
        dialog.operator_var.set("equals")
        dialog.sender_value_vars = [
            FakeVariable("first@example.com"),
            FakeVariable("second@example.com"),
        ]

        dialog._save()

        self.assertEqual(dialog.result.match_mode, MatchMode.ANY)
        self.assertEqual(
            [condition.value for condition in dialog.result.conditions],
            ["first@example.com", "second@example.com"],
        )
        self.assertTrue(
            all(condition.field == MailField.SENDER for condition in dialog.result.conditions)
        )

    @patch("mailarchive.dialogs.messagebox.showerror")
    def test_save_rule_rejects_empty_sender_field(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.field_var.set("Sender")
        dialog.sender_value_vars = [FakeVariable("first@example.com"), FakeVariable(" ")]

        dialog._save()

        self.assertIsNone(dialog.result)
        self.assertIn("each sender field", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()

    @patch("mailarchive.dialogs.messagebox.showerror")
    def test_save_rule_reports_invalid_attachment_value(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.field_var.set("Has attachments")
        dialog.value_var.set("sometimes")

        dialog._save()

        self.assertIsNone(dialog.result)
        self.assertIn("yes or no", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()

    @patch("mailarchive.dialogs.messagebox.showerror")
    def test_save_rule_requires_name_and_text_comparison(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.name_var.set(" ")
        dialog._save()
        self.assertIn("name", showerror.call_args.args[1].lower())

        dialog.name_var.set("Invoices")
        dialog.value_var.set(" ")
        dialog._save()
        self.assertIn("comparison value", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()


class DesktopControllerTests(unittest.TestCase):
    def test_rule_overview_shows_destination_pattern_for_both_date_orders(self) -> None:
        desktop = make_desktop(
            Settings(
                "/archive",
                rules=[
                    Rule("Root", ""),
                    Rule(
                        "Before",
                        "Finance/Supplier",
                        date_folder_position=DateFolderPosition.BEFORE_SUBFOLDER,
                    ),
                    Rule(
                        "After",
                        "Finance/Supplier",
                        date_folder_position=DateFolderPosition.AFTER_SUBFOLDER,
                    ),
                ],
            )
        )
        desktop.refresh_all()
        self.assertEqual(
            [row["values"][4] for row in desktop.rule_tree.rows],
            [
                "Archive folder",
                str(Path("YYYY/MM/Finance/Supplier")),
                str(Path("Finance/Supplier/YYYY/MM")),
            ],
        )

    def test_window_quit_button_exits_even_when_close_would_hide_to_tray(self) -> None:
        desktop = make_desktop()
        desktop.tray.safe_to_hide = True
        with (
            patch("mailarchive.desktop.ttk") as widgets,
            patch("mailarchive.desktop.tk.StringVar"),
            patch.object(DesktopApp, "_build_dashboard"),
            patch.object(DesktopApp, "_build_accounts"),
            patch.object(DesktopApp, "_build_rules"),
            patch.object(DesktopApp, "_build_settings"),
            patch.object(DesktopApp, "_build_log"),
        ):
            desktop._build_ui()

        quit_button = next(
            button for button in widgets.Button.call_args_list if button.kwargs["text"] == "Quit"
        )
        quit_button.kwargs["command"]()

        desktop.runner.stop.assert_called_once_with()
        desktop.tray.stop.assert_called_once_with()
        desktop.root.destroy.assert_called_once_with()
        desktop.root.withdraw.assert_not_called()

    def test_update_check_posts_result_to_ui_and_blocks_duplicate_checks(self) -> None:
        desktop = make_desktop()
        release = Release("0.2.0")
        with (
            patch("mailarchive.desktop.threading.Thread", ImmediateThread),
            patch("mailarchive.desktop.check_for_update", return_value=release) as check,
            patch("mailarchive.desktop.messagebox.askyesno", return_value=True) as ask,
            patch("mailarchive.desktop.webbrowser.open", return_value=True) as browser,
        ):
            desktop.check_for_updates()
            desktop.check_for_updates()
            check.assert_called_once_with()
            ask.assert_not_called()
            browser.assert_not_called()
            desktop.ui_queue.get_nowait()()
        browser.assert_called_once_with(release.url)
        self.assertFalse(desktop._checking_for_updates)
        desktop.update_button.configure.assert_called_with(state="normal", text="Check for updates")

    def test_update_check_error_is_reported_on_ui_thread(self) -> None:
        desktop = make_desktop()
        with (
            patch("mailarchive.desktop.threading.Thread", ImmediateThread),
            patch("mailarchive.desktop.check_for_update", side_effect=UpdateError("offline")),
            patch("mailarchive.desktop.messagebox.showerror") as showerror,
        ):
            desktop.check_for_updates()
            showerror.assert_not_called()
            desktop.ui_queue.get_nowait()()
        showerror.assert_called_once_with("Update check failed", "offline", parent=desktop.root)
        self.assertFalse(desktop._checking_for_updates)

    def test_update_ui_handles_no_update_decline_and_browser_failure(self) -> None:
        desktop = make_desktop()
        with patch("mailarchive.desktop.messagebox.showinfo") as showinfo:
            desktop._finish_update_check()
        self.assertIn(__version__, showinfo.call_args.args[1])
        with (
            patch("mailarchive.desktop.messagebox.askyesno", return_value=False),
            patch("mailarchive.desktop.webbrowser.open") as browser,
        ):
            desktop._finish_update_check(Release("0.2.0"))
        browser.assert_not_called()
        with (
            patch("mailarchive.desktop.messagebox.askyesno", return_value=True),
            patch("mailarchive.desktop.webbrowser.open", return_value=False),
            patch("mailarchive.desktop.messagebox.showerror") as showerror,
        ):
            desktop._finish_update_check(Release("0.2.0"))
        self.assertEqual(showerror.call_args.args[0], "Could not open release page")

    def test_failed_update_thread_start_restores_button(self) -> None:
        desktop = make_desktop()
        with (
            patch("mailarchive.desktop.threading.Thread") as thread,
            patch("mailarchive.desktop.messagebox.showerror") as showerror,
        ):
            thread.return_value.start.side_effect = RuntimeError("could not start thread")
            desktop.check_for_updates()
        self.assertFalse(desktop._checking_for_updates)
        showerror.assert_called_once()

    def test_constructor_wires_services_tray_and_runner_without_real_ui(self) -> None:
        root = MagicMock()
        store = MagicMock()
        store.state_database_path.return_value = Path("/state.sqlite3")
        settings = Settings(archive_root="/archive")
        credential_store = MagicMock()
        with (
            patch.object(DesktopApp, "_configure_style"),
            patch.object(DesktopApp, "_build_ui"),
            patch.object(DesktopApp, "refresh_all"),
            patch.object(DesktopApp, "refresh_log") as refresh_log,
            patch("mailarchive.desktop.ActivityLog") as activity_log,
            patch("mailarchive.desktop.ArchiveState") as archive_state,
            patch("mailarchive.desktop.ArchiveService") as service,
            patch("mailarchive.desktop.BackgroundRunner") as runner,
            patch("mailarchive.desktop.TrayController") as tray,
            patch("mailarchive.desktop.set_start_at_login") as startup,
        ):
            desktop = DesktopApp(root, store, settings, credential_store)

        root.protocol.assert_called_once_with("WM_DELETE_WINDOW", desktop.hide_to_tray)
        archive_state.assert_called_once_with(Path("/state.sqlite3"))
        activity_log.assert_called_once_with(store.data_dir / "activity-log.sqlite3")
        refresh_log.assert_called_once_with()
        service.assert_called_once_with(
            credential_store,
            archive_state.return_value,
            desktop.on_service_event,
            progress_handler=desktop.on_run_progress,
        )
        runner.return_value.start.assert_called_once_with()
        tray.assert_called_once_with(desktop.post_ui, desktop.show, desktop.run_now, desktop.quit)
        startup.assert_called_once_with(True)
        root.after.assert_called_once_with(100, desktop._drain_ui_queue)

    def test_refresh_and_selection_reflect_settings(self) -> None:
        active = Account(
            id="active",
            label="Work",
            host="imap.example.com",
            username="work@example.com",
            poll_minutes=None,
            mailboxes=[Mailbox("work@example.com", folders=["INBOX"])],
        )
        paused = Account(
            id="paused",
            label="Personal",
            host="imap.example.com",
            username="me@example.com",
            poll_minutes=15,
            enabled=False,
            mailboxes=[Mailbox("me@example.com", folders=["INBOX"])],
        )
        rule = Rule(
            "Invoices",
            "Finance",
            [Condition(MailField.SUBJECT, value="invoice")],
            SaveMode.EMAIL_ONLY,
            id="rule-1",
        )
        desktop = make_desktop(
            Settings(
                archive_root="/archive",
                accounts=[active, paused],
                rules=[rule],
                default_poll_minutes=5,
            )
        )

        desktop.refresh_all()

        self.assertEqual(desktop.account_tree.rows[0]["values"][3], "5 min (default)")
        self.assertEqual(desktop.account_tree.rows[1]["values"][3], "15 min")
        self.assertEqual(desktop.rule_tree.rows[0]["values"][2], "All email accounts")
        self.assertEqual(desktop.rule_tree.rows[0]["values"][3], 'Subject contains "invoice"')
        self.assertEqual(desktop.account_summary.get(), "1")
        self.assertEqual(desktop.rule_summary.get(), "1")
        desktop.account_tree.selected = ("paused",)
        desktop.rule_tree.selected = ("rule-1",)
        self.assertIs(desktop._selected_account(), paused)
        self.assertIs(desktop._selected_rule(), rule)

    def test_add_account_persists_submission_and_credentials(self) -> None:
        desktop = make_desktop()
        desktop.refresh_all = MagicMock()
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        submission = AccountSubmission(account, {"password": "secret"}, False)
        dialog = SimpleNamespace(result=submission)

        with (
            patch("mailarchive.desktop.AccountDialog", return_value=dialog),
            patch("mailarchive.desktop.store_account_credentials") as store_credentials,
        ):
            desktop.add_account()

        self.assertEqual(desktop.settings.accounts, [account])
        store_credentials.assert_called_once_with(
            desktop.credential_store,
            account,
            {"password": "secret"},
            replace=False,
        )
        desktop.config_store.save.assert_called_once_with(desktop.settings)
        desktop.refresh_all.assert_called_once_with()
        desktop.root.wait_window.assert_called_once_with(dialog)

    def test_edit_account_replaces_bound_credentials(self) -> None:
        current = Account(
            id="account-1",
            label="Old",
            host="imap.old.example",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        replacement = Account(
            id=current.id,
            label="New",
            host="imap.new.example",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[current]))
        desktop._selected_account = MagicMock(return_value=current)
        desktop.refresh_all = MagicMock()
        submission = AccountSubmission(replacement, {"password": "new-secret"}, True)
        dialog = SimpleNamespace(result=submission)

        with (
            patch("mailarchive.desktop.AccountDialog", return_value=dialog),
            patch("mailarchive.desktop.store_account_credentials") as store_credentials,
        ):
            desktop.edit_account()

        self.assertEqual(desktop.settings.accounts, [replacement])
        store_credentials.assert_called_once_with(
            desktop.credential_store,
            replacement,
            {"password": "new-secret"},
            replace=True,
        )
        desktop.config_store.save.assert_called_once_with(desktop.settings)
        desktop.refresh_all.assert_called_once_with()

    def test_account_edit_does_not_block_the_ui_while_authorization_is_active(self) -> None:
        account = Account(
            id="account-1",
            label="Outlook",
            username="mail@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop._authorizing_account_ids.add(account.id)

        with (
            patch("mailarchive.desktop.AccountDialog") as account_dialog,
            patch("mailarchive.desktop.messagebox.showinfo") as showinfo,
        ):
            desktop.edit_account()

        account_dialog.assert_not_called()
        self.assertEqual(showinfo.call_args.args[0], "Authorization in progress")

    def test_account_commit_fails_fast_when_credentials_are_busy(self) -> None:
        desktop = make_desktop()
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        credential_lock = MagicMock()
        credential_lock.acquire.return_value = False

        with (
            patch(
                "mailarchive.desktop.account_credential_lock",
                return_value=credential_lock,
            ),
            self.assertRaisesRegex(RuntimeError, "currently authorizing or refreshing"),
        ):
            desktop._commit_account_submission(
                AccountSubmission(account, {"password": "secret"}, False)
            )

        credential_lock.acquire.assert_called_once_with(blocking=False)
        credential_lock.release.assert_not_called()

    def test_failed_account_add_rolls_back_settings_and_credentials(self) -> None:
        store = MemoryCredentialStore()
        desktop = make_desktop()
        desktop.credential_store = store
        desktop.config_store.save.side_effect = RuntimeError("config is read-only")
        desktop.refresh_all = MagicMock()
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        submission = AccountSubmission(account, {"password": "new-secret"}, False)

        with self.assertRaisesRegex(RuntimeError, "config is read-only"):
            desktop._commit_account_submission(submission)

        self.assertEqual(desktop.settings.accounts, [])
        self.assertIsNone(store.get(account.id))
        desktop.refresh_all.assert_called_once_with()

    def test_account_without_credential_changes_does_not_touch_store(self) -> None:
        desktop = make_desktop()
        desktop.refresh_all = MagicMock()
        desktop.credential_store.get.side_effect = RuntimeError("unavailable")
        account = Account(
            id="gmail-account",
            label="Gmail",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            username="mail@example.com",
            client_id="client-id",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )

        desktop._commit_account_submission(AccountSubmission(account, {}, False))

        self.assertEqual(desktop.settings.accounts, [account])
        desktop.credential_store.get.assert_not_called()
        desktop.credential_store.set.assert_not_called()
        desktop.credential_store.delete.assert_not_called()

    def test_failed_account_edit_restores_previous_credentials(self) -> None:
        store = MemoryCredentialStore()
        current = Account(
            id="account-1",
            label="Old",
            host="imap.old.example",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        replacement = Account(
            id=current.id,
            label="New",
            host="imap.new.example",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        update_credential_data(store, current.id, password="old-secret")
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[current]))
        desktop.credential_store = store
        desktop.config_store.save.side_effect = RuntimeError("config is read-only")
        desktop.refresh_all = MagicMock()
        submission = AccountSubmission(replacement, {"password": "new-secret"}, True)

        with self.assertRaisesRegex(RuntimeError, "config is read-only"):
            desktop._commit_account_submission(submission, replacing=current)

        self.assertEqual(desktop.settings.accounts, [current])
        self.assertEqual(
            load_credential_data(store, current.id),
            {"password": "old-secret"},
        )
        desktop.refresh_all.assert_called_once_with()

    def test_authorize_paths_explain_noninteractive_accounts(self) -> None:
        desktop = make_desktop()
        with patch("mailarchive.desktop.messagebox.showinfo") as showinfo:
            desktop._selected_account = MagicMock(return_value=None)
            desktop.authorize_selected_account()
            self.assertEqual(showinfo.call_args.args[0], "Select an account")

            desktop._selected_account.return_value = Account(
                label="IMAP",
                host="imap.example.com",
                username="mail@example.com",
                mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
            )
            desktop.authorize_selected_account()
            self.assertEqual(showinfo.call_args.args[0], "Authorization not required")

            desktop._selected_account.return_value = Account(
                label="Workspace",
                username="mail@example.com",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_APPLICATION,
                mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
            )
            desktop.authorize_selected_account()
            self.assertIn("Google Workspace", showinfo.call_args.args[1])

            desktop._selected_account.return_value = Account(
                label="Graph",
                username="mail@example.com",
                provider=MailProvider.MICROSOFT_GRAPH,
                auth_mode=AuthMode.OAUTH_APPLICATION,
                client_id="client-id",
                tenant_id="tenant-id",
                mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
            )
            desktop.authorize_selected_account()
            self.assertIn("Microsoft application", showinfo.call_args.args[1])

    def test_interactive_authorization_reports_success_and_failure(self) -> None:
        account = Account(
            id="gmail",
            label="Gmail",
            username="mail@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop.on_service_event = MagicMock()
        ImmediateThread.created.clear()

        with (
            patch("mailarchive.desktop.threading.Thread", ImmediateThread),
            patch("mailarchive.desktop.authorize_account") as authorize,
        ):
            desktop.authorize_selected_account()
            event = desktop.on_service_event.call_args.args[0]
            self.assertEqual(event.level, EventLevel.SUCCESS)
            self.assertEqual(event.account_id, "gmail")

            authorize.side_effect = RuntimeError("denied")
            desktop.authorize_selected_account()
            event = desktop.on_service_event.call_args.args[0]
            self.assertEqual(event.level, EventLevel.ERROR)
            self.assertIn("denied", event.message)

        self.assertTrue(all(thread.daemon for thread in ImmediateThread.created))
        self.assertEqual(desktop.tray.set_state.call_args_list[0].args[0], "busy")

    def test_authorize_again_replaces_pending_attempt_without_stale_cleanup(self) -> None:
        account = Account(
            id="gmail",
            label="Gmail",
            username="mail@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop.on_service_event = MagicMock()
        with (
            patch("mailarchive.desktop.threading.Thread") as thread,
            patch("mailarchive.desktop.authorize_account") as authorize,
        ):
            desktop.authorize_selected_account()
            old_worker = thread.call_args.kwargs["target"]
            old_attempt = desktop._authorization_attempts[account.id]
            desktop.authorize_selected_account()
            new_worker = thread.call_args.kwargs["target"]
            new_attempt = desktop._authorization_attempts[account.id]
            self.assertTrue(old_attempt.is_set())
            self.assertFalse(new_attempt.is_set())
            for error in (None, RuntimeError("old failure")):
                authorize.side_effect = error
                old_worker()
                self.assertIs(desktop._authorization_attempts[account.id], new_attempt)
                self.assertIn(account.id, desktop._authorizing_account_ids)
                desktop.on_service_event.assert_not_called()
            authorize.side_effect = RuntimeError("new failure")
            new_worker()
            self.assertNotIn(account.id, desktop._authorizing_account_ids)
            self.assertNotIn(account.id, desktop._authorization_attempts)
            desktop.authorize_selected_account()
            self.assertEqual(thread.call_count, 3)

    def test_imap_oauth_uses_interactive_authorization(self) -> None:
        account = Account(
            id="imap-oauth",
            label="Hotmail",
            host="outlook.office365.com",
            username="mail@hotmail.com",
            auth_mode=AuthMode.OAUTH_USER,
            mailboxes=[Mailbox("mail@hotmail.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop.on_service_event = MagicMock()
        ImmediateThread.created.clear()

        with (
            patch("mailarchive.desktop.threading.Thread", ImmediateThread),
            patch("mailarchive.desktop.authorize_account") as authorize,
        ):
            desktop.authorize_selected_account()

        authorize.assert_called_once_with(account, desktop.credential_store, cancelled=ANY)
        self.assertEqual(desktop.on_service_event.call_args.args[0].level, EventLevel.SUCCESS)

    def test_remove_account_rolls_back_failed_persistence(self) -> None:
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop._persist = MagicMock(side_effect=RuntimeError("disk full"))
        desktop.refresh_all = MagicMock()

        with (
            patch("mailarchive.desktop.messagebox.askyesno", return_value=True),
            patch("mailarchive.desktop.messagebox.showerror") as showerror,
        ):
            desktop.remove_account()

        self.assertEqual(desktop.settings.accounts, [account])
        desktop.refresh_all.assert_called_once_with()
        showerror.assert_called_once()
        desktop.credential_store.delete.assert_not_called()

    def test_remove_account_does_not_change_credentials_during_an_archive_run(self) -> None:
        account = Account("Work", "imap.example.org", "mail@example.org")
        desktop = make_desktop(Settings("/archive", accounts=[account]))
        desktop.service.account_change.side_effect = RuntimeError("archive run is in progress")
        desktop._selected_account = MagicMock(return_value=account)
        with (
            patch("mailarchive.desktop.messagebox.askyesno", return_value=True),
            patch("mailarchive.desktop.messagebox.showinfo") as showinfo,
        ):
            desktop.remove_account()

        self.assertEqual(desktop.settings.accounts, [account])
        desktop.config_store.save.assert_not_called()
        desktop.credential_store.delete.assert_not_called()
        showinfo.assert_called_once_with(
            "Account busy", "archive run is in progress", parent=desktop.root
        )

    def test_remove_account_warns_when_credential_cleanup_fails(self) -> None:
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
            mailboxes=[Mailbox("mail@example.com", folders=["INBOX"])],
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop._persist = MagicMock()
        desktop.credential_store.delete.side_effect = RuntimeError("locked")

        with (
            patch("mailarchive.desktop.messagebox.askyesno", return_value=True),
            patch("mailarchive.desktop.messagebox.showwarning") as showwarning,
        ):
            desktop.remove_account()

        self.assertEqual(desktop.settings.accounts, [])
        desktop._persist.assert_called_once_with()
        showwarning.assert_called_once()

    def test_add_rule_inserts_before_catch_all_and_move_keeps_selection(self) -> None:
        catch_all = Rule("All", "Inbox", [Condition(MailField.ALL)], id="catch-all")
        new_rule = Rule(
            "Invoices",
            "Finance",
            [Condition(MailField.SUBJECT, value="invoice")],
            id="invoices",
        )
        desktop = make_desktop(Settings(archive_root="/archive", rules=[catch_all]))
        dialog = SimpleNamespace(result=new_rule)

        with patch("mailarchive.desktop.RuleDialog", return_value=dialog) as rule_dialog:
            desktop.add_rule()

        rule_dialog.assert_called_once_with(
            desktop.root, desktop.settings.archive_root, accounts=desktop.settings.accounts
        )

        self.assertEqual(desktop.settings.rules, [new_rule, catch_all])
        desktop.rule_tree.selected = ("invoices",)
        desktop.move_rule(1)
        self.assertEqual(desktop.settings.rules, [catch_all, new_rule])
        self.assertEqual(desktop.rule_tree.selected, ("invoices",))
        self.assertEqual(desktop.config_store.save.call_count, 2)
        self.assertEqual(
            desktop.config_store.save.call_args_list[0].args[0].rules, [new_rule, catch_all]
        )
        self.assertEqual(desktop.config_store.save.call_args.args[0].rules, [catch_all, new_rule])

    def test_edit_rule_passes_accounts_and_keeps_restricted_scope(self) -> None:
        account = Account(
            "Work",
            username="work@example.com",
            id="work",
            mailboxes=[Mailbox("work@example.com", folders=["INBOX"])],
        )
        original = Rule("Old", "Work", id="rule", account_ids=["work"])
        replacement = Rule("Updated", "Work", id=original.id, account_ids=["work"])
        desktop = make_desktop(Settings("/archive", accounts=[account], rules=[original]))
        desktop._selected_rule = MagicMock(return_value=original)
        with patch(
            "mailarchive.desktop.RuleDialog", return_value=SimpleNamespace(result=replacement)
        ) as rule_dialog:
            desktop.edit_rule()
        rule_dialog.assert_called_once_with(desktop.root, "/archive", original, accounts=[account])
        self.assertEqual(desktop.settings.rules, [replacement])
        desktop.config_store.save.assert_called_once_with(desktop.settings)

    def test_remove_rule_enforces_at_least_one_and_confirmation(self) -> None:
        rule = Rule("All", "Inbox", id="rule-1")
        desktop = make_desktop(Settings(archive_root="/archive", rules=[rule]))
        desktop._selected_rule = MagicMock(return_value=rule)

        with patch("mailarchive.desktop.messagebox.showerror") as showerror:
            desktop.remove_rule()
        showerror.assert_called_once()
        desktop.config_store.save.assert_not_called()

        second = Rule("Second", "Other", id="rule-2")
        desktop.settings.rules.append(second)
        with patch("mailarchive.desktop.messagebox.askyesno", return_value=True):
            desktop.remove_rule()
        self.assertEqual(desktop.settings.rules, [second])
        desktop.config_store.save.assert_called_once_with(desktop.settings)

    def test_failed_rule_changes_preserve_active_rules_and_display(self) -> None:
        for operation in ("add", "edit", "remove", "move"):
            with self.subTest(operation=operation):
                original = Rule("First", id="first")
                fallback = Rule("Second", id="second")
                settings = Settings("/archive", rules=[original, fallback])
                desktop = make_desktop(settings)
                desktop.refresh_all()
                displayed_rows = list(desktop.rule_tree.rows)
                desktop.rule_tree.selected = (original.id,)
                desktop.config_store.save.side_effect = OSError("disk full")
                replacement = Rule("Replacement", id=original.id)
                with (
                    patch(
                        "mailarchive.desktop.RuleDialog",
                        return_value=SimpleNamespace(result=replacement),
                    ),
                    patch("mailarchive.desktop.messagebox.askyesno", return_value=True),
                    patch("mailarchive.desktop.messagebox.showerror") as showerror,
                ):
                    if operation == "move":
                        desktop.move_rule(1)
                    else:
                        getattr(desktop, f"{operation}_rule")()

                self.assertIs(desktop.settings, settings)
                self.assertEqual(settings.rules, [original, fallback])
                self.assertEqual(desktop.rule_tree.rows, displayed_rows)
                self.assertEqual(desktop.rule_tree.selection(), (original.id,))
                showerror.assert_called_once_with(
                    "Rules not saved", "disk full", parent=desktop.root
                )

    def test_account_credentials_cannot_change_during_a_multifolder_run(self) -> None:
        from mailarchive.mail_identity import imap_scope

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            account = Account(
                "Work",
                "old.example.org",
                "mail@example.org",
                id="work",
                mailboxes=[Mailbox("mail@example.org", folders=["INBOX", "Archive"])],
            )
            desktop = make_desktop(Settings(str(root / "archive"), accounts=[account]))
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "old password")
            desktop.credential_store = credentials
            entered, release = threading.Event(), threading.Event()
            connections = []

            class BlockingMailbox:
                def fetch_messages(self, target, password, should_fetch, *, sync=None):
                    connections.append((target.account.host, password))

                    def messages():
                        if len(connections) == 1:
                            entered.set()
                            if not release.wait(timeout=2):
                                raise RuntimeError("Test did not release the connection.")
                        sync.next_cursor = "0"
                        yield from ()

                    return imap_scope(target, "42"), messages()

            service = ArchiveService(
                credentials, ArchiveState(root / "state.sqlite3"), mailbox=BlockingMailbox()
            )
            desktop.service = service
            replacement = Account(
                "Work",
                "new.example.org",
                account.username,
                id=account.id,
                mailboxes=account.mailboxes,
            )
            submission = AccountSubmission(replacement, {"password": "new password"}, True)
            results = []
            worker = threading.Thread(
                target=lambda: results.extend(service.run_once(desktop.settings))
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                with self.assertRaisesRegex(RuntimeError, "archive run is in progress"):
                    desktop._commit_account_submission(submission, replacing=account)
                self.assertIs(desktop.settings.accounts[0], account)
                self.assertEqual(credentials.get(account.id), "old password")
                desktop.config_store.save.assert_not_called()
            finally:
                release.set()
                worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0].failed, 0)
            self.assertEqual(connections, [(account.host, "old password")] * 2)
            desktop._commit_account_submission(submission, replacing=account)
            self.assertEqual(service.run_once(desktop.settings)[0].failed, 0)
            self.assertEqual(connections[2:], [(replacement.host, "new password")] * 2)

    def test_file_choosers_and_default_database_apply_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive"
            database = root / "custom.sqlite3"
            default_database = root / "default.sqlite3"
            desktop = make_desktop(Settings(archive_root=str(root)))
            desktop.state = SimpleNamespace(database_path=default_database)
            desktop.database_var.set(str(default_database))
            desktop.config_store.default_state_database_path = default_database
            desktop.service.relocate_state_database.side_effect = lambda path: SimpleNamespace(
                database_path=path
            )
            with (
                patch("mailarchive.desktop.filedialog.askdirectory", return_value=str(archive)),
                patch(
                    "mailarchive.desktop.filedialog.asksaveasfilename",
                    return_value=str(database),
                ),
                patch("mailarchive.desktop.set_start_at_login") as startup,
                patch("mailarchive.desktop.messagebox.showinfo") as showinfo,
            ):
                desktop.choose_archive()
                self.assertEqual(desktop.settings.archive_root, str(archive.resolve()))
                self.assertTrue(archive.is_dir())
                desktop.choose_state_database()
                self.assertEqual(desktop.settings.state_database_path, str(database.resolve()))
                self.assertEqual(desktop.state.database_path, database.resolve())
                desktop.use_default_state_database()

            self.assertEqual(desktop.settings.state_database_path, "")
            self.assertEqual(desktop.state.database_path, default_database.resolve())
            self.assertEqual(desktop.database_var.get(), str(default_database.resolve()))
            self.assertEqual(desktop.config_store.save.call_count, 3)
            startup.assert_not_called()
            showinfo.assert_not_called()

    def test_cancelled_settings_choosers_do_not_save(self) -> None:
        desktop = make_desktop()
        with (
            patch("mailarchive.desktop.filedialog.askdirectory", return_value=""),
            patch("mailarchive.desktop.filedialog.asksaveasfilename", return_value=""),
        ):
            desktop.choose_archive()
            desktop.choose_state_database()
        desktop.config_store.save.assert_not_called()
        desktop.service.relocate_state_database.assert_not_called()

    def test_checkboxes_apply_without_saving_unfinished_text_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite3"
            desktop = make_desktop(Settings(archive_root=str(root)))
            desktop.state = SimpleNamespace(database_path=database)
            desktop.config_store.default_state_database_path = database
            desktop.settings_tab = MagicMock()
            with (
                patch("mailarchive.desktop.ttk") as widgets,
                patch("mailarchive.desktop.tk.StringVar", side_effect=FakeVariable),
                patch("mailarchive.desktop.tk.BooleanVar", side_effect=FakeVariable),
            ):
                desktop._build_settings()
            desktop.archive_var.set("")
            desktop.database_var.set("")
            desktop.poll_var.set("unfinished")
            with (
                patch("mailarchive.desktop.set_start_at_login") as startup,
                patch("mailarchive.desktop.messagebox.showinfo") as showinfo,
                patch("mailarchive.desktop.messagebox.showerror") as showerror,
            ):
                for checkbox in widgets.Checkbutton.call_args_list:
                    checkbox.kwargs["variable"].set(False)
                    checkbox.kwargs["command"]()

            self.assertFalse(desktop.settings.start_at_login)
            self.assertFalse(desktop.settings.minimize_to_tray)
            self.assertFalse(desktop.settings.warn_on_error)
            self.assertEqual(desktop.settings.archive_root, str(root.resolve()))
            self.assertEqual(desktop.settings.default_poll_minutes, 5)
            self.assertEqual(desktop.settings.state_database_path, "")
            self.assertEqual(desktop.poll_var.get(), "unfinished")
            self.assertEqual(desktop.archive_var.get(), "")
            self.assertEqual(desktop.database_var.get(), "")
            self.assertEqual(desktop.config_store.save.call_count, 3)
            startup.assert_called_once_with(False)
            desktop.service.relocate_state_database.assert_not_called()
            showinfo.assert_not_called()
            showerror.assert_not_called()

            desktop.tray.safe_to_hide = True
            desktop.hide_to_tray()
            desktop.root.destroy.assert_called_once_with()
            desktop.root.withdraw.assert_not_called()

    def test_failed_checkbox_save_restores_setting_and_checkbox(self) -> None:
        for field, variable in [
            ("start_at_login", "startup_var"),
            ("minimize_to_tray", "minimize_var"),
            ("warn_on_error", "warning_var"),
        ]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                desktop = make_desktop(Settings(archive_root=str(root)))
                database = root / "state.sqlite3"
                desktop.state = SimpleNamespace(database_path=database)
                desktop.config_store.default_state_database_path = database
                desktop.config_store.save.side_effect = OSError("read-only")
                getattr(desktop, variable).set(False)
                with (
                    patch("mailarchive.desktop.set_start_at_login") as startup,
                    patch("mailarchive.desktop.messagebox.showerror") as showerror,
                ):
                    desktop.save_settings(field)
                self.assertTrue(getattr(desktop.settings, field))
                self.assertTrue(getattr(desktop, variable).get())
                self.assertFalse(desktop._saving_settings)
                showerror.assert_called_once()
                if field == "start_at_login":
                    self.assertEqual(startup.call_args_list, [call(False), call(True)])
                else:
                    startup.assert_not_called()

    def test_text_settings_apply_on_enter_or_focus_out_without_duplicate_saves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop = make_desktop(Settings(archive_root=str(root)))
            database = root / "state.sqlite3"
            desktop.state = SimpleNamespace(database_path=database)
            desktop.config_store.default_state_database_path = database
            desktop.settings_tab = MagicMock()
            entries = {}

            def make_entry(_parent, **options):
                entry = MagicMock()
                entries[id(options["textvariable"])] = entry
                return entry

            with (
                patch("mailarchive.desktop.ttk") as widgets,
                patch("mailarchive.desktop.tk.StringVar", side_effect=FakeVariable),
                patch("mailarchive.desktop.tk.BooleanVar", side_effect=FakeVariable),
            ):
                widgets.Entry.side_effect = make_entry
                desktop._build_settings()
            desktop.service.relocate_state_database.side_effect = lambda path: SimpleNamespace(
                database_path=path
            )
            with (
                patch("mailarchive.desktop.set_start_at_login") as startup,
                patch("mailarchive.desktop.messagebox.showinfo") as showinfo,
            ):
                for variable, value, event in [
                    (desktop.archive_var, str(root / "archive"), "<Return>"),
                    (desktop.poll_var, "010", "<FocusOut>"),
                    (desktop.database_var, str(root / "custom.sqlite3"), "<Return>"),
                ]:
                    variable.set(value)
                    entry = entries[id(variable)]
                    bindings = {args.args[0]: args.args[1] for args in entry.bind.call_args_list}
                    bindings[event](None)
                    bindings["<FocusOut>"](None)

            self.assertEqual(desktop.settings.archive_root, str((root / "archive").resolve()))
            self.assertEqual(desktop.settings.default_poll_minutes, 10)
            self.assertEqual(desktop.poll_var.get(), "10")
            self.assertEqual(
                desktop.settings.state_database_path, str((root / "custom.sqlite3").resolve())
            )
            self.assertEqual(desktop.config_store.save.call_count, 3)
            startup.assert_not_called()
            showinfo.assert_not_called()
            self.assertNotIn(
                "Save settings", [button.kwargs["text"] for button in widgets.Button.call_args_list]
            )

    def test_closing_window_persists_focused_text_field_across_restart(self) -> None:
        for action in ("hide_to_tray", "quit"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                desktop = make_desktop(Settings(archive_root=str(root)))
                desktop.config_store = ConfigStore(root / "data")
                desktop.state = SimpleNamespace(
                    database_path=desktop.config_store.default_state_database_path
                )
                entry = MagicMock()
                desktop._bind_setting_entry(entry, "default_poll_minutes")
                desktop.root.focus_get.return_value = entry
                desktop.poll_var.set("17")
                desktop.tray.safe_to_hide = True

                getattr(desktop, action)()

                reloaded = ConfigStore(root / "data").load()
                self.assertEqual(reloaded.default_poll_minutes, 17)
                self.assertEqual(desktop.settings.default_poll_minutes, 17)
                if action == "hide_to_tray":
                    desktop.root.withdraw.assert_called_once_with()
                    desktop.root.destroy.assert_not_called()
                else:
                    desktop.root.destroy.assert_called_once_with()

    def test_save_settings_relocates_state_and_persists_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive"
            old_database = root / "old.sqlite3"
            new_database = root / "new.sqlite3"
            settings = Settings(
                archive_root=str(root / "old-archive"),
                start_at_login=False,
                default_poll_minutes=5,
            )
            desktop = make_desktop(settings)
            desktop.archive_var.set(str(archive))
            desktop.database_var.set(str(new_database))
            desktop.poll_var.set("10")
            desktop.startup_var.set(True)
            desktop.state = SimpleNamespace(database_path=old_database)
            relocated = SimpleNamespace(database_path=new_database)
            desktop.service.relocate_state_database.return_value = relocated
            desktop.config_store.default_state_database_path = root / "default.sqlite3"
            desktop.refresh_all = MagicMock()

            with (
                patch("mailarchive.desktop.set_start_at_login") as startup,
                patch("mailarchive.desktop.messagebox.showinfo") as showinfo,
            ):
                desktop.save_settings()

        self.assertIs(desktop.state, relocated)
        self.assertEqual(desktop.settings.default_poll_minutes, 10)
        self.assertEqual(desktop.settings.archive_root, str(archive.resolve()))
        self.assertEqual(desktop.settings.state_database_path, str(new_database.resolve()))
        desktop.config_store.save.assert_called_once_with(desktop.settings)
        startup.assert_called_once_with(True)
        desktop.refresh_all.assert_called_once_with()
        showinfo.assert_not_called()

    def test_save_settings_rolls_back_state_and_startup_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_database = root / "old.sqlite3"
            new_database = root / "new.sqlite3"
            settings = Settings(
                archive_root=str(root / "archive"),
                start_at_login=False,
            )
            desktop = make_desktop(settings)
            desktop.archive_var.set(settings.archive_root)
            desktop.database_var.set(str(new_database))
            desktop.startup_var.set(True)
            desktop.state = SimpleNamespace(database_path=old_database)
            moved = SimpleNamespace(database_path=new_database)
            restored = SimpleNamespace(database_path=old_database)
            desktop.service.relocate_state_database.side_effect = [moved, restored]
            desktop.config_store.default_state_database_path = root / "default.sqlite3"
            desktop.config_store.save.side_effect = RuntimeError("read-only")

            with (
                patch("mailarchive.desktop.set_start_at_login") as startup,
                patch("mailarchive.desktop.messagebox.showerror") as showerror,
            ):
                desktop.save_settings()

        self.assertIs(desktop.settings, settings)
        self.assertIs(desktop.state, restored)
        self.assertEqual(startup.call_args_list, [call(True), call(False)])
        self.assertEqual(
            desktop.service.relocate_state_database.call_args_list,
            [call(new_database.resolve()), call(old_database.resolve())],
        )
        showerror.assert_called_once()
        self.assertEqual(desktop.database_var.get(), str(old_database))
        self.assertFalse(desktop.startup_var.get())

    def test_database_save_and_rollback_exclude_polling_and_preserve_history(self) -> None:
        for save_fails in (False, True):
            with self.subTest(save_fails=save_fails):
                self._assert_database_change_preserves_history(save_fails)

    def _assert_database_change_preserves_history(self, save_fails: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old_database, new_database = root / "old.sqlite3", root / "new.sqlite3"
            account = Account(
                "Work",
                "imap.example.org",
                "mail@example.org",
                mailboxes=[
                    Mailbox("mail@example.org", folders=["INBOX"], archive_existing_messages=True)
                ],
            )
            settings = Settings(
                str(root / "archive"),
                accounts=[account],
                state_database_path=str(old_database),
            )
            desktop = make_desktop(settings)
            desktop.config_store = ConfigStore(root / "data")
            desktop.config_store.save(settings)
            credentials = MemoryCredentialStore()
            credentials.set(account.id, "password")
            downloaded = []
            observed_cursors = []

            class MailboxSource:
                def fetch_messages(self, target, password, should_fetch, *, sync=None):
                    scope = imap_scope(target, "42")

                    def messages():
                        observed_cursors.append(sync.cursor_for(scope.synchronization_namespace))
                        if should_fetch(scope, "1"):
                            downloaded.append("1")
                            yield RemoteMessage("1", sample_mail())
                        sync.next_cursor = "1"

                    return scope, messages()

            desktop.service = ArchiveService(
                credentials, ArchiveState(old_database), mailbox=MailboxSource()
            )
            desktop.state = desktop.service.state
            desktop.database_var.set(str(new_database))
            runner = BackgroundRunner(desktop.service, lambda: desktop.settings)
            runner._last_run[account.id] = 99.0
            self.assertTrue(runner.run_now())
            original_save = desktop.config_store.save

            def save_with_concurrent_poll(candidate):
                worker = threading.Thread(target=runner._run_due_accounts)
                worker.start()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive(), "Polling blocked the settings save.")
                if save_fails:
                    raise OSError("config save failed")
                original_save(candidate)

            with (
                patch.object(desktop.config_store, "save", side_effect=save_with_concurrent_poll),
                patch("mailarchive.runner.time.monotonic", return_value=100.0),
                patch("mailarchive.desktop.messagebox.showerror") as showerror,
            ):
                desktop.save_settings("state_database_path")
                expected_database = old_database if save_fails else new_database
                self.assertEqual(desktop.state.database_path, expected_database)
                self.assertIs(desktop.service.state, desktop.state)
                self.assertEqual(desktop.settings.state_database_path, str(expected_database))
                self.assertEqual(downloaded, [])
                self.assertEqual(runner._last_run[account.id], 99.0)
                self.assertTrue(runner._force)

                runner._run_due_accounts()

            self.assertEqual(showerror.call_count, int(save_fails))
            self.assertEqual(downloaded, ["1"])
            self.assertFalse(runner._force)
            reloaded_settings = desktop.config_store.load()
            reloaded_state = ArchiveState(
                desktop.config_store.state_database_path(reloaded_settings)
            )
            namespace = imap_namespace(account, "42")
            self.assertEqual(reloaded_state.processed_message_ids(account.id, namespace), {"1"})
            restarted = ArchiveService(credentials, reloaded_state, mailbox=MailboxSource())
            result = restarted.run_once(reloaded_settings)[0]
            self.assertEqual(result.archived, 0)
            self.assertEqual(result.already_processed, 1)
            self.assertEqual(downloaded, ["1"])
            self.assertEqual(observed_cursors, [None, "1"])

    def test_save_settings_rejects_bad_poll_interval_before_side_effects(self) -> None:
        desktop = make_desktop()
        desktop.poll_var.set("0")
        with patch("mailarchive.desktop.messagebox.showerror") as showerror:
            desktop.save_settings("default_poll_minutes")
        self.assertIn("between 1 and 1440", showerror.call_args.args[1])
        desktop.service.relocate_state_database.assert_not_called()
        desktop.config_store.save.assert_not_called()
        self.assertEqual(desktop.poll_var.get(), "5")

    def test_save_settings_reports_all_failed_restorations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_database, new_database = root / "old.sqlite3", root / "new.sqlite3"
            settings = Settings(str(root / "archive"), start_at_login=False)
            desktop = make_desktop(settings)
            desktop.state = SimpleNamespace(database_path=old_database)
            desktop.database_var.set(str(new_database))
            desktop.startup_var.set(True)
            desktop.config_store.default_state_database_path = old_database
            moved = SimpleNamespace(database_path=new_database)
            desktop.service.relocate_state_database.side_effect = [moved, OSError("database busy")]
            desktop.config_store.save.side_effect = OSError("disk full")
            with (
                patch(
                    "mailarchive.desktop.set_start_at_login",
                    side_effect=[None, OSError("startup blocked")],
                ),
                patch("mailarchive.desktop.messagebox.showerror") as showerror,
            ):
                desktop.save_settings()

            message = showerror.call_args.args[1]
            for detail in ("disk full", "database busy", "startup blocked"):
                self.assertIn(detail, message)
            self.assertIs(desktop.settings, settings)
            self.assertIs(desktop.state, moved)
            self.assertEqual(desktop.database_var.get(), str(new_database))

    def test_failed_database_relocation_does_not_restore_unapplied_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old_database, new_database = root / "old.sqlite3", root / "new.sqlite3"
            desktop = make_desktop(Settings(str(root / "archive"), start_at_login=False))
            previous_state = SimpleNamespace(database_path=old_database)
            desktop.state = previous_state
            desktop.database_var.set(str(new_database))
            desktop.startup_var.set(True)
            desktop.config_store.default_state_database_path = old_database
            desktop.service.relocate_state_database.side_effect = RuntimeError(
                "archive run in progress"
            )
            with (
                patch("mailarchive.desktop.set_start_at_login") as startup,
                patch("mailarchive.desktop.messagebox.showerror") as showerror,
            ):
                desktop.save_settings()

            desktop.service.relocate_state_database.assert_called_once_with(new_database)
            desktop.config_store.save.assert_not_called()
            startup.assert_not_called()
            self.assertIs(desktop.state, previous_state)
            self.assertEqual(showerror.call_args.args[1], "archive run in progress")

    def test_queue_event_display_and_notifications(self) -> None:
        desktop = make_desktop()
        callback = MagicMock()
        desktop.post_ui(callback)
        desktop.root.after.reset_mock()
        desktop._drain_ui_queue()
        callback.assert_called_once_with()
        desktop.root.after.assert_called_once_with(100, desktop._drain_ui_queue)

        event = ServiceEvent(
            EventLevel.ERROR,
            "Authentication failed",
            created_at=datetime(2026, 9, 12, 8, 30, 0),
        )
        desktop.activity_log.page.return_value = ActivityPage([event], 1, 0)
        desktop.on_service_event(event)
        desktop.activity_log.record.assert_called_once_with(event)
        desktop._drain_ui_queue()
        self.assertEqual(desktop.progress_var.get(), "Authentication failed")
        self.assertEqual(desktop.log_tree.rows[0]["values"][0], "2026-09-12 08:30:00")
        desktop.tray.set_state.assert_called_with("error", "MailArchive - problem detected")
        desktop.tray.notify.assert_called_once_with("Authentication failed")

        desktop._display_event(ServiceEvent(EventLevel.WARNING, "Slow"))
        desktop.tray.set_state.assert_called_with("warning", "MailArchive - attention required")
        desktop._display_event(ServiceEvent(EventLevel.SUCCESS, "Done"))
        desktop.tray.set_state.assert_called_with("ok", "MailArchive - ready")
        desktop._display_event(ServiceEvent(EventLevel.INFO, "Checking"))

    def test_run_visibility_and_quit_lifecycle(self) -> None:
        desktop = make_desktop()
        desktop.run_now()
        self.assertEqual(desktop.progress_var.get(), "Waiting for the archive run to start...")
        desktop.runner.run_now.assert_called_once_with()

        desktop._display_progress(RunProgress("Hotmail: Downloading email 1"))
        desktop.run_now()
        desktop.runner.run_now.assert_called_once_with()
        self.assertEqual(desktop.progress_var.get(), "Hotmail: Downloading email 1")

        desktop.show()
        desktop.root.deiconify.assert_called_once_with()
        desktop.root.lift.assert_called_once_with()
        desktop.root.focus_force.assert_called_once_with()

        desktop.tray.safe_to_hide = True
        desktop.hide_to_tray()
        desktop.root.withdraw.assert_called_once_with()
        desktop.settings.minimize_to_tray = False
        desktop.quit = MagicMock()
        desktop.hide_to_tray()
        desktop.quit.assert_called_once_with()

        desktop.quit = DesktopApp.quit.__get__(desktop, DesktopApp)
        desktop.quit()
        desktop.runner.stop.assert_called_once_with()

        desktop.tray.stop.assert_called_once_with()
        desktop.root.destroy.assert_called_once_with()
        desktop.quit()
        desktop.runner.stop.assert_called_once_with()

    def test_progress_is_dispatched_to_ui_without_persisting_or_refreshing_log(self) -> None:
        desktop = make_desktop()
        progress = RunProgress("Hotmail: Downloading email 3")
        desktop.on_run_progress(progress)
        self.assertFalse(desktop._archive_running)

        with patch("mailarchive.desktop.time.monotonic", return_value=10.0):
            desktop._drain_ui_queue()

        self.assertTrue(desktop._archive_running)
        self.assertEqual(desktop.progress_var.get(), progress.message)
        desktop.archive_button.configure.assert_called_with(state="disabled", text="Archiving...")
        desktop.progress_bar.start.assert_called_once_with(15)
        desktop.activity_log.record.assert_not_called()
        self.assertEqual(desktop.log_tree.rows, [])

        with patch("mailarchive.desktop.time.monotonic", return_value=75.0):
            desktop._update_run_elapsed()
        self.assertEqual(desktop.elapsed_var.get(), "01:05 elapsed")
        desktop._display_event(ServiceEvent(EventLevel.ERROR, "Download failed"))
        desktop._display_event(ServiceEvent(EventLevel.WARNING, "Another warning"))
        desktop._display_event(ServiceEvent(EventLevel.SUCCESS, "Other account finished"))
        self.assertEqual(desktop.progress_var.get(), progress.message)
        desktop.on_run_progress(RunProgress("Finished: 1 failed.", active=False))
        desktop._drain_ui_queue()

        self.assertFalse(desktop._archive_running)
        self.assertEqual(desktop.progress_var.get(), "Finished: 1 failed.")
        desktop.progress_bar.stop.assert_called_once_with()
        desktop.progress_bar.pack_forget.assert_called_once_with()
        desktop.root.after_cancel.assert_called_once()
        desktop.archive_button.configure.assert_called_with(state="normal", text="Archive now")
        desktop.tray.set_state.assert_called_with("error", "MailArchive - problem detected")

    def test_rejected_request_preserves_progress_without_starting_indicator(self) -> None:
        desktop = make_desktop()
        desktop.runner.run_now.return_value = False
        desktop.progress_var.set("Finished: 5 archived.")

        desktop.run_now()

        self.assertEqual(desktop.progress_var.get(), "Finished: 5 archived.")
        self.assertFalse(desktop._archive_running)
        desktop.progress_bar.start.assert_not_called()

    @unittest.skipUnless(app_module.os.name == "posix", "POSIX folder opener")
    @patch("mailarchive.desktop.subprocess.Popen")
    def test_open_archive_creates_and_opens_folder(self, popen) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            desktop = make_desktop(Settings(archive_root=str(archive)))
            with patch("mailarchive.desktop.sys.platform", "linux"):
                desktop.open_archive()
            self.assertTrue(archive.is_dir())
            popen.assert_called_once_with(["xdg-open", str(archive)])

    @unittest.skipUnless(app_module.os.name == "nt", "Windows folder opener")
    def test_open_archive_uses_windows_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            desktop = make_desktop(Settings(archive_root=str(archive)))
            with patch.object(app_module.os, "startfile", create=True) as startfile:
                desktop.open_archive()

            self.assertTrue(archive.is_dir())
            startfile.assert_called_once_with(archive)


class MainEntryPointTests(unittest.TestCase):
    def test_parse_arguments_recognizes_minimized(self) -> None:
        with patch.object(sys, "argv", ["mailarchive", "--minimized"]):
            self.assertTrue(app_module._parse_arguments().minimized)

    def test_smoke_test_exits_before_platform_or_gui_setup(self) -> None:
        with (
            patch.object(sys, "argv", ["mailarchive", "--smoke-test"]),
            patch("mailarchive.app.SingleInstance") as single_instance,
            patch("mailarchive.app.tk.Tk") as tk_root,
        ):
            app_module.main()

        single_instance.assert_not_called()
        tk_root.assert_not_called()

    def test_main_activates_existing_instance_without_creating_tk(self) -> None:
        instance = MagicMock(already_running=True)
        with (
            patch("mailarchive.app.SingleInstance", return_value=instance),
            patch("mailarchive.app.activate_existing_window") as activate,
            patch("mailarchive.app.tk.Tk") as tk_root,
            patch.object(sys, "argv", ["mailarchive"]),
        ):
            app_module.main()
        activate.assert_called_once_with()
        instance.close.assert_called_once_with()
        tk_root.assert_not_called()

    def test_version_exits_before_platform_or_gui_setup(self) -> None:
        output = StringIO()
        with (
            patch.object(sys, "argv", ["mailarchive", "--version"]),
            patch("mailarchive.app.SingleInstance") as instance,
            patch("mailarchive.app.tk.Tk") as root,
            redirect_stdout(output),
            self.assertRaises(SystemExit) as result,
        ):
            app_module.main()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"MailArchive {__version__}")
        instance.assert_not_called()
        root.assert_not_called()

    def test_main_stops_on_unreadable_config_without_loading_defaults(self) -> None:
        instance = MagicMock(already_running=False)
        root = MagicMock()
        store = MagicMock()
        store.load.side_effect = RuntimeError("incompatible config")
        with (
            patch.object(sys, "argv", ["mailarchive"]),
            patch("mailarchive.app.SingleInstance", return_value=instance),
            patch("mailarchive.app.tk.Tk", return_value=root),
            patch("mailarchive.app.ConfigStore", return_value=store),
            patch("mailarchive.app.DesktopApp") as application,
            patch("mailarchive.app.messagebox.showerror") as showerror,
        ):
            app_module.main()
        showerror.assert_called_once_with("MailArchive", "incompatible config")
        application.assert_not_called()
        store.save.assert_not_called()
        root.mainloop.assert_not_called()
        root.destroy.assert_called_once_with()
        instance.close.assert_called_once_with()

    def test_main_stops_when_database_upgrade_fails(self) -> None:
        instance = MagicMock(already_running=False)
        root = MagicMock()
        with (
            patch.object(sys, "argv", ["mailarchive"]),
            patch("mailarchive.app.SingleInstance", return_value=instance),
            patch("mailarchive.app.tk.Tk", return_value=root),
            patch("mailarchive.app.ConfigStore"),
            patch("mailarchive.app.KeyringCredentialStore"),
            patch("mailarchive.app.WindowsCredentialStore"),
            patch("mailarchive.app.DesktopApp", side_effect=DatabaseMigrationError("rolled back")),
            patch("mailarchive.app.messagebox.showerror") as showerror,
        ):
            app_module.main()
        showerror.assert_called_once_with("MailArchive could not start", "rolled back", parent=root)
        root.mainloop.assert_not_called()
        root.destroy.assert_called_once_with()
        instance.close.assert_called_once_with()

    def test_main_reports_credential_warning_and_minimizes(self) -> None:
        instance = MagicMock(already_running=False)
        root = MagicMock()
        store = MagicMock()
        store.load.return_value = Settings("/archive")
        credential_store = MagicMock()
        application = MagicMock()
        application.tray.safe_to_hide = True
        with (
            patch("mailarchive.app.SingleInstance", return_value=instance),
            patch("mailarchive.app.tk.Tk", return_value=root),
            patch("mailarchive.app.ConfigStore", return_value=store),
            patch("mailarchive.app.KeyringCredentialStore", side_effect=RuntimeError("no keyring")),
            patch("mailarchive.app.UnavailableCredentialStore", return_value=credential_store),
            patch("mailarchive.app.DesktopApp", return_value=application),
            patch("mailarchive.app.messagebox.showerror") as showerror,
            patch.object(app_module.os, "name", "posix"),
            patch.object(sys, "argv", ["mailarchive", "--minimized"]),
        ):
            app_module.main()

        showerror.assert_not_called()
        application.on_service_event.assert_called_once()
        warning = application.on_service_event.call_args.args[0]
        self.assertEqual(warning.level, EventLevel.ERROR)
        self.assertIn("no keyring", warning.message)
        root.withdraw.assert_called_once_with()
        root.mainloop.assert_called_once_with()
        instance.close.assert_called_once_with()
        application.offer_desktop_integration.assert_not_called()

    def test_main_offers_desktop_setup_only_for_interactive_start(self) -> None:
        for minimized, tray_available in ((False, True), (True, True), (True, False)):
            with self.subTest(minimized=minimized, tray_available=tray_available):
                instance = MagicMock(already_running=False)
                root = MagicMock()
                application = MagicMock()
                application.tray.safe_to_hide = tray_available
                arguments = ["mailarchive"] + (["--minimized"] if minimized else [])
                with (
                    patch.object(sys, "argv", arguments),
                    patch("mailarchive.app.SingleInstance", return_value=instance),
                    patch("mailarchive.app.tk.Tk", return_value=root),
                    patch("mailarchive.app.ConfigStore"),
                    patch("mailarchive.app.KeyringCredentialStore"),
                    patch("mailarchive.app.WindowsCredentialStore"),
                    patch("mailarchive.app.DesktopApp", return_value=application),
                ):
                    app_module.main()
                self.assertEqual(
                    application.offer_desktop_integration.call_count, int(not minimized)
                )


if __name__ == "__main__":
    unittest.main()
