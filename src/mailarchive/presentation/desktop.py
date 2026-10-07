from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import font, messagebox, simpledialog, ttk

from mailarchive import APP_NAME, __version__
from mailarchive.application.account_status import AccountStatus, AuthorizationState
from mailarchive.application.background import BackgroundResult
from mailarchive.application.desktop_integration import DesktopIntegrationPort
from mailarchive.application.events import EventLevel, ExecutionState, RunProgress, ServiceEvent
from mailarchive.application.polling import AutomaticMonitoringState
from mailarchive.application.session import MailArchiveApplication
from mailarchive.application.update_port import Release
from mailarchive.domain.configuration import Account, Rule, Settings
from mailarchive.presentation.archive_activity_dialog import ArchiveActivityDialog
from mailarchive.presentation.desktop_setup import DesktopIntegrationUI
from mailarchive.presentation.dialogs import (
    AccountDialog,
    RangeDialog,
    RuleDialog,
    _wrap_label_to_width,
)
from mailarchive.presentation.responsive_actions import ResponsiveActions
from mailarchive.presentation.scrollable_frame import ScrollableFrame
from mailarchive.presentation.settings_form import (
    SettingsFormValues,
    SettingsUpdate,
    prepare_settings_update,
)
from mailarchive.presentation.timezone_choices import local_timezone_name, timezone_choices
from mailarchive.presentation.tray import TrayController
from mailarchive.presentation.ui_text import (
    ACCOUNT_STATE_LABELS,
    PROVIDER_LABELS,
    SAVE_LABELS,
    _account_scope_summary,
    _condition_summary,
    _destination_summary,
    _label_for,
)

LOG_FILTERS = {
    "Last 50": None,
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days": timedelta(days=7),
    "Last 30 days": timedelta(days=30),
    "All time": None,
}
LOG_PAGE_SIZE = 50
ACCOUNT_STATUS_RETRY_SECONDS = 1.0
logger = logging.getLogger(__name__)


