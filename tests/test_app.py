from __future__ import annotations

import queue
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import mailarchive.app as app_module
from mailarchive.app import (
    AccountDialog,
    DesktopApp,
    RuleDialog,
    TrayController,
    _auth_label_for,
    _condition_summary,
    _label_for,
)
from mailarchive.models import (
    Account,
    AuthMode,
    Condition,
    MailField,
    MailProvider,
    MatchOperator,
    Rule,
    SaveMode,
    Settings,
)
from mailarchive.service import EventLevel, ServiceEvent


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
                row
                for index, row in enumerate(self.rows)
                if row.get("iid", index) not in item_set
            ]

    def selection(self):
        return self.selected

    def selection_set(self, item) -> None:
        self.selected = (item,)


class ImmediateThread:
    created: list["ImmediateThread"] = []

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
    }
    dialog.destroy = MagicMock()
    return dialog


def make_rule_dialog() -> RuleDialog:
    dialog = object.__new__(RuleDialog)
    dialog.name_var = FakeVariable("Invoices")
    dialog.field_var = FakeVariable("Subject")
    dialog.operator_var = FakeVariable("contains")
    dialog.value_var = FakeVariable("invoice")
    dialog.destination_var = FakeVariable("Finance")
    dialog.save_var = FakeVariable("Email only (.eml)")
    dialog.enabled_var = FakeVariable(True)
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
    desktop.status_var = FakeVariable()
    desktop.archive_var = FakeVariable(desktop.settings.archive_root)
    desktop.poll_var = FakeVariable(str(desktop.settings.default_poll_minutes))
    desktop.database_var = FakeVariable("/state.sqlite3")
    desktop.startup_var = FakeVariable(desktop.settings.start_at_login)
    desktop.minimize_var = FakeVariable(desktop.settings.minimize_to_tray)
    desktop.warning_var = FakeVariable(desktop.settings.warn_on_error)
    desktop.state = SimpleNamespace(database_path=Path("/state.sqlite3"))
    desktop.service = MagicMock()
    desktop.runner = MagicMock()
    desktop.tray = MagicMock()
    desktop.log_tree = FakeTree()
    desktop.ui_queue = queue.Queue()
    desktop._closing = False
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


class TrayControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = object.__new__(TrayController)
        self.controller.post_ui = MagicMock()
        self.controller.show_callback = MagicMock()
        self.controller.run_callback = MagicMock()
        self.controller.quit_callback = MagicMock()
        self.controller.available = True
        self.controller.icon = MagicMock()

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

    def test_constructor_starts_supported_linux_backend(self) -> None:
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

            def run(self) -> None:
                pass

        FakeIcon.__module__ = "pystray._xorg"
        fake_pystray = SimpleNamespace(
            MenuItem=FakeMenuItem,
            Menu=FakeMenu,
            Icon=FakeIcon,
        )
        with (
            patch.dict(sys.modules, {"pystray": fake_pystray}),
            patch.object(TrayController, "_image", return_value="image"),
            patch("mailarchive.app.tray_backend_is_available", return_value=True),
            patch("mailarchive.app.threading.Thread") as thread,
            patch.object(app_module.os, "name", "posix"),
        ):
            controller = TrayController(
                MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )

        self.assertTrue(controller.available)
        self.assertTrue(controller.safe_to_hide)
        self.assertEqual(controller.icon.title, "MailArchive - ready")
        self.assertEqual(controller.icon.menu.items[0].label, "Open MailArchive")
        thread.assert_called_once_with(
            target=controller.icon.run,
            name="MailArchive-Tray",
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_constructor_disables_unsupported_backend_and_contains_failures(self) -> None:
        fake_pystray = SimpleNamespace(
            MenuItem=lambda *args, **kwargs: object(),
            Menu=SimpleNamespace(SEPARATOR=object()),
            Icon=MagicMock(),
        )
        fake_pystray.Menu = MagicMock()
        fake_pystray.Menu.SEPARATOR = object()
        with (
            patch.dict(sys.modules, {"pystray": fake_pystray}),
            patch.object(TrayController, "_image", return_value="image"),
            patch("mailarchive.app.tray_backend_is_available", return_value=False),
        ):
            controller = TrayController(
                MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )
        self.assertFalse(controller.available)
        self.assertFalse(controller.safe_to_hide)
        self.assertIsNone(controller.icon)

        with patch.dict(sys.modules, {"pystray": None}):
            controller = TrayController(
                MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )
        self.assertFalse(controller.available)
        self.assertFalse(controller.safe_to_hide)

    def test_generated_tray_image_has_expected_size_and_state_color(self) -> None:
        image = TrayController._image("error")
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((6, 25)), (197, 48, 48, 255))


