from __future__ import annotations

import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import mailarchive.app as app_module
import mailarchive.presentation.tray as tray_module
from mailarchive.application.account_status import AccountStatusService
from mailarchive.application.events import EventLevel, RunProgress, ServiceEvent
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.domain.configuration import (
    Account,
    AuthMode,
    Condition,
    Mailbox,
    MailField,
    MailProvider,
    MatchMode,
    MatchOperator,
    Rule,
    RuleTarget,
    SaveMode,
    Settings,
)
from mailarchive.presentation.account_form import AccountSubmission, visible_account_fields
from mailarchive.presentation.desktop import DesktopApp
from mailarchive.presentation.dialogs import AccountDialog, DestinationBlock, RuleDialog
from mailarchive.presentation.tray import TrayController
from mailarchive.presentation.ui_text import (
    AUTH_LABELS,
    PROVIDER_LABELS,
    _auth_label_for,
    _condition_summary,
    _label_for,
)

TEST_ARCHIVE_ROOT = Path.cwd() / "archive"
TEST_DATABASE_PATH = Path.cwd() / "state.sqlite3"


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

    def pack(self, **options) -> None:
        self.removed = False

    def pack_forget(self) -> None:
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
    dialog.editor = None
    dialog._account_id = account.id if account else "draft-account"
    dialog._authorization_error = ""
    dialog._authorization_submission = None
    dialog.result = None
    dialog.authorization_frame = FakeWidget()
    dialog.authorization_label = FakeWidget()
    dialog.authorization_help = FakeWidget()
    dialog.authorize_button = FakeWidget()
    dialog.authorization_detail = FakeWidget()
    dialog.cancel_authorization_button = FakeWidget()
    dialog.retry_credentials_button = MagicMock()
    dialog.save_button = FakeWidget()
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
    dialog.read_service_account = MagicMock(return_value={"type": "service_account"})
    dialog.destroy = MagicMock()
    return dialog


def make_rule_dialog() -> RuleDialog:
    dialog = object.__new__(RuleDialog)
    dialog.name_var = FakeVariable("Invoices")
    dialog.field_var = FakeVariable("Subject")
    dialog.operator_var = FakeVariable("contains")
    dialog.value_var = FakeVariable("invoice")
    dialog.sender_value_vars = [FakeVariable("")]
    dialog._focus_sender = MagicMock()
    block = object.__new__(DestinationBlock)
    block.target_id = "target-id"
    block.path_var = FakeVariable(str(TEST_ARCHIVE_ROOT / "Finance"))
    block.preview_var = FakeVariable()
    block.save_var = FakeVariable("Email only (.eml)")
    block.attachments_in_destination_var = FakeVariable(False)
    block.attachments_in_destination_box = FakeWidget()
    block.winfo_toplevel = MagicMock(return_value=dialog)
    dialog.destinations = SimpleNamespace(blocks=[block], focus_path=MagicMock())
    dialog.enabled_var = FakeVariable(True)
    dialog.account_scope_var = FakeVariable("all")
    dialog.account_options = []
    dialog.account_list = MagicMock()
    dialog.account_list.curselection.return_value = ()
    dialog.rule = None
    dialog.save_rule = None
    dialog.result = None
    dialog.destroy = MagicMock()
    return dialog