class DesktopApp:
    def __init__(
        self,
        root: tk.Tk,
        application: MailArchiveApplication,
        desktop_integration: DesktopIntegrationPort | None = None,
    ) -> None:
        self.root = root
        self.application = application
        self.settings = application.settings
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._closing = False
        self._account_status_revision = None
        self._account_status_retry_at: float | None = None
        self._archive_summary_refresh_at = 0.0
        self._account_selection_after_refresh: str | None = None
        self._saving_settings = False
        self._profile_switch_update: SettingsUpdate | None = None
        self._setting_entry_fields: dict[ttk.Entry, str] = {}
        self._checking_for_updates = False
        self._archive_running = False
        self._check_id: str | None = None
        self._check_progress: RunProgress | None = None
        self._other_progress: RunProgress | None = None
        self._seen_progress: dict[str, RunProgress] = {}
        self._stop_requested = False
        self._run_event_level = EventLevel.INFO
        self._run_started_at = 0.0
        self._progress_timer: str | None = None
        self._monitoring_state: AutomaticMonitoringState | None = None
        self.desktop_integration = (
            DesktopIntegrationUI(
                root,
                desktop_integration,
                lambda: self.settings.start_at_login,
                application.submit_background,
            )
            if desktop_integration is not None
            else None
        )

        root.title(APP_NAME)
        root.geometry("980x680")
        root.minsize(820, 580)
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._configure_style()
        self._build_ui()
        self.tray = TrayController(
            self.post_ui,
            self.show,
            self.run_now,
            self.quit,
            self._toggle_automatic_monitoring,
            restore_on_tray_loss=self._restore_after_tray_loss,
        )
        self.root.after(100, self._drain_ui_queue)
        self.refresh_all()
        self.refresh_log()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Sub.TLabel", foreground="#555555", font=("Segoe UI", 10))
        row_font = font.Font(
            root=self.root, font=style.lookup("Treeview", "font") or "TkDefaultFont"
        )
        style.configure("Treeview", rowheight=max(28, row_font.metrics("linespace") + 8))

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=(22, 18))
        container.pack(fill="both", expand=True)
        header = ttk.Frame(container)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="MailArchive", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text=f"v{__version__}", style="Sub.TLabel").pack(side="left", padx=(8, 0))
        ttk.Button(header, text="Quit", command=self.quit).pack(side="right")
        self.check_button = ttk.Button(header, text="Check mail now", command=self._check_clicked)
        self.check_button.pack(side="right", padx=(0, 8))
        self.automatic_button = ttk.Button(
            header, text="Pause automatic checks", command=self._toggle_automatic_monitoring
        )
        self.automatic_button.pack(side="right", padx=(0, 8))
        self.automatic_status_var = tk.StringVar()
        ttk.Label(container, textvariable=self.automatic_status_var).pack(fill="x", pady=(0, 8))

        progress = ttk.Frame(container)
        progress.pack(fill="x", pady=(0, 12))
        self.progress_var = tk.StringVar(value="No archive run in progress.")
        progress_label = ttk.Label(
            progress, textvariable=self.progress_var, anchor="nw", justify="left", width=1
        )
        progress_label.pack(side="left", fill="x", expand=True, anchor="n")
        progress_label.bind(
            "<Configure>",
            lambda event: progress_label.configure(wraplength=max(event.width, 1)),
        )
        self.elapsed_var = tk.StringVar(value="")
        # Keep the indicators at the top when the status message wraps onto more lines.
        ttk.Label(progress, textvariable=self.elapsed_var).pack(
            side="right", padx=(8, 0), anchor="n"
        )
        self.progress_bar = ttk.Progressbar(progress, mode="indeterminate", length=110)

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        self.dashboard_tab = ttk.Frame(self.notebook, padding=18)
        self.accounts_tab = ttk.Frame(self.notebook, padding=18)
        self.rules_tab = ttk.Frame(self.notebook, padding=18)
        self.settings_tab = ttk.Frame(self.notebook, padding=18)
        self.log_tab = ttk.Frame(self.notebook, padding=18)
        for tab, label in [
            (self.dashboard_tab, "Overview"),
            (self.accounts_tab, "Accounts"),
            (self.rules_tab, "Rules"),
            (self.settings_tab, "Settings"),
            (self.log_tab, "Activity log"),
        ]:
            self.notebook.add(tab, text=label)
        self._build_dashboard()
        self._build_accounts()
        self._build_rules()
        self._build_settings()
        self._build_log()

    def _build_dashboard(self) -> None:
        ttk.Label(self.dashboard_tab, text="Local email archive", style="Header.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            self.dashboard_tab,
            text="MailArchive checks your email accounts in the background and saves matching emails according to your rules.",
            style="Sub.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(4, 20))
        summary = ttk.Frame(self.dashboard_tab)
        summary.pack(fill="x")
        self.account_summary = tk.StringVar()
        self.rule_summary = tk.StringVar()
        self.archive_summary = tk.StringVar()
        for column, (title, variable) in enumerate(
            [
                ("Active email accounts", self.account_summary),
                ("Active rules", self.rule_summary),
                ("Pending archive work", self.archive_summary),
            ]
        ):
            card = ttk.LabelFrame(summary, text=title, padding=14)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            ttk.Label(
                card, textvariable=variable, font=("Segoe UI", 12, "bold"), wraplength=260
            ).pack(anchor="w")
            summary.columnconfigure(column, weight=1)
        actions = ttk.Frame(self.dashboard_tab)
        actions.pack(fill="x", pady=(24, 8))
        ttk.Button(actions, text="Add email account", command=self.add_account).pack(side="left")
        ttk.Button(actions, text="Add rule", command=self.add_rule).pack(side="left", padx=8)
        self.update_button = ttk.Button(
            actions, text="Check for updates", command=self.check_for_updates
        )
        self.update_button.pack(side="right")
        work_actions = ttk.Frame(self.dashboard_tab)
        work_actions.pack(fill="x", pady=(0, 16))
        ttk.Button(work_actions, text="Archive activity", command=self.show_archive_activity).pack(
            side="left"
        )
        ttk.Label(
            self.dashboard_tab,
            text="Note: Emails on the server are never deleted, moved, or marked as read.",
            foreground="#18794e",
        ).pack(anchor="w", pady=(10, 0))

    def check_for_updates(self) -> None:
        if self._checking_for_updates:
            return
        self._checking_for_updates = True
        self.update_button.configure(state="disabled", text="Checking")

        try:
            self.application.check_for_updates(
                lambda release, error: self._finish_update_check(release, error=error)
            )
        except Exception as exc:
            self._finish_update_check(error=str(exc))

    def _finish_update_check(self, release: Release | None = None, *, error: str = "") -> None:
        self._checking_for_updates = False
        self.update_button.configure(state="normal", text="Check for updates")
        if error:
            messagebox.showerror("Update check failed", error, parent=self.root)
        elif release is None:
            messagebox.showinfo(
                "MailArchive updates",
                f"No newer stable release is available.\nInstalled version: {__version__}",
                parent=self.root,
            )
        elif messagebox.askyesno(
            "MailArchive update available",
            f"MailArchive {release.version} is available.\nInstalled version: {__version__}\n\n"
            "Open the release page to download the update?\n"
            "Quit MailArchive before running the Windows installer or starting the new Linux AppImage.\n"
            "For an integrated Linux installation, use Settings > Desktop integration > Configure > Apply "
            "in the new AppImage, then quit and reopen MailArchive.",
            parent=self.root,
        ):
            try:
                if not webbrowser.open(release.url):
                    raise OSError("The system browser could not be opened.")
            except OSError as exc:
                messagebox.showerror("Could not open release page", str(exc), parent=self.root)

    def _build_accounts(self) -> None:
        ttk.Label(self.accounts_tab, text="Email accounts", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.accounts_tab,
            text="Use generic IMAP, the Gmail API, or Microsoft Graph for Outlook and Microsoft 365.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        columns = ("name", "provider", "user", "interval", "active")
        self.account_tree = ttk.Treeview(
            self.accounts_tab, columns=columns, show="headings", selectmode="browse"
        )
        for key, title, width in [
            ("name", "Name", 150),
            ("provider", "Provider", 230),
            ("user", "Mailboxes", 220),
            ("interval", "Polling", 100),
            ("active", "Status", 220),
        ]:
            self.account_tree.heading(key, text=title)
            self.account_tree.column(key, width=width, anchor="w")
        self.account_tree.pack(fill="both", expand=True)
        self.account_tree.bind("<Double-1>", lambda event: self.edit_account())
        self.account_tree.bind("<<TreeviewSelect>>", lambda event: self._refresh_account_notice())
        self.account_notice_var = tk.StringVar(master=self.root)
        notice = ttk.Label(self.accounts_tab, textvariable=self.account_notice_var, wraplength=850)
        buttons = ResponsiveActions(
            self.accounts_tab,
            (
                ("Add", self.add_account),
                ("Edit", self.edit_account),
                ("Remove", self.remove_account),
                ("Reset paused folder", self.reset_paused_folder),
            ),
        )
        buttons.pack(side="bottom", fill="x", pady=(12, 0), before=self.account_tree)
        notice.pack(side="bottom", fill="x", pady=(8, 0), before=self.account_tree)

    def _build_rules(self) -> None:
        ttk.Label(self.rules_tab, text="Archive rules", style="Header.TLabel").pack(anchor="w")
        description = ttk.Label(
            self.rules_tab,
            text="Rules are evaluated from top to bottom for each email account. The first match determines the destination and save mode.",
            style="Sub.TLabel",
            width=1,
            wraplength=720,
        )
        description.pack(fill="x", pady=(4, 12))
        description.bind(
            "<Configure>",
            lambda event: description.configure(wraplength=max(event.width, 1)),
        )
        table = ttk.Frame(self.rules_tab)
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("order", "name", "accounts", "condition", "destination", "mode", "active")
        self.rule_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        cell_font = font.Font(
            root=self.root,
            font=ttk.Style(self.root).lookup("Treeview", "font") or "TkDefaultFont",
        )
        order_width = max(32, cell_font.measure("999") + 10)
        mode_width = max(cell_font.measure(label) for label in SAVE_LABELS) + 16
        status_width = cell_font.measure("Inactive") + 16
        for key, title, width, minwidth, stretch, anchor in [
            ("order", "#", order_width, order_width, False, "center"),
            ("name", "Name", 120, 100, True, "w"),
            ("accounts", "Email accounts", 140, 130, True, "w"),
            ("condition", "When", 200, 180, True, "w"),
            ("destination", "Destination", 140, 130, True, "w"),
            ("mode", "Save as", mode_width, mode_width, False, "w"),
            ("active", "Status", status_width, status_width, False, "w"),
        ]:
            self.rule_tree.heading(key, text=title, anchor=anchor)
            self.rule_tree.column(
                key, width=width, minwidth=minwidth, stretch=stretch, anchor=anchor
            )
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.rule_tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.rule_tree.xview)
        self.rule_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.rule_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self._rule_outer_resize_blocked = False
        self.rule_tree.bind("<Motion>", self._rule_table_motion)
        self.rule_tree.bind("<ButtonPress-1>", self._preserve_rule_column_widths)
        self.rule_tree.bind("<ButtonRelease-1>", self._rule_table_release)
        self.rule_tree.bind(
            "<Double-1>",
            lambda event: (
                self.edit_rule()
                if self.rule_tree.identify_region(event.x, event.y) == "cell"
                else self._preserve_rule_column_widths(event)
            ),
        )
        buttons = ResponsiveActions(
            self.rules_tab,
            (
                ("Add", self.add_rule),
                ("Edit", self.edit_rule),
                ("Remove", self.remove_rule),
                ("Apply to past mail", self.run_rule_history_dialog),
                ("Move up", lambda: self.move_rule(-1)),
                ("Move down", lambda: self.move_rule(1)),
            ),
        )
        buttons.pack(side="bottom", fill="x", pady=(12, 0), before=table)

    def _is_rule_table_outer_separator(self, event: tk.Event) -> bool:
        return (
            self.rule_tree.identify_region(event.x, event.y) == "separator"
            and self.rule_tree.identify_column(event.x) == f"#{len(self.rule_tree['columns'])}"
        )

    def _rule_table_motion(self, event: tk.Event) -> str | None:
        if self._rule_outer_resize_blocked or (
            not event.state & 0x100  # Button1Mask: allow inner separators to drag across the edge.
            and self._is_rule_table_outer_separator(event)
        ):
            self.rule_tree.configure(cursor="")
            return "break"
        return None

    def _rule_table_release(self, event: tk.Event) -> str | None:
        if self._rule_outer_resize_blocked:
            self._rule_outer_resize_blocked = False
            self.rule_tree.configure(cursor="")
            return "break"
        return None

    def _preserve_rule_column_widths(self, event: tk.Event) -> str | None:
        self._rule_outer_resize_blocked = self._is_rule_table_outer_separator(event)
        if self._rule_outer_resize_blocked:
            self.rule_tree.configure(cursor="")
            return "break"
        if self.rule_tree.identify_region(event.x, event.y) != "separator":
            return None
        # Preserve manual widths; let Tk's final column absorb spare space so the
        # headings always fill the viewport without redistributing the other columns.
        widths = {key: self.rule_tree.column(key, "width") for key in self.rule_tree["columns"]}
        for key, width in widths.items():
            self.rule_tree.column(key, width=width, stretch=key == "active")
        return None

    def _build_settings(self) -> None:
        ttk.Label(self.settings_tab, text="Settings", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        settings_pages = self.settings_pages = ttk.Notebook(self.settings_tab)
        settings_pages.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(18, 0))
        general_container = ttk.Frame(settings_pages, padding=16)
        advanced_container = ttk.Frame(settings_pages, padding=16)
        settings_pages.add(general_container, text="General")
        settings_pages.add(advanced_container, text="Advanced")
        self.general_settings_scroll = ScrollableFrame(general_container)
        self.general_settings_scroll.pack(fill="both", expand=True)
        self.advanced_settings_scroll = ScrollableFrame(advanced_container)
        self.advanced_settings_scroll.pack(fill="both", expand=True)
        general_page = self.general_settings_scroll.content
        advanced_page = self.advanced_settings_scroll.content
        if self.desktop_integration is not None:
            self.desktop_integration.add_settings_page(settings_pages)

        poll_label = ttk.Label(general_page, text="Default polling interval (minutes)")
        poll_label.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 6),
        )
        _wrap_label_to_width(poll_label)
        self.poll_var = tk.StringVar(value=str(self.settings.default_poll_minutes))
        poll_entry = ttk.Entry(general_page, textvariable=self.poll_var, width=12)
        poll_entry.grid(
            row=1,
            column=0,
            sticky="w",
        )
        self._bind_setting_entry(poll_entry, "default_poll_minutes")
        poll_hint = ttk.Label(
            general_page,
            text="Used by every account without its own polling override.",
            style="Sub.TLabel",
        )
        poll_hint.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        _wrap_label_to_width(poll_hint)
        self.startup_var = tk.BooleanVar(value=self.settings.start_at_login)
        self.minimize_var = tk.BooleanVar(value=self.settings.minimize_to_tray)
        self.warning_var = tk.BooleanVar(value=self.settings.warn_on_error)
        ttk.Checkbutton(
            general_page,
            text="Start automatically at login",
            variable=self.startup_var,
            command=lambda: self.save_settings("start_at_login"),
        ).grid(row=3, column=0, sticky="w", pady=(22, 6))
        ttk.Checkbutton(
            general_page,
            text="Keep running in the notification area when closed",
            variable=self.minimize_var,
            command=lambda: self.save_settings("minimize_to_tray"),
        ).grid(row=4, column=0, sticky="w", pady=6)
        ttk.Checkbutton(
            general_page,
            text="Show a desktop notification when an error occurs",
            variable=self.warning_var,
            command=lambda: self.save_settings("warn_on_error"),
        ).grid(row=5, column=0, sticky="w", pady=6)
        ttk.Label(general_page, text="Archive date timezone").grid(
            row=6, column=0, sticky="w", pady=(16, 4)
        )
        self.timezone_var = tk.StringVar(value=self.settings.archive_timezone)
        timezone_box = ttk.Combobox(
            general_page,
            textvariable=self.timezone_var,
            values=timezone_choices(self.settings.archive_timezone),
            state="readonly",
        )
        timezone_box.grid(row=7, column=0, sticky="ew")
        timezone_box.bind(
            "<<ComboboxSelected>>", lambda _event: self.save_settings("archive_timezone")
        )
        self.profile_switch_status_var = tk.StringVar(master=self.root)
        ttk.Label(advanced_page, textvariable=self.profile_switch_status_var, wraplength=700).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        self.database_var = tk.StringVar(value=str(self.application.database_path))
        ttk.Label(advanced_page, text="Database").grid(row=0, column=0, sticky="w")
        database_entry = ttk.Entry(advanced_page, textvariable=self.database_var)
        database_entry.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self._bind_setting_entry(database_entry, "state_database_path")
        settings_hint = ttk.Label(
            self.settings_tab,
            text="Changes are saved automatically. For text fields, press Enter or leave the field.",
            style="Sub.TLabel",
            wraplength=720,
        )
        settings_hint.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        _wrap_label_to_width(settings_hint)
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.columnconfigure(1, weight=1)
        self.settings_tab.rowconfigure(1, weight=1)
        general_page.columnconfigure(0, weight=1)
        advanced_page.columnconfigure(0, weight=1)
        self.general_settings_scroll.bind_widgets()
        self.advanced_settings_scroll.bind_widgets()

    def _bind_setting_entry(self, entry: ttk.Entry, field: str) -> None:
        self._setting_entry_fields[entry] = field
        for event in ("<FocusOut>", "<Return>"):
            entry.bind(event, lambda _event, field=field: self.save_settings(field))

    def _save_focused_setting(self) -> None:
        field = self._setting_entry_fields.get(self.root.focus_get())
        if field is not None:
            self.save_settings(field)

    def _build_log(self) -> None:
        ttk.Label(self.log_tab, text="Activity log", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.log_tab,
            text="Checks, warnings, and errors are saved locally across restarts until cleared.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        controls = ttk.Frame(self.log_tab)
        controls.pack(fill="x", pady=(0, 12))
        ttk.Label(controls, text="Show").pack(side="left", padx=(0, 8))
        self.log_filter_var = tk.StringVar(value="Last 50")
        self._log_offset = 0
        filters = ttk.Combobox(
            controls,
            textvariable=self.log_filter_var,
            values=tuple(LOG_FILTERS),
            state="readonly",
            width=18,
        )
        filters.pack(side="left")
        filters.bind("<<ComboboxSelected>>", lambda event: self.refresh_log(reset_page=True))
        ttk.Button(controls, text="Refresh", command=self.refresh_log).pack(side="left", padx=8)
        ttk.Button(controls, text="Clear log", command=self.clear_log).pack(side="right")
        columns = ("time", "level", "message")
        table = ttk.Frame(self.log_tab)
        table.pack(fill="both", expand=True)
        self.log_tree = ttk.Treeview(table, columns=columns, show="headings")
        for key, title, width in [
            ("time", "Time", 170),
            ("level", "Status", 90),
            ("message", "Message", 650),
        ]:
            self.log_tree.heading(key, text=title)
            self.log_tree.column(key, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_tree.pack(side="left", fill="both", expand=True)
        footer = ttk.Frame(self.log_tab)
        footer.pack(side="bottom", fill="x", pady=(10, 0), before=table)
        self.log_summary_var = tk.StringVar()
        ttk.Label(footer, textvariable=self.log_summary_var, wraplength=500).pack(side="left")
        self.log_next_button = ttk.Button(
            footer, text="Next", command=lambda: self.change_log_page(1)
        )
        self.log_next_button.pack(side="right")
        self.log_previous_button = ttk.Button(
            footer, text="Previous", command=lambda: self.change_log_page(-1)
        )
        self.log_previous_button.pack(side="right", padx=8)

    def _profile_available(self) -> bool:
        return self.application.automatic_monitoring_state() != AutomaticMonitoringState.UNAVAILABLE

    def refresh_log(self, *, reset_page: bool = False) -> None:
        if not self._profile_available():
            self.log_summary_var.set("Activity log unavailable while restoring the profile.")
            return
        if reset_page or self.log_filter_var.get() == "Last 50":
            self._log_offset = 0
        duration = LOG_FILTERS[self.log_filter_var.get()]
        since = datetime.now().astimezone() - duration if duration is not None else None
        try:
            page = self.application.activity_log_page(
                since=since, offset=self._log_offset, limit=LOG_PAGE_SIZE
            )
        except Exception as exc:
            self.log_summary_var.set(f"Could not load activity log: {exc}")
            return
        self._log_offset = page.offset
        total = (
            min(page.total, LOG_PAGE_SIZE) if self.log_filter_var.get() == "Last 50" else page.total
        )
        self.log_tree.delete(*self.log_tree.get_children())
        for event in page.events:
            self.log_tree.insert(
                "",
                "end",
                values=(
                    event.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    event.level.value.title(),
                    event.message,
                ),
            )
        self.log_summary_var.set(
            f"Showing {page.offset + 1}-{page.offset + len(page.events)} of {total} entries"
            if total
            else "No activity in this view."
        )
        self.log_previous_button.configure(state="normal" if page.offset else "disabled")
        self.log_next_button.configure(
            state="normal" if page.offset + len(page.events) < total else "disabled"
        )

    def change_log_page(self, direction: int) -> None:
        self._log_offset = max(0, self._log_offset + direction * LOG_PAGE_SIZE)
        self.refresh_log()

    def clear_log(self) -> None:
        if not self._profile_available():
            return
        if not messagebox.askyesno(
            "Clear activity log?",
            "Permanently delete all saved activity log entries, including entries outside "
            "the current filter?\n\nArchived files and processing history will be kept.",
            parent=self.root,
        ):
            return
        try:
            self.application.clear_activity_log()
        except Exception as exc:
            messagebox.showerror("Activity log not cleared", str(exc), parent=self.root)
            return
        self.refresh_log(reset_page=True)

    def refresh_all(self) -> None:
        self._refresh_monitoring_controls()
        self._refresh_account_rows()
        self.rule_tree.delete(*self.rule_tree.get_children())
        for index, rule in enumerate(self.settings.rules, start=1):
            self.rule_tree.insert(
                "",
                "end",
                iid=rule.id,
                values=(
                    index,
                    rule.name,
                    _account_scope_summary(rule, self.settings.accounts),
                    _condition_summary(rule),
                    _destination_summary(rule),
                    ", ".join(_label_for(SAVE_LABELS, target.save_mode) for target in rule.targets),
                    "Active" if rule.enabled else "Inactive",
                ),
            )
        self.account_summary.set(str(sum(account.enabled for account in self.settings.accounts)))
        self.rule_summary.set(str(sum(rule.enabled for rule in self.settings.rules)))
        self._refresh_archive_summary(force=True)

    def _refresh_archive_summary(self, *, force: bool = False) -> None:
        if not self._profile_available():
            self.archive_summary.set("Work queue unavailable")
            return
        now = time.monotonic()
        if not force and now < self._archive_summary_refresh_at:
            return
        self._archive_summary_refresh_at = now + 1.0
        try:
            status = self.application.status()
            self.archive_summary.set(
                f"{status.pending_count} pending / {status.spool_bytes / 1024**2:.1f} MiB"
            )
        except Exception:
            self.archive_summary.set("Work queue unavailable")

    def _refresh_account_rows(self) -> None:
        if not self._profile_available():
            self.account_notice_var.set("Account statuses unavailable while restoring the profile.")
            return
        if (
            self._account_status_retry_at is not None
            and time.monotonic() < self._account_status_retry_at
        ):
            return
        revision = (self.application.database_path, self.application.account_statuses.revision)
        selected = self.account_tree.selection()
        selected_id = self._account_selection_after_refresh or (selected[0] if selected else None)
        try:
            statuses = {
                account.id: self.application.account_status(account.id)
                for account in self.settings.accounts
            }
        except Exception:
            self._account_status_refresh_failed()
            return
        rows = [
            (
                account.id,
                (
                    account.label,
                    _label_for(PROVIDER_LABELS, account.provider),
                    ", ".join(mailbox.address for mailbox in account.mailboxes),
                    f"{account.poll_minutes or self.settings.default_poll_minutes} min"
                    + (" (default)" if account.poll_minutes is None else ""),
                    ACCOUNT_STATE_LABELS[statuses[account.id].state],
                ),
            )
            for account in self.settings.accounts
        ]
        account = next((a for a in self.settings.accounts if a.id == selected_id), None)
        notice = self._account_notice(account, statuses[account.id]) if account is not None else ""
        self.account_tree.delete(*self.account_tree.get_children())
        for account_id, values in rows:
            self.account_tree.insert("", "end", iid=account_id, values=values)
        if selected_id in statuses:
            self.account_tree.selection_set(selected_id)
        self.account_notice_var.set(notice)
        self._account_status_revision = revision
        self._account_status_retry_at = None
        self._account_selection_after_refresh = None

    def _account_status_refresh_failed(self) -> None:
        if self._account_status_retry_at is None:
            logger.exception("Could not refresh account statuses; keeping the previous rows.")
        self._account_status_retry_at = time.monotonic() + ACCOUNT_STATUS_RETRY_SECONDS
        self.account_notice_var.set("Account statuses could not be refreshed. Retrying…")

    def _refresh_account_notice(self) -> None:
        if not self._profile_available():
            self.account_notice_var.set("Account statuses unavailable while restoring the profile.")
            return
        if self._account_status_retry_at is not None:
            self._refresh_account_rows()
            return
        account = self._selected_account()
        if account is None:
            self.account_notice_var.set("")
            return
        try:
            status = self.application.account_status(account.id)
        except Exception:
            self._account_status_refresh_failed()
            return
        self.account_notice_var.set(self._account_notice(account, status))

    @staticmethod
    def _account_notice(account: Account, status: AccountStatus) -> str:
        guidance = {
            AuthorizationState.REQUIRED: "Open this account with Edit and choose Authorize to enable mail checks.",
            AuthorizationState.AUTHORIZING: "Complete sign-in in your browser. Authorization is managed in the account dialog.",
            AuthorizationState.CHECKING: "Checking the saved authorization.",
            AuthorizationState.UNAVAILABLE: "Unlock the operating system credential store, then open this account with Edit and retry the credential check.",
        }.get(status.authorization.state, "")
        return "\n".join(
            value
            for value in (
                f"{account.label}: {ACCOUNT_STATE_LABELS[status.state]}",
                guidance,
                status.authorization.detail,
            )
            if value
        )

    def _selected_account(self) -> Account | None:
        selected = self.account_tree.selection()
        return next(
            (item for item in self.settings.accounts if selected and item.id == selected[0]), None
        )

    def reset_paused_folder(self) -> None:
        if not self._profile_available():
            return
        account = self._selected_account()
        if account is None:
            messagebox.showinfo(
                "Select an account", "Select an email account first.", parent=self.root
            )
            return
        try:
            rows = self.application.paused_scopes(account.id)
        except Exception as exc:
            messagebox.showerror("Could not load paused folders", str(exc), parent=self.root)
            return
        if not rows:
            messagebox.showinfo(
                "No paused folder", "This account has no paused folder.", parent=self.root
            )
            return
        options = "\n".join(
            f"{index}. {row.scope_key}: {row.error}" for index, row in enumerate(rows, 1)
        )
        index = simpledialog.askinteger(
            "Reset paused folder", options, minvalue=1, maxvalue=len(rows), parent=self.root
        )
        if index is None:
            return
        row = rows[index - 1]
        if not messagebox.askyesno(
            "Start a new baseline?",
            "Current messages in this folder will be skipped. "
            "Any downloads not yet accepted will be cancelled. "
            "Completed files remain available in Archive activity.",
            parent=self.root,
        ):
            return
        try:
            self.application.reset_scope_baseline(row.source_id, row.scope_key)
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Could not reset folder", str(exc), parent=self.root)

    def add_account(self) -> None:
        dialog = AccountDialog(
            self.root,
            self.settings.default_poll_minutes,
            read_service_account=self.application.read_service_account,
            editor=self.application.account_editor(),
        )
        self.root.wait_window(dialog)
        self._account_editor_closed(dialog)

    def edit_account(self) -> None:
        if not self._profile_available():
            return
        account = self._selected_account()
        if not account:
            messagebox.showinfo(
                "Select an account", "Select an email account first.", parent=self.root
            )
            return
        if self.application.authorization_in_progress(account.id):
            messagebox.showinfo(
                "Authorization in progress",
                "Finish or cancel this account's browser authorization before editing it.",
                parent=self.root,
            )
            return
        dialog = AccountDialog(
            self.root,
            self.settings.default_poll_minutes,
            account,
            read_service_account=self.application.read_service_account,
            editor=self.application.account_editor(account.id),
        )
        self.root.wait_window(dialog)
        self._account_editor_closed(dialog)

    def _account_editor_closed(self, dialog: AccountDialog) -> None:
        if not dialog.result:
            return
        self.settings = self.application.settings
        self._account_selection_after_refresh = dialog.result.account.id
        self.refresh_all()
        if not self._archive_running:
            self.progress_var.set(f"{dialog.result.account.label}: Account saved.")

    def remove_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo(
                "Select an account", "Select an email account first.", parent=self.root
            )
            return
        if self.application.authorization_in_progress(account.id):
            messagebox.showinfo(
                "Authorization in progress",
                "Finish or cancel this account's browser authorization before removing it.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Remove email account",
            f'Remove "{account.label}" from MailArchive?\n\nFiles already archived will be kept.',
            parent=self.root,
        ):
            return
        try:
            self.settings = self.application.delete_account(account.id)
            self.refresh_all()
        except Exception as exc:
            messagebox.showerror("Email account not removed", str(exc), parent=self.root)

    def _selected_rule(self) -> Rule | None:
        selected = self.rule_tree.selection()
        return next(
            (item for item in self.settings.rules if selected and item.id == selected[0]), None
        )

    def add_rule(self) -> None:
        dialog = RuleDialog(self.root, accounts=self.settings.accounts, save_rule=self._save_rule)
        self.root.wait_window(dialog)
        if dialog.result:
            self.refresh_all()
            self.rule_tree.selection_set(dialog.result.id)

    def edit_rule(self) -> None:
        if not self._profile_available():
            return
        rule = self._selected_rule()
        if not rule:
            messagebox.showinfo("Select a rule", "Select a rule first.", parent=self.root)
            return
        dialog = RuleDialog(
            self.root,
            rule,
            accounts=self.settings.accounts,
            save_rule=lambda candidate: self._save_rule(candidate, replacing_id=rule.id),
        )
        self.root.wait_window(dialog)
        if dialog.result:
            self.refresh_all()
            self.rule_tree.selection_set(dialog.result.id)

    def _save_rule(self, rule: Rule, *, replacing_id: str | None = None) -> None:
        rules = self.application.settings.rules
        if replacing_id is None:
            rules.append(rule)
        else:
            index = next((i for i, item in enumerate(rules) if item.id == replacing_id), None)
            if index is None:
                raise ValueError("The archive rule no longer exists.")
            rules[index] = rule
        self.settings = self.application.save_rules(rules)

    def remove_rule(self) -> None:
        rule = self._selected_rule()
        if not rule:
            messagebox.showinfo("Select a rule", "Select a rule first.", parent=self.root)
            return
        if messagebox.askyesno("Remove rule", f'Remove the rule "{rule.name}"?', parent=self.root):
            rules = self.settings.rules.copy()
            rules.remove(rule)
            self._commit_rules(rules)

    def move_rule(self, offset: int) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        index = self.settings.rules.index(rule)
        target = index + offset
        if not 0 <= target < len(self.settings.rules):
            return
        rules = self.settings.rules.copy()
        rules[index], rules[target] = rules[target], rules[index]
        if self._commit_rules(rules):
            self.rule_tree.selection_set(rule.id)

    def _commit_rules(self, rules: list[Rule]) -> bool:
        try:
            self.settings = self.application.save_rules(rules)
        except Exception as exc:
            messagebox.showerror("Rules not saved", str(exc), parent=self.root)
            return False
        self.refresh_all()
        return True

    def _saved_settings_form_values(self) -> SettingsFormValues:
        return SettingsFormValues(
            state_database_path=str(self.application.database_path),
            default_poll_minutes=str(self.settings.default_poll_minutes),
            start_at_login=self.settings.start_at_login,
            minimize_to_tray=self.settings.minimize_to_tray,
            warn_on_error=self.settings.warn_on_error,
            archive_timezone=self.settings.archive_timezone,
        )

    def save_settings(self, field: str | None = None) -> None:
        if self._closing or self._saving_settings:
            return
        all_variables = {
            "state_database_path": self.database_var,
            "default_poll_minutes": self.poll_var,
            "start_at_login": self.startup_var,
            "minimize_to_tray": self.minimize_var,
            "warn_on_error": self.warning_var,
            "archive_timezone": self.timezone_var,
        }
        variables = all_variables if field is None else {field: all_variables[field]}
        saved_values = self._saved_settings_form_values()
        values = replace(saved_values, **{name: var.get() for name, var in variables.items()})
        if values == saved_values:
            return

        def sync_fields(fields: dict[str, tk.Variable] = variables) -> None:
            saved = self._saved_settings_form_values()
            for name, variable in fields.items():
                variable.set(getattr(saved, name))

        self._saving_settings = True
        try:
            update = prepare_settings_update(
                self.settings,
                values,
                current_database_path=self.application.database_path,
            )
            if update.settings == self.settings and not update.database_changed:
                sync_fields()
                return
            if update.database_changed and update.settings != self.settings:
                raise ValueError("Change the database separately from other settings.")
            if update.database_changed:
                self._request_profile_switch(update)
                return
            self._apply_settings_update(update)
            sync_fields(variables)
            self.refresh_all()
            if update.database_changed:
                self.refresh_log(reset_page=True)
        except Exception as exc:
            sync_fields()
            messagebox.showerror("Settings not saved", str(exc), parent=self.root)
        finally:
            self._saving_settings = self._profile_switch_update is not None

    def _request_profile_switch(self, update: SettingsUpdate) -> None:
        self._profile_switch_update = update
        try:
            self.application.request_profile_switch(
                update.database_path, lambda result: self._finish_profile_switch(update, result)
            )
        except Exception:
            self._profile_switch_update = None
            self._refresh_profile_controls()
            raise
        self._refresh_profile_controls()
        self._refresh_monitoring_controls()
        self._refresh_open_activity()

    def _refresh_open_activity(self) -> None:
        activity = getattr(self, "activity_dialog", None)
        if activity is not None and activity.winfo_exists():
            activity.refresh()

    def _refresh_profile_controls(self) -> None:
        unavailable = not self._profile_available()
        pending = self._profile_switch_update is not None
        self.profile_switch_status_var.set(
            "Opening the selected profile…"
            if pending
            else "The previous profile is unavailable. Restore its database or select another profile."
            if unavailable
            else ""
        )
        for tab in (self.dashboard_tab, self.accounts_tab, self.rules_tab, self.log_tab):
            self.notebook.tab(tab, state="disabled" if unavailable or pending else "normal")
            for container in tab.winfo_children():
                for control in container.winfo_children():
                    kind = control.winfo_class()
                    if kind in {"TButton", "TCombobox"}:
                        disabled = (
                            unavailable
                            or pending
                            or (control is self.update_button and self._checking_for_updates)
                        )
                        if not disabled and control in (
                            self.log_previous_button,
                            self.log_next_button,
                        ):
                            # Paging owns its enabled state; refresh_log restores it after recovery.
                            continue
                        control.configure(
                            state="disabled"
                            if disabled
                            else "readonly"
                            if kind == "TCombobox"
                            else "normal"
                        )
        for index in range(self.settings_pages.index("end")):
            if index != 1:
                self.settings_pages.tab(
                    index, state="disabled" if unavailable or pending else "normal"
                )
        for viewport in (self.general_settings_scroll, self.advanced_settings_scroll):
            for control in viewport.content.winfo_children():
                kind = control.winfo_class()
                if kind in {"TEntry", "TCheckbutton", "TCombobox"}:
                    disabled = pending or (unavailable and viewport is self.general_settings_scroll)
                    control.configure(
                        state="disabled"
                        if disabled
                        else "readonly"
                        if kind == "TCombobox"
                        else "normal"
                    )
        self._refresh_check_controls()

    def _refresh_check_controls(self, progress: RunProgress | None = None) -> None:
        current = progress or self._check_progress or self._other_progress
        if not self._profile_available() or self._profile_switch_update is not None:
            self.check_button.configure(state="disabled")
        elif self._check_id is not None:
            stopping = self._stop_requested or (
                current is not None and current.state == ExecutionState.STOPPING
            )
            self.check_button.configure(
                state="disabled" if stopping else "normal",
                text="Stopping" if stopping else "Stop check",
            )
        elif self._archive_running:
            stopping = current is not None and current.state == ExecutionState.STOPPING
            self.check_button.configure(
                state="disabled", text="Stopping" if stopping else "Checking"
            )
        else:
            self.check_button.configure(state="normal", text="Check mail now")

    def _finish_profile_switch(
        self, update: SettingsUpdate, result: BackgroundResult[Settings]
    ) -> None:
        if self._closing or self._profile_switch_update is not update:
            return
        self._profile_switch_update = None
        self._saving_settings = False
        self._refresh_profile_controls()
        self.settings = self.application.settings
        self.database_var.set(str(self.application.database_path))
        if result.error is not None:
            messagebox.showerror("Settings not saved", str(result.error), parent=self.root)
        else:
            self.poll_var.set(str(self.settings.default_poll_minutes))
            self.startup_var.set(self.settings.start_at_login)
            self.minimize_var.set(self.settings.minimize_to_tray)
            self.warning_var.set(self.settings.warn_on_error)
            self.timezone_var.set(self.settings.archive_timezone)
            self.refresh_log(reset_page=True)
        self.refresh_all()
        self._refresh_open_activity()

    def _apply_settings_update(self, update: SettingsUpdate) -> None:
        self.settings = self.application.save_settings(update.settings)

    def _check_clicked(self) -> None:
        if self._check_id is None:
            self.run_now()
            return
        if self._stop_requested:
            return
        try:
            self.application.stop_check(self._check_id)
        except Exception as exc:
            messagebox.showerror("Could not stop mail check", str(exc), parent=self.root)
            return
        self._stop_requested = True
        self._render_progress(
            RunProgress(
                "Stopping the mail check.",
                execution_id=self._check_id,
                origin="check",
                state=ExecutionState.STOPPING,
            )
        )

    def _toggle_automatic_monitoring(self) -> None:
        try:
            self.settings = self.application.set_automatic_monitoring_paused(
                not self.application.settings.automatic_monitoring_paused
            )
        except Exception as exc:
            messagebox.showerror("Could not change automatic checks", str(exc), parent=self.root)
        self._refresh_monitoring_controls()

    def _refresh_monitoring_controls(self) -> None:
        state = self.application.automatic_monitoring_state()
        if state == self._monitoring_state:
            return
        previous = self._monitoring_state
        self._monitoring_state = state
        paused = state != AutomaticMonitoringState.ACTIVE
        self.automatic_button.configure(
            text="Resume automatic checks" if paused else "Pause automatic checks",
            state="disabled" if state == AutomaticMonitoringState.UNAVAILABLE else "normal",
        )
        self.automatic_status_var.set(
            {
                AutomaticMonitoringState.ACTIVE: "Automatic checks active",
                AutomaticMonitoringState.PAUSING: "Automatic checks pausing",
                AutomaticMonitoringState.PAUSED: "Automatic checks paused",
                AutomaticMonitoringState.UNAVAILABLE: "Automatic checks unavailable — restore the profile or select another database",
            }[state]
        )
        self.tray.set_monitoring_paused(paused)
        self._refresh_profile_controls()
        if state == AutomaticMonitoringState.UNAVAILABLE:
            self.archive_summary.set("Work queue unavailable")
            self.account_notice_var.set("Account statuses unavailable while restoring the profile.")
            self.log_summary_var.set("Activity log unavailable while restoring the profile.")
        elif previous == AutomaticMonitoringState.UNAVAILABLE:
            self.settings = self.application.settings
            # A fresh worker can reopen the same path with the same status revision.
            # Availability, rather than those cache keys, completes UI recovery.
            self._account_status_revision = None
            self._account_status_retry_at = None
            self.refresh_all()
            self.refresh_log()
            self._refresh_open_activity()

    def run_now(self) -> None:
        if self._archive_running:
            return
        try:
            check_id = self.application.check_now()
        except Exception as exc:
            messagebox.showerror("Could not check mail", str(exc), parent=self.root)
            return
        if check_id is None:
            return
        self._check_id = check_id
        self._stop_requested = False
        self._check_progress = RunProgress(
            "Waiting for the mail check to start.",
            execution_id=check_id,
            origin="check",
            state=ExecutionState.QUEUED,
        )
        self._render_progress(self._check_progress)

    def run_rule_history_dialog(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            messagebox.showinfo("Select a rule", "Select a rule first.", parent=self.root)
            return
        if not rule.enabled:
            messagebox.showinfo("Rule disabled", "Enable the rule first.", parent=self.root)
            return
        dialog = RangeDialog(self.root, rule, local_timezone_name(self.settings.archive_timezone))
        self.root.wait_window(dialog)
        selection = dialog.result
        if selection is None:
            return
        try:
            self.application.apply_rule_to_past_mail(
                rule.id, selection.start, selection.end, selection.timezone_name
            )
        except Exception as exc:
            messagebox.showerror("Could not start past-mail operation", str(exc), parent=self.root)
            return
        self.show_archive_activity()

    def show_archive_activity(self) -> None:
        dialog = getattr(self, "activity_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            return
        self.activity_dialog = ArchiveActivityDialog(
            self.root, self.application, self._open_output_path
        )

    def on_run_progress(self, progress: RunProgress) -> None:
        self.post_ui(lambda: self._display_progress(progress))

    def _display_progress(self, progress: RunProgress) -> None:
        if self._closing:
            return
        if progress.execution_id is not None:
            previous = self._seen_progress.get(progress.execution_id)
            if previous is not None and (
                not previous.active or progress.sequence <= previous.sequence
            ):
                return
            self._seen_progress[progress.execution_id] = progress
            if len(self._seen_progress) > 128:
                del self._seen_progress[next(iter(self._seen_progress))]
            if progress.origin == "check":
                if progress.execution_id != self._check_id:
                    return
                self._check_progress = progress
                if not progress.active:
                    self._check_id = None
                    self._check_progress = None
                    self._stop_requested = False
            elif progress.active:
                self._other_progress = progress
            elif (
                self._other_progress is not None
                and self._other_progress.execution_id == progress.execution_id
            ):
                self._other_progress = None
        refresh_accounts = not progress.active
        if self._check_progress is not None:
            progress = self._check_progress
            if self._stop_requested:
                progress = RunProgress(
                    "Stopping the mail check.",
                    execution_id=self._check_id,
                    origin="check",
                    state=ExecutionState.STOPPING,
                )
        elif self._other_progress is not None:
            progress = self._other_progress
        self._render_progress(progress)
        if refresh_accounts and self._profile_switch_update is None:
            self._refresh_archive_summary(force=True)
            self._refresh_account_rows()

    def _render_progress(self, progress: RunProgress) -> None:
        self.progress_var.set(progress.message)
        if progress.active:
            if not self._archive_running:
                self._archive_running = True
                self._run_event_level = EventLevel.INFO
                self._run_started_at = time.monotonic()
                self.progress_bar.pack(side="right", padx=(8, 0), anchor="n")
                self.progress_bar.start(15)
                self.tray.set_state("busy", "MailArchive - checking mail")
                self._update_run_elapsed()
            if progress.origin == "check" and progress.state != ExecutionState.STOPPING:
                self.check_button.configure(state="normal", text="Stop check")
            else:
                self.check_button.configure(
                    state="disabled",
                    text="Stopping" if progress.state == ExecutionState.STOPPING else "Checking",
                )
        else:
            self._archive_running = False
            self.progress_bar.stop()
            self.progress_bar.pack_forget()
            if self._progress_timer is not None:
                self.root.after_cancel(self._progress_timer)
                self._progress_timer = None
            self.check_button.configure(state="normal", text="Check mail now")
            if progress.state == ExecutionState.FAILED or self._run_event_level == EventLevel.ERROR:
                self.tray.set_state("error", "MailArchive - problem detected")
            elif self._run_event_level == EventLevel.WARNING:
                self.tray.set_state("warning", "MailArchive - attention required")
            else:
                self.tray.set_state("ok", "MailArchive - ready")
        self._refresh_check_controls(progress)
        self._refresh_monitoring_controls()

    def _update_run_elapsed(self) -> None:
        elapsed = int(time.monotonic() - self._run_started_at)
        self.elapsed_var.set(f"{elapsed // 60:02d}:{elapsed % 60:02d} elapsed")
        self._progress_timer = self.root.after(1000, self._update_run_elapsed)

    def on_service_event(self, event: ServiceEvent) -> None:
        self.post_ui(lambda: self._display_event(event))

    def post_ui(self, callback: Callable[[], None]) -> None:
        if not self._closing:
            self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        if self._closing:
            return
        try:
            self.application.dispatch_callbacks()
            while True:
                try:
                    callback = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception:
                    logger.exception("A queued user-interface callback failed.")
            self._refresh_monitoring_controls()
            if not self._profile_available():
                return
            self._refresh_archive_summary()
            revision = (self.application.database_path, self.application.account_statuses.revision)
            if (
                revision != self._account_status_revision
                or self._account_status_retry_at is not None
            ):
                self._refresh_account_rows()
        finally:
            if not self._closing:
                self.root.after(100, self._drain_ui_queue)

    def _display_event(self, event: ServiceEvent) -> None:
        if not self._profile_available():
            return
        self._refresh_archive_summary()
        if event.account_id:
            self._refresh_account_rows()
        if not self._archive_running:
            self.progress_var.set(event.message)
        # Leave an older page in place while new events arrive in the background.
        if not self._log_offset:
            self.refresh_log()
        if event.level == EventLevel.ERROR:
            if self._archive_running:
                self._run_event_level = EventLevel.ERROR
            self.tray.set_state("error", "MailArchive - problem detected")
            if self.settings.warn_on_error:
                self.tray.notify(event.message)
        elif event.level == EventLevel.WARNING:
            if self._archive_running and self._run_event_level != EventLevel.ERROR:
                self._run_event_level = EventLevel.WARNING
            self.tray.set_state("warning", "MailArchive - attention required")
            if self.settings.warn_on_error:
                self.tray.notify(event.message)
        elif event.level == EventLevel.SUCCESS and not self._archive_running:
            self.tray.set_state("ok", "MailArchive - ready")

    @staticmethod
    def _open_output_path(path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"Saved output is unavailable: {path}")
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def show(self) -> None:
        if self._closing:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _restore_after_tray_loss(self) -> None:
        if not self._closing and self.root.state() == "withdrawn":
            self.show()

    def offer_desktop_integration(self) -> None:
        if self.desktop_integration is not None:
            self.desktop_integration.offer_once()

    def hide_to_tray(self) -> None:
        if self._closing:
            return
        if self.desktop_integration is not None and self.desktop_integration.busy:
            self.desktop_integration.show_busy()
            return
        self._save_focused_setting()
        if self.settings.minimize_to_tray and self.tray.safe_to_hide:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        if self._closing:
            return
        if self.desktop_integration is not None and self.desktop_integration.busy:
            self.desktop_integration.show_busy()
            return
        self._save_focused_setting()
        self._closing = True
        self.progress_var.set("Closing MailArchive — waiting for current work to stop.")
        self.check_button.configure(state="disabled")
        self._finish_close()

    def _finish_close(self) -> None:
        try:
            closed = self.application.close(timeout=0)
        except Exception as exc:
            self.progress_var.set(f"Could not close MailArchive: {exc}. Retrying...")
            self.root.after(1000, self._finish_close)
            return
        if not closed:
            self.root.deiconify()
            self.root.after(100, self._finish_close)
            return
        failures = tuple(self.application.shutdown_errors)
        if failures:
            messagebox.showwarning(
                "MailArchive closed with recovery pending",
                "All running work has stopped, but some work status could not be saved. "
                "The next start will recover unfinished work.\n\n" + "\n".join(failures),
                parent=self.root,
            )
        self.tray.stop()
        self.root.destroy()