class AccountDialogTests(unittest.TestCase):
    def test_provider_change_sets_compatible_auth_and_folder(self) -> None:
        dialog = make_account_dialog()
        dialog._update_fields = MagicMock()

        dialog.variables["folder"].set("")
        dialog.variables["provider"].set("Gmail (Google API)")
        dialog._provider_changed()
        self.assertEqual(dialog.variables["auth"].get(), "Google OAuth - user sign-in")
        self.assertEqual(dialog.variables["folder"].get(), "INBOX")

        dialog.variables["provider"].set(
            "Outlook / Microsoft 365 (Microsoft Graph)"
        )
        dialog._provider_changed()
        self.assertEqual(
            dialog.variables["auth"].get(),
            "Microsoft OAuth - delegated user access",
        )
        self.assertEqual(dialog.variables["folder"].get(), "inbox")

        dialog.variables["folder"].set("")
        dialog.variables["provider"].set("Generic IMAP")
        dialog._provider_changed()
        self.assertEqual(dialog.variables["auth"].get(), "Password")
        self.assertEqual(dialog.variables["folder"].get(), "INBOX")
        self.assertEqual(dialog._update_fields.call_count, 3)

    def test_layout_fields_hides_irrelevant_widgets_and_ssl(self) -> None:
        dialog = make_account_dialog()
        dialog.field_order = ["label", "host"]
        dialog.field_labels = {key: FakeWidget() for key in dialog.field_order}
        dialog.field_containers = {key: FakeWidget() for key in dialog.field_order}
        dialog.ssl_check = FakeWidget()
        dialog.enabled_check = FakeWidget()
        dialog.help_label = FakeWidget()
        dialog.buttons = FakeWidget()

        dialog._layout_fields(frozenset({"label"}), show_ssl=False)

        self.assertFalse(dialog.field_labels["label"].removed)
        self.assertTrue(dialog.field_labels["host"].removed)
        self.assertTrue(dialog.field_containers["host"].removed)
        self.assertTrue(dialog.ssl_check.removed)
        self.assertEqual(dialog.enabled_check.grid_calls[-1]["row"], 1)

    def test_update_fields_covers_every_provider_auth_combination(self) -> None:
        cases = [
            ("Generic IMAP", "Password", True, "Password / app password"),
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
                    key: FakeWidget()
                    for key in ("secret", "client_id", "tenant_id")
                }
                dialog.service_account_button = FakeWidget()
                dialog.help_label = FakeWidget()
                dialog._layout_fields = MagicMock()

                dialog._update_fields()

                visible = app_module.visible_account_fields(
                    app_module.PROVIDER_LABELS[provider],
                    app_module.AUTH_LABELS[auth],
                )
                dialog._layout_fields.assert_called_once_with(
                    visible, show_ssl=show_ssl
                )
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
        self.assertEqual(dialog.widgets["auth"].options["values"], ["Password"])

    @patch("mailarchive.app.filedialog.askopenfilename")
    def test_choose_service_account_file_only_updates_on_selection(self, ask) -> None:
        dialog = make_account_dialog()
        ask.return_value = "/keys/workspace.json"
        dialog._choose_google_service_account_file()
        self.assertEqual(
            dialog.variables["service_account_file"].get(), "/keys/workspace.json"
        )
        ask.return_value = ""
        dialog._choose_google_service_account_file()
        self.assertEqual(
            dialog.variables["service_account_file"].get(), "/keys/workspace.json"
        )

    def test_save_imap_account_and_secret(self) -> None:
        dialog = make_account_dialog()

        dialog._save()

        account, credentials = dialog.result
        self.assertEqual(account.provider, MailProvider.GENERIC_IMAP)
        self.assertEqual(account.host, "imap.example.com")
        self.assertEqual(credentials, {"password": "secret"})
        dialog.destroy.assert_called_once_with()

    @patch("mailarchive.app.parse_google_service_account_file")
    def test_save_google_application_account_uses_parsed_key(self, parse_key) -> None:
        parse_key.return_value = {"type": "service_account"}
        dialog = make_account_dialog(
            provider="Gmail (Google API)",
            auth="Google Workspace - domain-wide delegation",
        )
        dialog.variables["secret"].set("")
        dialog.variables["folder"].set("")

        dialog._save()

        account, credentials = dialog.result
        self.assertEqual(account.folder, "INBOX")
        self.assertEqual(account.client_id, "")
        self.assertEqual(
            credentials, {"google_service_account": {"type": "service_account"}}
        )
        parse_key.assert_called_once_with("service-account.json")

    @patch("mailarchive.app.messagebox.showerror")
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
            host="imap.old.example",
            username="mail@example.com",
        )
        dialog = make_account_dialog(account=existing)
        dialog.variables["secret"].set("")

        dialog._save()

        account, credentials = dialog.result
        self.assertEqual(account.id, "account-1")
        self.assertEqual(credentials, {})

    def test_save_google_user_secret_uses_oauth_specific_key(self) -> None:
        dialog = make_account_dialog(
            provider="Gmail (Google API)",
            auth="Google OAuth - user sign-in",
        )

        dialog._save()

        account, credentials = dialog.result
        self.assertEqual(account.client_id, "client-id")
        self.assertEqual(credentials, {"oauth_client_secret": "secret"})

    @patch("mailarchive.app.messagebox.showerror")
    def test_new_google_application_requires_service_account_file(
        self, showerror
    ) -> None:
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
    def test_update_fields_handles_all_attachments_and_text(self) -> None:
        dialog = make_rule_dialog()
        dialog.operator_box = FakeWidget()
        dialog.value_entry = FakeWidget()
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

    @patch("mailarchive.app.filedialog.askdirectory")
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
            self.assertEqual(dialog.destination_var.get(), "Inbox")

            askdirectory.return_value = ""
            dialog._choose_folder()
            self.assertEqual(dialog.destination_var.get(), "Inbox")

            askdirectory.return_value = str(outside)
            with patch("mailarchive.app.messagebox.showerror") as showerror:
                dialog._choose_folder()
            showerror.assert_called_once()
            self.assertEqual(dialog.destination_var.get(), "Inbox")

    @patch("mailarchive.app.destination_path")
    def test_save_rule_preserves_id_and_builds_condition(self, destination) -> None:
        dialog = make_rule_dialog()
        dialog.rule = Rule("Old", "Old", id="rule-1")

        dialog._save()

        self.assertEqual(dialog.result.id, "rule-1")
        self.assertEqual(dialog.result.save_mode, SaveMode.EMAIL_ONLY)
        self.assertEqual(dialog.result.conditions[0].field, MailField.SUBJECT)
        self.assertEqual(dialog.result.conditions[0].value, "invoice")
        destination.assert_called_once_with(Path("/archive"), "Finance")
        dialog.destroy.assert_called_once_with()

    @patch("mailarchive.app.messagebox.showerror")
    def test_save_rule_reports_invalid_attachment_value(self, showerror) -> None:
        dialog = make_rule_dialog()
        dialog.field_var.set("Has attachments")
        dialog.value_var.set("sometimes")

        dialog._save()

        self.assertIsNone(dialog.result)
        self.assertIn("yes or no", showerror.call_args.args[1].lower())
        dialog.destroy.assert_not_called()

    @patch("mailarchive.app.messagebox.showerror")
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
            patch("mailarchive.app.ArchiveState") as archive_state,
            patch("mailarchive.app.ArchiveService") as service,
            patch("mailarchive.app.BackgroundRunner") as runner,
            patch("mailarchive.app.TrayController") as tray,
            patch("mailarchive.app.set_start_at_login") as startup,
        ):
            desktop = DesktopApp(root, store, settings, credential_store)

        root.protocol.assert_called_once_with("WM_DELETE_WINDOW", desktop.hide_to_tray)
        archive_state.assert_called_once_with(Path("/state.sqlite3"))
        service.assert_called_once_with(
            credential_store, archive_state.return_value, desktop.on_service_event
        )
        runner.return_value.start.assert_called_once_with()
        tray.assert_called_once_with(
            desktop.post_ui, desktop.show, desktop.run_now, desktop.quit
        )
        startup.assert_called_once_with(True)
        root.after.assert_called_once_with(100, desktop._drain_ui_queue)

    def test_refresh_and_selection_reflect_settings(self) -> None:
        active = Account(
            id="active",
            label="Work",
            host="imap.example.com",
            username="work@example.com",
            poll_minutes=None,
        )
        paused = Account(
            id="paused",
            label="Personal",
            host="imap.example.com",
            username="me@example.com",
            poll_minutes=15,
            enabled=False,
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
        self.assertEqual(desktop.rule_tree.rows[0]["values"][2], 'Subject contains "invoice"')
        self.assertEqual(desktop.account_summary.get(), "1")
        self.assertEqual(desktop.rule_summary.get(), "1")
        desktop.account_tree.selected = ("paused",)
        desktop.rule_tree.selected = ("rule-1",)
        self.assertIs(desktop._selected_account(), paused)
        self.assertIs(desktop._selected_rule(), rule)

    @patch("mailarchive.app.save_credential_data")
    @patch("mailarchive.app.load_credential_data")
    def test_store_credentials_filters_by_provider_and_can_replace(
        self, load, save
    ) -> None:
        desktop = make_desktop()
        load.return_value = {
            "google_credentials": {"token": "old"},
            "oauth_client_secret": "old-secret",
            "password": "must-not-leak",
        }
        account = Account(
            id="gmail",
            label="Gmail",
            username="mail@example.com",
            provider=MailProvider.GMAIL_API,
            auth_mode=AuthMode.OAUTH_USER,
            client_id="client-id",
        )

        desktop._store_account_credentials(
            account,
            {"oauth_client_secret": "new-secret", "password": "ignored"},
        )

        save.assert_called_once_with(
            desktop.credential_store,
            "gmail",
            {
                "google_credentials": {"token": "old"},
                "oauth_client_secret": "new-secret",
            },
        )

        save.reset_mock()
        desktop._store_account_credentials(account, {}, replace=True)
        load.assert_called_once()
        save.assert_not_called()
        desktop.credential_store.delete.assert_called_once_with("gmail")

    def test_authorize_paths_explain_noninteractive_accounts(self) -> None:
        desktop = make_desktop()
        with patch("mailarchive.app.messagebox.showinfo") as showinfo:
            desktop._selected_account = MagicMock(return_value=None)
            desktop.authorize_selected_account()
            self.assertEqual(showinfo.call_args.args[0], "Select an account")

            desktop._selected_account.return_value = Account(
                label="IMAP", host="imap.example.com", username="mail@example.com"
            )
            desktop.authorize_selected_account()
            self.assertEqual(showinfo.call_args.args[0], "Authorization not required")

            desktop._selected_account.return_value = Account(
                label="Workspace",
                username="mail@example.com",
                provider=MailProvider.GMAIL_API,
                auth_mode=AuthMode.OAUTH_APPLICATION,
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
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop.on_service_event = MagicMock()
        ImmediateThread.created.clear()

        with (
            patch("mailarchive.app.threading.Thread", ImmediateThread),
            patch("mailarchive.app.authorize_account") as authorize,
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

    def test_remove_account_rolls_back_failed_persistence(self) -> None:
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop._persist = MagicMock(side_effect=RuntimeError("disk full"))
        desktop.refresh_all = MagicMock()

        with (
            patch("mailarchive.app.messagebox.askyesno", return_value=True),
            patch("mailarchive.app.messagebox.showerror") as showerror,
        ):
            desktop.remove_account()

        self.assertEqual(desktop.settings.accounts, [account])
        desktop.refresh_all.assert_called_once_with()
        showerror.assert_called_once()
        desktop.credential_store.delete.assert_not_called()

    def test_remove_account_warns_when_credential_cleanup_fails(self) -> None:
        account = Account(
            id="account-1",
            label="Work",
            host="imap.example.com",
            username="mail@example.com",
        )
        desktop = make_desktop(Settings(archive_root="/archive", accounts=[account]))
        desktop._selected_account = MagicMock(return_value=account)
        desktop._persist = MagicMock()
        desktop.credential_store.delete.side_effect = RuntimeError("locked")

        with (
            patch("mailarchive.app.messagebox.askyesno", return_value=True),
            patch("mailarchive.app.messagebox.showwarning") as showwarning,
        ):
            desktop.remove_account()

        self.assertEqual(desktop.settings.accounts, [])
        desktop._persist.assert_called_once_with()
        showwarning.assert_called_once()

    def test_add_rule_inserts_before_catch_all_and_move_keeps_selection(self) -> None:
        catch_all = Rule(
            "All", "Inbox", [Condition(MailField.ALL)], id="catch-all"
        )
        new_rule = Rule(
            "Invoices",
            "Finance",
            [Condition(MailField.SUBJECT, value="invoice")],
            id="invoices",
        )
        desktop = make_desktop(Settings(archive_root="/archive", rules=[catch_all]))
        desktop._persist = MagicMock()
        dialog = SimpleNamespace(result=new_rule)

        with patch("mailarchive.app.RuleDialog", return_value=dialog):
            desktop.add_rule()

        self.assertEqual(desktop.settings.rules, [new_rule, catch_all])
        desktop.rule_tree.selected = ("invoices",)
        desktop.move_rule(1)
        self.assertEqual(desktop.settings.rules, [catch_all, new_rule])
        self.assertEqual(desktop.rule_tree.selected, ("invoices",))
        self.assertEqual(desktop._persist.call_count, 2)

    def test_remove_rule_enforces_at_least_one_and_confirmation(self) -> None:
        rule = Rule("All", "Inbox", id="rule-1")
        desktop = make_desktop(Settings(archive_root="/archive", rules=[rule]))
        desktop._selected_rule = MagicMock(return_value=rule)
        desktop._persist = MagicMock()

        with patch("mailarchive.app.messagebox.showerror") as showerror:
            desktop.remove_rule()
        showerror.assert_called_once()
        desktop._persist.assert_not_called()

        second = Rule("Second", "Other", id="rule-2")
        desktop.settings.rules.append(second)
        with patch("mailarchive.app.messagebox.askyesno", return_value=True):
            desktop.remove_rule()
        self.assertEqual(desktop.settings.rules, [second])
        desktop._persist.assert_called_once_with()

    def test_file_choosers_and_default_database_update_variables(self) -> None:
        desktop = make_desktop()
        desktop.database_var.set("/old/state.sqlite3")
        desktop.config_store.default_state_database_path = Path("/default/state.sqlite3")
        with (
            patch("mailarchive.app.filedialog.askdirectory", return_value="/new/archive"),
            patch(
                "mailarchive.app.filedialog.asksaveasfilename",
                return_value="/new/state.sqlite3",
            ),
        ):
            desktop.choose_archive()
            desktop.choose_state_database()
        self.assertEqual(desktop.archive_var.get(), "/new/archive")
        self.assertEqual(desktop.database_var.get(), "/new/state.sqlite3")
        desktop.use_default_state_database()
        self.assertEqual(
            desktop.database_var.get(),
            str(desktop.config_store.default_state_database_path),
        )

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
                patch("mailarchive.app.set_start_at_login") as startup,
                patch("mailarchive.app.messagebox.showinfo") as showinfo,
            ):
                desktop.save_settings()

        self.assertIs(desktop.state, relocated)
        self.assertEqual(desktop.settings.default_poll_minutes, 10)
        self.assertEqual(desktop.settings.archive_root, str(archive.resolve()))
        self.assertEqual(desktop.settings.state_database_path, str(new_database.resolve()))
        desktop.config_store.save.assert_called_once_with(desktop.settings)
        startup.assert_called_once_with(True)
        desktop.refresh_all.assert_called_once_with()
        showinfo.assert_called_once()

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
                patch("mailarchive.app.set_start_at_login") as startup,
                patch("mailarchive.app.messagebox.showerror") as showerror,
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

    def test_save_settings_rejects_bad_poll_interval_before_side_effects(self) -> None:
        desktop = make_desktop()
        desktop.poll_var.set("0")
        with patch("mailarchive.app.messagebox.showerror") as showerror:
            desktop.save_settings()
        self.assertIn("between 1 and 1440", showerror.call_args.args[1])
        desktop.service.relocate_state_database.assert_not_called()
        desktop.config_store.save.assert_not_called()

    def test_queue_event_display_and_log_limit(self) -> None:
        desktop = make_desktop()
        callback = MagicMock()
        desktop.post_ui(callback)
        desktop.root.after.reset_mock()
        desktop._drain_ui_queue()
        callback.assert_called_once_with()
        desktop.root.after.assert_called_once_with(100, desktop._drain_ui_queue)

        desktop.log_tree.rows = [
            {"iid": f"old-{index}"} for index in range(300)
        ]
        event = ServiceEvent(
            EventLevel.ERROR,
            "Authentication failed",
            created_at=datetime(2026, 9, 12, 8, 30, 0),
        )
        desktop._display_event(event)
        self.assertEqual(desktop.status_var.get(), "Authentication failed")
        self.assertEqual(desktop.log_tree.rows[0]["values"][0], "2026-09-12 08:30:00")
        desktop.tray.set_state.assert_called_with(
            "error", "MailArchive - problem detected"
        )
        desktop.tray.notify.assert_called_once_with("Authentication failed")
        self.assertTrue(desktop.log_tree.deleted)

        desktop._display_event(ServiceEvent(EventLevel.WARNING, "Slow"))
        desktop.tray.set_state.assert_called_with(
            "warning", "MailArchive - attention required"
        )
        desktop._display_event(ServiceEvent(EventLevel.SUCCESS, "Done"))
        desktop.tray.set_state.assert_called_with("ok", "MailArchive - ready")
        desktop._display_event(ServiceEvent(EventLevel.INFO, "Checking"))

    def test_run_visibility_and_quit_lifecycle(self) -> None:
        desktop = make_desktop()
        desktop.run_now()
        self.assertEqual(desktop.status_var.get(), "Starting archive run...")
        desktop.runner.run_now.assert_called_once_with()

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

    @unittest.skipUnless(app_module.os.name == "posix", "POSIX folder opener")
    @patch("mailarchive.app.subprocess.Popen")
    def test_open_archive_creates_and_opens_folder(self, popen) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "archive"
            desktop = make_desktop(Settings(archive_root=str(archive)))
            with patch.object(app_module.sys, "platform", "linux"):
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

    def test_main_loads_defaults_reports_credential_warning_and_minimizes(self) -> None:
        instance = MagicMock(already_running=False)
        root = MagicMock()
        store = MagicMock()
        store.load.side_effect = RuntimeError("broken config")
        credential_store = MagicMock()
        application = MagicMock()
        application.tray.safe_to_hide = True
        with (
            patch("mailarchive.app.SingleInstance", return_value=instance),
            patch("mailarchive.app.tk.Tk", return_value=root),
            patch("mailarchive.app.ConfigStore", return_value=store),
            patch("mailarchive.app.Settings.defaults", return_value=Settings("/archive")),
            patch("mailarchive.app.KeyringCredentialStore", side_effect=RuntimeError("no keyring")),
            patch("mailarchive.app.UnavailableCredentialStore", return_value=credential_store),
            patch("mailarchive.app.DesktopApp", return_value=application),
            patch("mailarchive.app.messagebox.showerror") as showerror,
            patch.object(app_module.os, "name", "posix"),
            patch.object(sys, "argv", ["mailarchive", "--minimized"]),
        ):
            app_module.main()

        showerror.assert_called_once_with("MailArchive", "broken config")
        application.on_service_event.assert_called_once()
        warning = application.on_service_event.call_args.args[0]
        self.assertEqual(warning.level, EventLevel.ERROR)
        self.assertIn("no keyring", warning.message)
        root.withdraw.assert_called_once_with()
        root.mainloop.assert_called_once_with()
        instance.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