def make_desktop(settings: Settings | None = None) -> DesktopApp:
    desktop = object.__new__(DesktopApp)
    desktop.settings = settings or Settings()
    desktop.application = MagicMock()
    desktop.application.settings = desktop.settings
    desktop.application.account_statuses = AccountStatusService()
    desktop.application.account_status.side_effect = lambda account_id: (
        desktop.application.account_statuses.resolve(
            next(a for a in desktop.settings.accounts if a.id == account_id), desktop.settings.rules
        )
    )
    desktop.application.database_path = TEST_DATABASE_PATH
    desktop.application.status.return_value = SimpleNamespace(pending_count=0, spool_bytes=0)
    desktop.application.monitoring_status.return_value = SimpleNamespace(status="active")
    desktop.application.automatic_monitoring_state.return_value = AutomaticMonitoringState.ACTIVE
    desktop.application.paused_scopes.return_value = ()
    desktop.application.close.return_value = True
    desktop.application.check_now.return_value = True
    desktop.application.authorization_in_progress.return_value = False
    desktop.application.activity_log_page.return_value = SimpleNamespace(
        events=(), total=0, offset=0
    )
    desktop.root = MagicMock()
    desktop.account_tree = FakeTree()
    desktop._account_status_revision = None
    desktop._account_status_retry_at = None
    desktop._archive_summary_refresh_at = 0.0
    desktop._account_selection_after_refresh = None
    desktop.account_notice_var = FakeVariable()
    desktop.rule_tree = FakeTree()
    desktop.account_summary = FakeVariable()
    desktop.rule_summary = FakeVariable()
    desktop.archive_summary = FakeVariable()
    desktop.progress_var = FakeVariable()
    desktop.elapsed_var = FakeVariable()
    desktop.progress_bar = MagicMock()
    desktop.check_button = MagicMock()
    desktop.automatic_button = MagicMock()
    desktop.automatic_status_var = FakeVariable()
    desktop._monitoring_state = None
    desktop._archive_running = False
    desktop._check_id = None
    desktop._check_progress = None
    desktop._other_progress = None
    desktop._seen_progress = {}
    desktop._stop_requested = False
    desktop._run_event_level = EventLevel.INFO
    desktop._run_started_at = 0.0
    desktop._progress_timer = None
    desktop.poll_var = FakeVariable(str(desktop.settings.default_poll_minutes))
    desktop.database_var = FakeVariable(str(TEST_DATABASE_PATH))
    desktop.startup_var = FakeVariable(desktop.settings.start_at_login)
    desktop.minimize_var = FakeVariable(desktop.settings.minimize_to_tray)
    desktop.warning_var = FakeVariable(desktop.settings.warn_on_error)
    desktop.timezone_var = FakeVariable(desktop.settings.archive_timezone)
    desktop.tray = MagicMock()
    desktop.log_tree = FakeTree()
    desktop.log_filter_var = FakeVariable("Last 50")
    desktop.log_summary_var = FakeVariable()
    desktop.log_previous_button = FakeWidget()
    desktop.log_next_button = FakeWidget()
    desktop._log_offset = 0
    desktop.ui_queue = queue.Queue()
    desktop._closing = False
    desktop._saving_settings = False
    desktop._profile_switch_update = None
    desktop.profile_switch_status_var = FakeVariable()
    desktop.notebook = MagicMock()
    desktop.dashboard_tab = MagicMock()
    desktop.accounts_tab = MagicMock()
    desktop.rules_tab = MagicMock()
    desktop.log_tab = MagicMock()
    desktop.settings_pages = MagicMock()
    desktop.settings_pages.index.return_value = 3
    desktop.general_settings_scroll = MagicMock()
    desktop.general_settings_scroll.content.winfo_children.return_value = ()
    desktop.advanced_settings_scroll = MagicMock()
    desktop.advanced_settings_scroll.content.winfo_children.return_value = ()
    desktop._setting_entry_fields = {}
    desktop._checking_for_updates = False
    desktop.update_button = MagicMock()
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
        self.assertEqual(_condition_summary(Rule("All")), "All emails")
        self.assertEqual(
            _condition_summary(
                Rule(
                    "With files",
                    conditions=[Condition(MailField.HAS_ATTACHMENT, value="yes")],
                )
            ),
            "Has attachments: Yes",
        )
        self.assertEqual(
            _condition_summary(
                Rule(
                    "Without files",
                    conditions=[Condition(MailField.HAS_ATTACHMENT, value="false")],
                )
            ),
            "Has attachments: No",
        )
        self.assertEqual(
            _condition_summary(
                Rule(
                    "Invoices",
                    conditions=[Condition(MailField.SUBJECT, MatchOperator.STARTS_WITH, "Invoice")],
                )
            ),
            'Subject starts with "Invoice"',
        )
        self.assertEqual(
            _condition_summary(
                Rule(
                    "Senders",
                    conditions=[
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
        self.controller._monitoring_paused = False
        self.controller._state = "ok"
        self.controller._title = "MailArchive - ready"
        self.controller.monitoring_callback = MagicMock()

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
        linux_tray.available = True
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
        linux_tray.available = False
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
            def __init__(self, label, callback, default=False, visible=True) -> None:
                self.label = label
                self.callback = callback
                self.default = default
                self.visible = visible

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

            def update_menu(self) -> None:
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
            toggle = MagicMock()
            post_ui = MagicMock()
            controller = TrayController(post_ui, MagicMock(), MagicMock(), MagicMock(), toggle)

        self.assertTrue(controller.available)
        self.assertTrue(controller.safe_to_hide)
        self.assertEqual(controller.icon.title, "MailArchive - ready")
        self.assertEqual(controller.icon.menu.items[0].label, "Open MailArchive")
        action = controller.icon.menu.items[2]
        self.assertEqual(action.label(None), "Pause automatic checks")
        controller.set_monitoring_paused(True)
        self.assertEqual(action.label(None), "Resume automatic checks")
        self.assertEqual(controller.icon.title, "MailArchive - automatic checks paused")
        action.callback()
        post_ui.assert_called_once_with(toggle)

    def test_paused_tray_preserves_errors_and_restores_idle_state_on_resume(self):
        controller = self.controller
        with patch.object(TrayController, "_image") as image:
            controller.set_monitoring_paused(True)
            image.assert_called_with("paused")
            controller.set_state("error", "MailArchive - problem detected")
            image.assert_called_with("error")
            self.assertIn("automatic checks paused", controller.icon.title)
            controller.set_monitoring_paused(False)
            self.assertEqual(controller.icon.title, "MailArchive - problem detected")
            controller.set_state("ok", "MailArchive - ready")
            image.assert_called_with("ok")

    def test_generated_tray_image_has_expected_size_and_state_color(self) -> None:
        image = TrayController._image("error")
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((6, 25)), (197, 48, 48, 255))


class AccountDialogTests(unittest.TestCase):
    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
    def test_save_failure_keeps_editor_and_inputs(self, showerror):
        dialog = make_account_dialog(
            provider="Gmail (Google API)", auth="Google OAuth - user sign-in"
        )
        dialog.editor = MagicMock()
        dialog.editor.save.side_effect = OSError("Disk full")
        dialog._save()
        self.assertIsNone(dialog.result)
        dialog.destroy.assert_not_called()
        self.assertEqual(showerror.call_args.args[:2], ("Email account not saved", "Disk full"))

    def test_authorize_keeps_editor_open_and_does_not_save(self):
        dialog = make_account_dialog(
            provider="Gmail (Google API)", auth="Google OAuth - user sign-in"
        )
        dialog.editor = MagicMock()
        dialog._update_authorization = MagicMock()
        dialog._authorize()
        dialog.editor.authorize.assert_called_once()
        dialog.editor.save.assert_not_called()
        dialog.destroy.assert_not_called()
        self.assertIsNone(dialog.result)
        dialog._save()
        dialog.editor.save.assert_called_once_with(dialog.result)
        dialog.destroy.assert_called_once()

    def test_dialog_size_covers_all_provider_layouts_and_restores_selection(self) -> None:
        dialog = make_account_dialog(
            provider="Gmail (Google API)", auth="Google OAuth - user sign-in"
        )
        dialog._update_fields = MagicMock()
        dialog.update_idletasks = MagicMock()
        dialog.form_scroll = MagicMock()
        dialog.form_scroll.content.winfo_reqwidth.side_effect = [500, 510, 620, 600, 590, 610]
        dialog.form_scroll.content.winfo_reqheight.side_effect = [420, 450, 430, 480, 440, 470]
        dialog.field_labels = {}
        dialog.buttons = MagicMock()
        dialog.buttons.winfo_reqheight.return_value = 28
        dialog.winfo_screenwidth = MagicMock(return_value=1024)
        dialog.winfo_screenheight = MagicMock(return_value=720)
        dialog.minsize = MagicMock()
        dialog.geometry = MagicMock()

        dialog._fix_size_for_layouts()

        self.assertEqual(dialog.variables["provider"].get(), "Gmail (Google API)")
        self.assertEqual(dialog.variables["auth"].get(), "Google OAuth - user sign-in")
        dialog.minsize.assert_called_once_with(678, 560)
        dialog.geometry.assert_called_once_with("678x560")
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

                visible = visible_account_fields(
                    PROVIDER_LABELS[provider],
                    AUTH_LABELS[auth],
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

    @patch("mailarchive.presentation.dialogs.filedialog.askopenfilename")
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

    def test_save_google_application_account_uses_parsed_key(self) -> None:
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
        dialog.read_service_account.assert_called_once_with("service-account.json")

    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
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

    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
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

    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
    def test_save_rule_requires_account_selection_when_scope_is_restricted(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.account_scope_var.set("selected")
        dialog._save()
        self.assertIsNone(dialog.result)
        self.assertIn("Select at least one email account", showerror.call_args.args[1])
        dialog.destroy.assert_not_called()

    def test_dialog_limits_height_and_allocates_remaining_space_to_destinations(self) -> None:
        dialog = make_rule_dialog()
        dialog._fixed_width = 560
        dialog._base_height = 360
        dialog.update_idletasks = MagicMock()
        dialog.winfo_screenheight = MagicMock(return_value=720)
        dialog.form_frame = MagicMock()
        dialog.form_frame.winfo_reqheight.return_value = 500
        dialog.dialog_frame = MagicMock()
        dialog.dialog_frame.winfo_reqheight.return_value = 570
        dialog.form_scroll = MagicMock()
        dialog.form_scroll.winfo_reqheight.return_value = 500
        dialog.destinations.canvas = MagicMock()
        dialog.destinations.canvas.winfo_reqheight.return_value = 200
        dialog.destinations.viewport_height = 180
        dialog.winfo_reqheight = MagicMock(return_value=1900)
        dialog.winfo_ismapped = MagicMock(return_value=False)
        dialog.geometry = MagicMock()

        dialog._fit_content_height()

        dialog.destinations.canvas.configure.assert_called_once_with(height=180)
        dialog.form_scroll.canvas.configure.assert_called_once_with(height=500)
        dialog.geometry.assert_called_once_with("560x640")

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

    @patch("mailarchive.presentation.dialogs.tk.StringVar")
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

    @patch("mailarchive.presentation.dialogs.choose_destination_folder")
    def test_choose_folder_accepts_any_full_destination(self, choose_folder) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            inside = archive / "Finance"
            inside.mkdir(parents=True)
            outside = Path(temporary) / "outside"
            outside.mkdir()
            dialog = make_rule_dialog()

            choose_folder.return_value = str(inside)
            dialog.destinations.blocks[0]._choose_folder()
            self.assertEqual(dialog.destinations.blocks[0].path_var.get(), str(inside))

            choose_folder.return_value = str(archive)
            dialog.destinations.blocks[0]._choose_folder()
            self.assertEqual(dialog.destinations.blocks[0].path_var.get(), str(archive))

            choose_folder.return_value = None
            dialog.destinations.blocks[0]._choose_folder()
            self.assertEqual(dialog.destinations.blocks[0].path_var.get(), str(archive))

            choose_folder.return_value = str(outside)
            dialog.destinations.blocks[0]._choose_folder()
            self.assertEqual(dialog.destinations.blocks[0].path_var.get(), str(outside))

    def test_rule_destination_preview_and_save_use_full_template(self) -> None:
        dialog = make_rule_dialog()
        destination = str(TEST_ARCHIVE_ROOT.parent / "another" / "archive" / "{year}" / "{month}")
        dialog.destinations.blocks[0].path_var.set(destination)
        dialog.destinations.blocks[0].attachments_in_destination_var.set(True)
        dialog.destinations.blocks[0]._update_preview()
        self.assertEqual(
            dialog.destinations.blocks[0].preview_var.get(),
            str(TEST_ARCHIVE_ROOT.parent / "another" / "archive" / "YYYY" / "MM"),
        )
        dialog._save()
        self.assertEqual(dialog.result.targets[0].path, destination)
        self.assertTrue(dialog.result.targets[0].attachments_in_destination)

    def test_attachment_option_follows_save_mode_and_preserves_selection(self) -> None:
        dialog = make_rule_dialog()
        dialog.destinations.blocks[0].attachments_in_destination_var.set(True)
        for mode, expected in (
            ("Email only (.eml)", "disabled"),
            ("Email and attachments", "normal"),
            ("Attachments only", "normal"),
        ):
            with self.subTest(mode=mode):
                dialog.destinations.blocks[0].save_var.set(mode)
                dialog.destinations.blocks[0]._update_attachment_option()
                self.assertEqual(
                    dialog.destinations.blocks[0].attachments_in_destination_box.options["state"],
                    expected,
                )
                self.assertTrue(dialog.destinations.blocks[0].attachments_in_destination_var.get())
                dialog._save()
                self.assertTrue(dialog.result.targets[0].attachments_in_destination)

    def test_invalid_destination_is_visible_in_preview_and_blocks_save(self) -> None:
        dialog = make_rule_dialog()
        dialog.destinations.blocks[0].path_var.set("../outside")
        dialog.destinations.blocks[0]._update_preview()
        self.assertIn("full destination path", dialog.destinations.blocks[0].preview_var.get())
        with patch("mailarchive.presentation.dialogs.messagebox.showerror") as showerror:
            dialog._save()
        showerror.assert_called_once()
        dialog.destroy.assert_not_called()

    @patch("mailarchive.presentation.rule_form.destination_path")
    def test_save_rule_preserves_id_and_builds_condition(self, destination) -> None:
        dialog = make_rule_dialog()
        dialog.rule = Rule("Old", id="rule-1", targets=[RuleTarget("/old")])

        dialog._save()

        self.assertEqual(dialog.result.id, "rule-1")
        self.assertEqual(dialog.result.targets[0].save_mode, SaveMode.EMAIL_ONLY)
        self.assertEqual(dialog.result.conditions[0].field, MailField.SUBJECT)
        self.assertEqual(dialog.result.conditions[0].value, "invoice")
        destination.assert_called_once_with(str(TEST_ARCHIVE_ROOT / "Finance"))
        dialog.destroy.assert_called_once_with()

    @patch("mailarchive.presentation.rule_form.destination_path")
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

    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
    def test_save_rule_rejects_empty_sender_field(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.field_var.set("Sender")
        dialog.sender_value_vars = [FakeVariable("first@example.com"), FakeVariable(" ")]

        dialog._save()

        self.assertIsNone(dialog.result)
        self.assertIn("each sender field", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()

    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
    def test_save_rule_reports_invalid_attachment_value(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.field_var.set("Has attachments")
        dialog.value_var.set("sometimes")

        dialog._save()

        self.assertIsNone(dialog.result)
        self.assertIn("yes or no", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()

    @patch("mailarchive.presentation.dialogs.messagebox.showerror")
    def test_save_rule_requires_name_and_text_comparison(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.name_var.set(" ")
        dialog._save()
        self.assertIn("name", showerror.call_args.args[1].lower())

        dialog.name_var.set("Invoices")
        dialog.value_var.set(" ")
        dialog._save()
        self.assertIn("comparison value", showerror.call_args.args[1].lower())


class DesktopControllerTests(unittest.TestCase):
    def test_periodic_work_summary_bounds_reads_and_recovers_without_rebuilding_rows(self):
        desktop = make_desktop(Settings(rules=[Rule("Keep selected")]))
        desktop.refresh_all()
        rule = desktop.settings.rules[0]
        desktop.rule_tree.selection_set(rule.id)
        desktop.poll_var.set("unsaved input")
        desktop.application.status.reset_mock()
        with patch("mailarchive.presentation.desktop.time.monotonic", return_value=100.0) as clock:
            desktop._archive_summary_refresh_at = 100.0
            desktop.application.status.side_effect = OSError("profile unavailable")
            desktop._drain_ui_queue()
            self.assertEqual(desktop.archive_summary.get(), "Work queue unavailable")
            for _ in range(5):
                desktop._drain_ui_queue()
            self.assertEqual(desktop.application.status.call_count, 1)
            desktop.application.status.side_effect = None
            desktop.application.status.return_value = SimpleNamespace(
                pending_count=3, spool_bytes=2 * 1024**2
            )
            clock.return_value = 101.0
            desktop._drain_ui_queue()
            self.assertEqual(desktop.archive_summary.get(), "3 pending / 2.0 MiB")
        self.assertEqual(desktop.rule_tree.selection(), (rule.id,))
        self.assertEqual(desktop.poll_var.get(), "unsaved input")

    def test_add_uses_editor_and_does_not_start_authorization_after_closing(self):
        account = Account(
            "Microsoft",
            username="me@example.com",
            provider=MailProvider.MICROSOFT_GRAPH,
            auth_mode=AuthMode.OAUTH_USER,
        )
        desktop = make_desktop(Settings(accounts=[account]))
        submission = AccountSubmission(account, {}, False)
        with patch(
            "mailarchive.presentation.desktop.AccountDialog",
            return_value=SimpleNamespace(result=submission),
        ) as dialog:
            desktop.add_account()
        self.assertIs(
            dialog.call_args.kwargs["editor"], desktop.application.account_editor.return_value
        )
        desktop.application.authorize_account.assert_not_called()
        self.assertEqual(desktop.account_tree.selection(), (account.id,))

    def test_refresh_uses_facade_status_and_canonical_target_modes(self) -> None:
        rule = Rule("Invoices", targets=[RuleTarget(str(TEST_ARCHIVE_ROOT), SaveMode.EMAIL_ONLY)])
        desktop = make_desktop(Settings(rules=[rule]))

        desktop.refresh_all()

        self.assertEqual(desktop.archive_summary.get(), "0 pending / 0.0 MiB")
        self.assertIn(str(TEST_ARCHIVE_ROOT), desktop.rule_tree.rows[0]["values"][4])
        self.assertEqual(desktop.rule_tree.rows[0]["values"][5], "Email only (.eml)")
        desktop.application.status.assert_called_once_with()

    def test_saved_editor_refreshes_selection_and_remove_uses_application_command(self) -> None:
        account = Account("Work", host="imap.example.com", username="mail@example.com")
        desktop = make_desktop(Settings(accounts=[account]))
        submission = AccountSubmission(account, {}, False)
        desktop._account_editor_closed(SimpleNamespace(result=submission))
        self.assertEqual(desktop.account_tree.selection(), (account.id,))

        desktop.account_tree.selection_set(account.id)
        with patch("mailarchive.presentation.desktop.messagebox.askyesno", return_value=True):
            desktop.remove_account()
        desktop.application.delete_account.assert_called_once_with(account.id)

    def test_busy_authorization_prevents_account_removal(self) -> None:
        account = Account("Work", host="imap.example.com", username="mail@example.com")
        desktop = make_desktop(Settings(accounts=[account]))
        desktop.account_tree.selection_set(account.id)
        desktop.application.authorization_in_progress.return_value = True
        with patch("mailarchive.presentation.desktop.messagebox.showinfo") as info:
            desktop.remove_account()
        info.assert_called_once()
        desktop.application.delete_account.assert_not_called()

    def test_account_removal_failure_is_visible(self) -> None:
        account = Account("Work", host="imap.example.com", username="mail@example.com")
        desktop = make_desktop(Settings(accounts=[account]))
        desktop.account_tree.selection_set(account.id)
        desktop.application.delete_account.side_effect = OSError("profile read only")
        with (
            patch("mailarchive.presentation.desktop.messagebox.askyesno", return_value=True),
            patch("mailarchive.presentation.desktop.messagebox.showerror") as showerror,
        ):
            desktop.remove_account()
        self.assertIn("profile read only", showerror.call_args.args[1])

    def test_rule_save_failure_preserves_visible_settings(self) -> None:
        desktop = make_desktop(Settings(rules=[Rule("Original")]))
        desktop.application.save_rules.side_effect = OSError("disk full")
        with patch("mailarchive.presentation.desktop.messagebox.showerror") as showerror:
            result = desktop._commit_rules([Rule("Replacement")])
        self.assertFalse(result)
        self.assertEqual(desktop.settings.rules[0].name, "Original")
        self.assertIn("disk full", showerror.call_args.args[1])

    def test_settings_save_uses_facade_and_reverts_field_on_failure(self) -> None:
        desktop = make_desktop(Settings(default_poll_minutes=5))
        desktop.poll_var.set("10")
        desktop.application.save_settings.side_effect = OSError("disk full")
        with patch("mailarchive.presentation.desktop.messagebox.showerror") as showerror:
            desktop.save_settings("default_poll_minutes")
        self.assertEqual(desktop.poll_var.get(), "5")
        self.assertIn("disk full", showerror.call_args.args[1])

    def test_profile_switch_rejection_keeps_selected_database(self) -> None:
        desktop = make_desktop()
        desktop.database_var.set(str(TEST_DATABASE_PATH.with_name("different.sqlite3")))
        desktop.application.request_profile_switch.side_effect = RuntimeError(
            "mail processing is stopping"
        )
        with patch("mailarchive.presentation.desktop.messagebox.showerror") as showerror:
            desktop.save_settings("state_database_path")
        self.assertEqual(desktop.database_var.get(), str(TEST_DATABASE_PATH))
        self.assertIn("mail processing is stopping", showerror.call_args.args[1])
        self.assertIsNone(desktop._profile_switch_update)
        self.assertFalse(desktop._saving_settings)
        desktop.application.request_profile_switch.assert_called_once()
        desktop.application.switch_profile.assert_not_called()

    def test_past_mail_defaults_to_system_zone_and_submits_selected_zone(self) -> None:
        rule = Rule("Invoices")
        desktop = make_desktop(Settings(rules=[rule]))
        desktop.rule_tree.selection_set(rule.id)
        with (
            patch(
                "mailarchive.presentation.timezone_choices.get_localzone_name",
                return_value="Europe/Berlin",
            ),
            patch("mailarchive.presentation.desktop.RangeDialog") as dialog,
        ):
            dialog.return_value.result = SimpleNamespace(
                start=None, end=None, timezone_name="America/New_York"
            )
            desktop.show_archive_activity = MagicMock()
            desktop.run_rule_history_dialog()
        dialog.assert_called_once_with(desktop.root, rule, "Europe/Berlin")
        desktop.application.apply_rule_to_past_mail.assert_called_once_with(
            rule.id, None, None, "America/New_York"
        )
        self.assertEqual(desktop.settings.archive_timezone, "UTC")
        desktop.show_archive_activity.assert_called_once_with()

    def test_update_check_uses_facade_callback_and_reports_error(self) -> None:
        desktop = make_desktop()
        desktop.check_for_updates()
        callback = desktop.application.check_for_updates.call_args.args[0]
        desktop.check_for_updates()
        desktop.application.check_for_updates.assert_called_once()
        with patch("mailarchive.presentation.desktop.messagebox.showerror") as showerror:
            callback(None, "network unavailable")
        self.assertIn("network unavailable", showerror.call_args.args[1])
        self.assertFalse(desktop._checking_for_updates)

    def test_event_and_progress_are_queued_for_ui_and_tray(self) -> None:
        desktop = make_desktop()
        desktop.on_service_event(ServiceEvent(EventLevel.ERROR, "Archive failed"))
        desktop.on_run_progress(RunProgress("Checking", active=True))
        self.assertEqual(desktop.ui_queue.qsize(), 2)
        desktop._drain_ui_queue()
        self.assertEqual(desktop.progress_var.get(), "Checking")
        desktop.tray.notify.assert_called_once_with("Archive failed")
        desktop.application.dispatch_callbacks.assert_called_once_with()

    def test_close_waits_for_application_shutdown(self) -> None:
        desktop = make_desktop()
        desktop.application.close.return_value = False
        desktop.quit()
        desktop.root.destroy.assert_not_called()
        self.assertTrue(desktop._closing)
        self.assertIn("waiting for current work", desktop.progress_var.get())
        desktop.root.after.assert_called_once_with(100, desktop._finish_close)
        desktop.hide_to_tray()
        desktop.root.withdraw.assert_not_called()
        desktop.application.close.return_value = True
        desktop.root.after.call_args.args[1]()
        desktop.root.destroy.assert_called_once_with()
        desktop.tray.stop.assert_called_once_with()
        self.assertEqual(desktop.application.close.call_count, 2)
        desktop.application.close.assert_called_with(timeout=0)


class MainEntryPointTests(unittest.TestCase):
    def test_version_and_smoke_test_exit_without_opening_profile(self) -> None:
        with patch.object(sys, "argv", ["mailarchive", "--version"]), self.assertRaises(SystemExit):
            app_module.main()
        with (
            patch.object(sys, "argv", ["mailarchive", "--smoke-test"]),
            patch("mailarchive.app.create_root") as create_root,
            patch("mailarchive.app.create_application") as create_application,
        ):
            app_module.main()
        create_root.return_value.destroy.assert_called_once_with()
        create_application.assert_not_called()

    def test_main_composes_binds_and_starts_application(self) -> None:
        for platform in ("posix", "nt"):
            with (
                self.subTest(platform=platform),
                patch.object(sys, "argv", ["mailarchive"]),
                patch.object(app_module, "os", SimpleNamespace(name=platform)),
                patch("mailarchive.app.SingleInstance") as instance,
                patch("mailarchive.app.create_root") as create_root,
                patch("mailarchive.app.ConfigStore") as store,
                patch("mailarchive.app.KeyringCredentialStore") as keyring_credentials,
                patch("mailarchive.app.WindowsCredentialStore") as windows_credentials,
                patch("mailarchive.app.create_application") as create_application,
                patch("mailarchive.app.AppImageIntegration.for_current_process") as integration,
                patch("mailarchive.app.DesktopApp") as desktop_app,
            ):
                instance.return_value.already_running = False
                app_module.main()
                credentials, unused_credentials = (
                    (windows_credentials, keyring_credentials)
                    if platform == "nt"
                    else (keyring_credentials, windows_credentials)
                )
                credentials.assert_called_once_with()
                unused_credentials.assert_not_called()
                create_application.assert_called_once_with(
                    store.return_value, credentials.return_value
                )
                desktop_app.assert_called_once_with(
                    create_root.return_value,
                    create_application.return_value,
                    integration.return_value,
                )
                create_application.return_value.set_observers.assert_called_once_with(
                    desktop_app.return_value.on_service_event,
                    desktop_app.return_value.on_run_progress,
                )
                create_application.return_value.start.assert_called_once_with()
                instance.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
