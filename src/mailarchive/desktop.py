from __future__ import annotations

import os
import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mailarchive import __version__
from mailarchive.account_form import AccountSubmission
from mailarchive.activity_log import ActivityLog
from mailarchive.config import ConfigStore
from mailarchive.credential_data import account_credential_lock, store_account_credentials
from mailarchive.credentials import CredentialStore
from mailarchive.dialogs import AccountDialog, RuleDialog
from mailarchive.migrations import DATABASE_SCHEMA_VERSION
from mailarchive.models import Account, AuthMode, MailField, MailProvider, Rule, Settings
from mailarchive.oauth import authorize_account
from mailarchive.platform_integration import set_start_at_login
from mailarchive.runner import BackgroundRunner
from mailarchive.service import ArchiveService, EventLevel, ServiceEvent
from mailarchive.settings_form import SettingsFormValues, prepare_settings_update
from mailarchive.storage import ArchiveState
from mailarchive.tray import TrayController
from mailarchive.ui_text import (
    PROVIDER_LABELS,
    SAVE_LABELS,
    _account_scope_summary,
    _condition_summary,
    _destination_summary,
    _label_for,
)
from mailarchive.updates import Release, UpdateError, check_for_update

LOG_FILTERS = {
    "Last 50": None,
    "Last 24 hours": timedelta(hours=24),
    "Last 7 days": timedelta(days=7),
    "Last 30 days": timedelta(days=30),
    "All time": None,
}
LOG_PAGE_SIZE = 50


class DesktopApp:
    def __init__(
        self,
        root: tk.Tk,
        config_store: ConfigStore,
        settings: Settings,
        credential_store: CredentialStore,
    ) -> None:
        self.root = root
        self.config_store = config_store
        self.settings = settings
        self.credential_store = credential_store
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.activity_log = ActivityLog(config_store.data_dir / "activity-log.sqlite3")
        self.state = ArchiveState(config_store.state_database_path(settings))
        self.service = ArchiveService(credential_store, self.state, self.on_service_event)
        self.runner = BackgroundRunner(self.service, lambda: self.settings)
        self._closing = False
        self._saving_settings = False
        self._setting_entry_fields: dict[ttk.Entry, str] = {}
        self._checking_for_updates = False
        self._authorizing_account_ids: set[str] = set()

        root.title(f"MailArchive {__version__}")
        root.geometry("980x680")
        root.minsize(820, 580)
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._configure_style()
        self._build_ui()
        self.tray = TrayController(self.post_ui, self.show, self.run_now, self.quit)
        self.root.after(100, self._drain_ui_queue)
        try:
            set_start_at_login(self.settings.start_at_login)
        except Exception as exc:
            self.on_service_event(
                ServiceEvent(EventLevel.WARNING, f"Could not configure start at login: {exc}")
            )
        self.refresh_all()
        self.refresh_log()
        self.runner.start()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Sub.TLabel", foreground="#555555", font=("Segoe UI", 10))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Treeview", rowheight=28)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=(22, 18))
        container.pack(fill="both", expand=True)
        header = ttk.Frame(container)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="MailArchive", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text=f"v{__version__}", style="Sub.TLabel").pack(side="left", padx=(8, 0))
        ttk.Button(header, text="Quit", command=self.quit).pack(side="right")
        ttk.Button(header, text="Archive now", command=self.run_now).pack(side="right", padx=(0, 8))

        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(
            container,
            textvariable=self.status_var,
            style="Status.TLabel",
            anchor="w",
            justify="left",
            width=1,
            wraplength=720,
        )
        status_label.pack(fill="x", pady=(0, 16))
        status_label.bind(
            "<Configure>",
            lambda event: status_label.configure(wraplength=max(event.width, 1)),
        )

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
                ("Archive folder", self.archive_summary),
            ]
        ):
            card = ttk.LabelFrame(summary, text=title, padding=14)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            ttk.Label(
                card, textvariable=variable, font=("Segoe UI", 12, "bold"), wraplength=260
            ).pack(anchor="w")
            summary.columnconfigure(column, weight=1)
        actions = ttk.Frame(self.dashboard_tab)
        actions.pack(fill="x", pady=24)
        ttk.Button(actions, text="Add email account", command=self.add_account).pack(side="left")
        ttk.Button(actions, text="Add rule", command=self.add_rule).pack(side="left", padx=8)
        ttk.Button(actions, text="Open archive folder", command=self.open_archive).pack(side="left")
        self.update_button = ttk.Button(
            actions, text="Check for updates", command=self.check_for_updates
        )
        self.update_button.pack(side="right")
        ttk.Label(
            self.dashboard_tab,
            text="Note: Emails on the server are never deleted, moved, or marked as read.",
            foreground="#18794e",
        ).pack(anchor="w", pady=(10, 0))

    def check_for_updates(self) -> None:
        if self._checking_for_updates:
            return
        self._checking_for_updates = True
        self.update_button.configure(state="disabled", text="Checking...")

        def check() -> None:
            try:
                release = check_for_update()
            except UpdateError as exc:
                self.post_ui(lambda error=str(exc): self._finish_update_check(error=error))
            else:
                self.post_ui(lambda: self._finish_update_check(release))

        try:
            threading.Thread(target=check, name="MailArchive-UpdateCheck", daemon=True).start()
        except RuntimeError as exc:
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
            "Quit MailArchive before running the Windows installer or replacing the Linux AppImage.",
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
            ("user", "User", 220),
            ("interval", "Polling", 100),
            ("active", "Status", 90),
        ]:
            self.account_tree.heading(key, text=title)
            self.account_tree.column(key, width=width, anchor="w")
        self.account_tree.pack(fill="both", expand=True)
        self.account_tree.bind("<Double-1>", lambda event: self.edit_account())
        buttons = ttk.Frame(self.accounts_tab)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Add", command=self.add_account).pack(side="left")
        ttk.Button(buttons, text="Edit", command=self.edit_account).pack(side="left", padx=6)
        ttk.Button(buttons, text="Remove", command=self.remove_account).pack(side="left")
        ttk.Button(
            buttons,
            text="Authorize",
            command=self.authorize_selected_account,
        ).pack(side="left", padx=(16, 0))

    def _build_rules(self) -> None:
        ttk.Label(self.rules_tab, text="Archive rules", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.rules_tab,
            text="Rules are evaluated from top to bottom for each email account. The first match determines the destination and save mode.",
            style="Sub.TLabel",
            wraplength=720,
        ).pack(anchor="w", pady=(4, 14))
        columns = ("order", "name", "accounts", "condition", "destination", "mode", "active")
        self.rule_tree = ttk.Treeview(
            self.rules_tab, columns=columns, show="headings", selectmode="browse"
        )
        for key, title, width in [
            ("order", "#", 40),
            ("name", "Name", 140),
            ("accounts", "Email accounts", 170),
            ("condition", "When", 210),
            ("destination", "Destination", 120),
            ("mode", "Save as", 130),
            ("active", "Status", 60),
        ]:
            self.rule_tree.heading(key, text=title)
            self.rule_tree.column(key, width=width, anchor="w")
        self.rule_tree.pack(fill="both", expand=True)
        self.rule_tree.bind("<Double-1>", lambda event: self.edit_rule())
        buttons = ttk.Frame(self.rules_tab)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Add", command=self.add_rule).pack(side="left")
        ttk.Button(buttons, text="Edit", command=self.edit_rule).pack(side="left", padx=6)
        ttk.Button(buttons, text="Remove", command=self.remove_rule).pack(side="left")
        ttk.Button(buttons, text="Move up", command=lambda: self.move_rule(-1)).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(buttons, text="Move down", command=lambda: self.move_rule(1)).pack(side="right")

    def _build_settings(self) -> None:
        ttk.Label(self.settings_tab, text="Settings", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        settings_pages = ttk.Notebook(self.settings_tab)
        settings_pages.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(18, 0))
        general_page = ttk.Frame(settings_pages, padding=16)
        advanced_page = ttk.Frame(settings_pages, padding=16)
        settings_pages.add(general_page, text="General")
        settings_pages.add(advanced_page, text="Advanced")

        ttk.Label(general_page, text="Archive folder").grid(row=0, column=0, sticky="w")
        self.archive_var = tk.StringVar(value=self.settings.archive_root)
        archive_entry = ttk.Entry(general_page, textvariable=self.archive_var)
        archive_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(6, 0))
        self._bind_setting_entry(archive_entry, "archive_root")
        ttk.Button(general_page, text="Choose...", command=self.choose_archive).grid(
            row=1, column=2, pady=(6, 0)
        )
        ttk.Label(general_page, text="Default polling interval (minutes)").grid(
            row=2,
            column=0,
            sticky="w",
            pady=(22, 6),
        )
        self.poll_var = tk.StringVar(value=str(self.settings.default_poll_minutes))
        poll_entry = ttk.Entry(general_page, textvariable=self.poll_var, width=12)
        poll_entry.grid(
            row=3,
            column=0,
            sticky="w",
        )
        self._bind_setting_entry(poll_entry, "default_poll_minutes")
        ttk.Label(
            general_page,
            text="Used by every account without its own polling override.",
            style="Sub.TLabel",
        ).grid(row=3, column=1, columnspan=2, sticky="w")
        self.startup_var = tk.BooleanVar(value=self.settings.start_at_login)
        self.minimize_var = tk.BooleanVar(value=self.settings.minimize_to_tray)
        self.warning_var = tk.BooleanVar(value=self.settings.warn_on_error)
        ttk.Checkbutton(
            general_page,
            text="Start automatically at login",
            variable=self.startup_var,
            command=lambda: self.save_settings("start_at_login"),
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(22, 6))
        ttk.Checkbutton(
            general_page,
            text="Keep running in the notification area when closed",
            variable=self.minimize_var,
            command=lambda: self.save_settings("minimize_to_tray"),
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Checkbutton(
            general_page,
            text="Show a desktop notification when an error occurs",
            variable=self.warning_var,
            command=lambda: self.save_settings("warn_on_error"),
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(advanced_page, text="Archive processing database").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.database_var = tk.StringVar(
            value=str(self.config_store.state_database_path(self.settings))
        )
        database_entry = ttk.Entry(advanced_page, textvariable=self.database_var)
        database_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0))
        self._bind_setting_entry(database_entry, "state_database_path")
        ttk.Button(
            advanced_page,
            text="Choose...",
            command=self.choose_state_database,
        ).grid(row=1, column=1, pady=(6, 0))
        ttk.Button(
            advanced_page,
            text="Use default",
            command=self.use_default_state_database,
        ).grid(row=1, column=2, padx=(8, 0), pady=(6, 0))
        ttk.Label(
            advanced_page,
            text=(
                "Stores processing history to prevent duplicate archives. Changing this path "
                "copies the existing history into the selected SQLite database."
            ),
            style="Sub.TLabel",
            wraplength=720,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(
            advanced_page,
            text="Activity log database",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(22, 0))
        self.activity_database_var = tk.StringVar(
            value=str(self.activity_log.database_path.expanduser().resolve())
        )
        ttk.Entry(advanced_page, textvariable=self.activity_database_var, state="readonly").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0)
        )
        ttk.Label(
            advanced_page,
            text=(
                "Stores checks, warnings, errors, and authorization results in a separate "
                "SQLite database in the application data folder. Use Clear log... in the "
                "Activity log tab to delete saved entries."
            ),
            style="Sub.TLabel",
            wraplength=720,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(
            advanced_page,
            text=f"Archive processing schema: {DATABASE_SCHEMA_VERSION}",
            style="Sub.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(22, 0))
        ttk.Label(
            advanced_page,
            text=f"Settings schema: {self.settings.schema_version}",
            style="Sub.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(
            self.settings_tab,
            text="Changes are saved automatically. For text fields, press Enter or leave the field.",
            style="Sub.TLabel",
            wraplength=720,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(18, 0))
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.columnconfigure(1, weight=1)
        self.settings_tab.rowconfigure(1, weight=1)
        general_page.columnconfigure(0, weight=1)
        general_page.columnconfigure(1, weight=1)
        advanced_page.columnconfigure(0, weight=1)

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
        ttk.Button(controls, text="Clear log...", command=self.clear_log).pack(side="right")
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
        footer.pack(fill="x", pady=(10, 0))
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

    def refresh_log(self, *, reset_page: bool = False) -> None:
        if reset_page or self.log_filter_var.get() == "Last 50":
            self._log_offset = 0
        duration = LOG_FILTERS[self.log_filter_var.get()]
        since = datetime.now().astimezone() - duration if duration is not None else None
        try:
            page = self.activity_log.page(since=since, offset=self._log_offset, limit=LOG_PAGE_SIZE)
        except (OSError, sqlite3.Error) as exc:
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
        if not messagebox.askyesno(
            "Clear activity log?",
            "Permanently delete all saved activity log entries, including entries outside "
            "the current filter?\n\nArchived files and processing history will be kept.",
            parent=self.root,
        ):
            return
        try:
            self.activity_log.clear()
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Activity log not cleared", str(exc), parent=self.root)
            return
        self.refresh_log(reset_page=True)

    def refresh_all(self) -> None:
        self.account_tree.delete(*self.account_tree.get_children())
        for account in self.settings.accounts:
            self.account_tree.insert(
                "",
                "end",
                iid=account.id,
                values=(
                    account.label,
                    _label_for(PROVIDER_LABELS, account.provider),
                    account.username,
                    f"{account.poll_minutes or self.settings.default_poll_minutes} min"
                    + (" (default)" if account.poll_minutes is None else ""),
                    "Active" if account.enabled else "Paused",
                ),
            )
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
                    _destination_summary(rule, Path(self.settings.archive_root)),
                    _label_for(SAVE_LABELS, rule.save_mode),
                    "Active" if rule.enabled else "Off",
                ),
            )
        self.account_summary.set(str(sum(account.enabled for account in self.settings.accounts)))
        self.rule_summary.set(str(sum(rule.enabled for rule in self.settings.rules)))
        self.archive_summary.set(self.settings.archive_root)

    def _selected_account(self) -> Account | None:
        selected = self.account_tree.selection()
        return next(
            (item for item in self.settings.accounts if selected and item.id == selected[0]), None
        )

    def add_account(self) -> None:
        dialog = AccountDialog(self.root, self.settings.default_poll_minutes)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        try:
            self._commit_account_submission(dialog.result)
        except Exception as exc:
            messagebox.showerror("Email account not saved", str(exc), parent=self.root)

    def edit_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo("Select an account", "Select an email account first.")
            return
        if account.id in self._authorizing_account_ids:
            messagebox.showinfo(
                "Authorization in progress",
                "Finish or cancel this account's browser authorization before editing it.",
                parent=self.root,
            )
            return
        dialog = AccountDialog(self.root, self.settings.default_poll_minutes, account)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        try:
            self._commit_account_submission(dialog.result, replacing=account)
        except Exception as exc:
            messagebox.showerror("Email account not saved", str(exc), parent=self.root)

    def _commit_account_submission(
        self,
        submission: AccountSubmission,
        *,
        replacing: Account | None = None,
    ) -> None:
        """Commit account settings and credentials as one recoverable operation."""
        credential_lock = account_credential_lock(submission.account.id)
        if not credential_lock.acquire(blocking=False):
            raise RuntimeError(
                "This account is currently authorizing or refreshing credentials. Try again "
                "after that operation finishes."
            )
        try:
            previous_accounts = self.settings.accounts.copy()
            changes_credentials = bool(
                submission.credential_updates or submission.replace_credentials
            )
            previous_credential = (
                self.credential_store.get(submission.account.id) if changes_credentials else None
            )
            try:
                if changes_credentials:
                    store_account_credentials(
                        self.credential_store,
                        submission.account,
                        submission.credential_updates,
                        replace=submission.replace_credentials,
                    )
                if replacing is None:
                    self.settings.accounts.append(submission.account)
                else:
                    index = self.settings.accounts.index(replacing)
                    self.settings.accounts[index] = submission.account
                self.config_store.save(self.settings)
            except Exception as exc:
                self.settings.accounts[:] = previous_accounts
                try:
                    if changes_credentials:
                        if previous_credential is None:
                            self.credential_store.delete(submission.account.id)
                        else:
                            self.credential_store.set(
                                submission.account.id,
                                previous_credential,
                            )
                except Exception as rollback_exc:
                    self.refresh_all()
                    raise RuntimeError(
                        f"{exc} Restoring the previous credentials also failed: {rollback_exc}"
                    ) from exc
                self.refresh_all()
                raise
        finally:
            credential_lock.release()
        self.refresh_all()

    def authorize_selected_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo("Select an account", "Select an email account first.")
            return
        if account.id in self._authorizing_account_ids:
            messagebox.showinfo(
                "Authorization in progress",
                "This account already has a browser authorization in progress.",
                parent=self.root,
            )
            return
        if account.provider == MailProvider.GENERIC_IMAP and account.auth_mode == AuthMode.PASSWORD:
            messagebox.showinfo(
                "Authorization not required",
                "This account uses its stored IMAP password and does not have an interactive OAuth sign-in.",
                parent=self.root,
            )
            return
        if account.auth_mode == AuthMode.OAUTH_APPLICATION:
            if account.provider == MailProvider.GMAIL_API:
                detail = (
                    "Google Workspace application access uses the saved service-account key "
                    "and domain-wide delegation automatically. Choose Archive now to test access."
                )
            else:
                detail = (
                    "Microsoft application access uses the saved tenant ID, client ID, and "
                    "client secret automatically. Choose Archive now to test access."
                )
            messagebox.showinfo(
                "Application access",
                detail,
                parent=self.root,
            )
            return
        self.status_var.set(f"{account.label}: Waiting for authorization...")
        self.tray.set_state("busy", "MailArchive - authorization in progress")
        self._authorizing_account_ids.add(account.id)

        def authorize() -> None:
            try:
                authorize_account(account, self.credential_store)
            except Exception as exc:
                self.on_service_event(
                    ServiceEvent(
                        EventLevel.ERROR,
                        f"{account.label}: Authorization failed: {exc}",
                        account.id,
                    )
                )
            else:
                self.on_service_event(
                    ServiceEvent(
                        EventLevel.SUCCESS,
                        f"{account.label}: Authorization completed.",
                        account.id,
                    )
                )
            finally:
                self._authorizing_account_ids.discard(account.id)

        authorization_thread = threading.Thread(
            target=authorize,
            name=f"MailArchive-Authorize-{account.id}",
            daemon=True,
        )
        try:
            authorization_thread.start()
        except Exception:
            self._authorizing_account_ids.discard(account.id)
            raise

    def remove_account(self) -> None:
        account = self._selected_account()
        if not account:
            messagebox.showinfo("Select an account", "Select an email account first.")
            return
        if account.id in self._authorizing_account_ids:
            messagebox.showinfo(
                "Authorization in progress",
                "Finish or cancel this account's browser authorization before removing it.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Remove email account",
            f'Remove "{account.label}" from MailArchive?\n\nFiles already archived will be kept.',
        ):
            return
        credential_lock = account_credential_lock(account.id)
        if not credential_lock.acquire(blocking=False):
            messagebox.showinfo(
                "Account busy",
                "This account is currently refreshing credentials. Try again when it finishes.",
                parent=self.root,
            )
            return
        try:
            index = self.settings.accounts.index(account)
            self.settings.accounts.remove(account)
            try:
                self._persist()
            except Exception as exc:
                self.settings.accounts.insert(index, account)
                self.refresh_all()
                messagebox.showerror("Mailbox not removed", str(exc), parent=self.root)
                return
            try:
                self.credential_store.delete(account.id)
            except Exception as exc:
                messagebox.showwarning(
                    "Email account removed",
                    "The email account was removed, but its stored credentials could not be "
                    f"deleted: {exc}",
                    parent=self.root,
                )
        finally:
            credential_lock.release()

    def _selected_rule(self) -> Rule | None:
        selected = self.rule_tree.selection()
        return next(
            (item for item in self.settings.rules if selected and item.id == selected[0]), None
        )

    def add_rule(self) -> None:
        dialog = RuleDialog(self.root, self.settings.archive_root, accounts=self.settings.accounts)
        self.root.wait_window(dialog)
        if dialog.result:
            catch_all_index = next(
                (
                    index
                    for index, rule in enumerate(self.settings.rules)
                    if rule.conditions and rule.conditions[0].field == MailField.ALL
                ),
                len(self.settings.rules),
            )
            self.settings.rules.insert(catch_all_index, dialog.result)
            self._persist()

    def edit_rule(self) -> None:
        rule = self._selected_rule()
        if not rule:
            messagebox.showinfo("Select a rule", "Select a rule first.")
            return
        dialog = RuleDialog(
            self.root, self.settings.archive_root, rule, accounts=self.settings.accounts
        )
        self.root.wait_window(dialog)
        if dialog.result:
            index = self.settings.rules.index(rule)
            self.settings.rules[index] = dialog.result
            self._persist()

    def remove_rule(self) -> None:
        rule = self._selected_rule()
        if not rule:
            messagebox.showinfo("Select a rule", "Select a rule first.")
            return
        if len(self.settings.rules) == 1:
            messagebox.showerror("Rule required", "At least one archive rule must remain.")
            return
        if messagebox.askyesno("Remove rule", f'Remove the rule "{rule.name}"?'):
            self.settings.rules.remove(rule)
            self._persist()

    def move_rule(self, offset: int) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        index = self.settings.rules.index(rule)
        target = index + offset
        if not 0 <= target < len(self.settings.rules):
            return
        self.settings.rules[index], self.settings.rules[target] = (
            self.settings.rules[target],
            self.settings.rules[index],
        )
        self._persist()
        self.rule_tree.selection_set(rule.id)

    def choose_archive(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, initialdir=self.archive_var.get())
        if selected:
            self.archive_var.set(selected)
            self.save_settings("archive_root")

    def choose_state_database(self) -> None:
        current = Path(self.database_var.get()).expanduser()
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Choose archive processing database",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".sqlite3",
            filetypes=[
                ("SQLite database", "*.sqlite3 *.sqlite *.db"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            self.database_var.set(selected)
            self.save_settings("state_database_path")

    def use_default_state_database(self) -> None:
        self.database_var.set(str(self.config_store.default_state_database_path))
        self.save_settings("state_database_path")

    def _saved_settings_form_values(self) -> SettingsFormValues:
        return SettingsFormValues(
            archive_root=self.settings.archive_root,
            state_database_path=str(self.state.database_path),
            default_poll_minutes=str(self.settings.default_poll_minutes),
            start_at_login=self.settings.start_at_login,
            minimize_to_tray=self.settings.minimize_to_tray,
            warn_on_error=self.settings.warn_on_error,
        )

    def save_settings(self, field: str | None = None) -> None:
        if self._closing or self._saving_settings:
            return
        variables = {
            "archive_root": self.archive_var,
            "state_database_path": self.database_var,
            "default_poll_minutes": self.poll_var,
            "start_at_login": self.startup_var,
            "minimize_to_tray": self.minimize_var,
            "warn_on_error": self.warning_var,
        }
        if field is not None:
            variables = {field: variables[field]}
        saved_values = self._saved_settings_form_values()
        values = replace(saved_values, **{name: var.get() for name, var in variables.items()})
        if values == saved_values:
            return

        def sync_fields() -> None:
            saved = self._saved_settings_form_values()
            for name, variable in variables.items():
                variable.set(getattr(saved, name))

        self._saving_settings = True
        try:
            update = prepare_settings_update(
                self.settings,
                values,
                current_database_path=self.state.database_path,
                default_database_path=self.config_store.default_state_database_path,
            )
            if update.settings == self.settings and not update.database_changed:
                sync_fields()
                return
            if field is None or field == "archive_root":
                update.archive_root.mkdir(parents=True, exist_ok=True)

            try:
                if update.database_changed:
                    self.state = self.service.relocate_state_database(update.database_path)
                if update.startup_changed:
                    set_start_at_login(update.settings.start_at_login)
                self.config_store.save(update.settings)
            except Exception:
                if update.startup_changed:
                    try:
                        set_start_at_login(self.settings.start_at_login)
                    except Exception:
                        pass
                if update.database_changed:
                    try:
                        self.state = self.service.relocate_state_database(
                            update.previous_database_path
                        )
                    except Exception:
                        pass
                raise

            self.settings = update.settings
            sync_fields()
            self.refresh_all()
        except Exception as exc:
            sync_fields()
            messagebox.showerror("Settings not saved", str(exc))
        finally:
            self._saving_settings = False

    def _persist(self) -> None:
        self.config_store.save(self.settings)
        self.refresh_all()

    def run_now(self) -> None:
        self.status_var.set("Starting archive run...")
        self.tray.set_state("busy", "MailArchive - checking mail")
        self.runner.run_now()

    def on_service_event(self, event: ServiceEvent) -> None:
        # Commit before queuing UI work, including events emitted during shutdown.
        error = None
        try:
            self.activity_log.record(event)
        except (OSError, sqlite3.Error) as exc:
            error = str(exc)
        self.post_ui(lambda: self._display_event(event, log_error=error))

    def post_ui(self, callback: Callable[[], None]) -> None:
        if not self._closing:
            self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()
        if not self._closing:
            self.root.after(100, self._drain_ui_queue)

    def _display_event(self, event: ServiceEvent, *, log_error: str | None = None) -> None:
        self.status_var.set(event.message)
        # Leave an older page in place while new events arrive in the background.
        if not self._log_offset:
            self.refresh_log()
        if log_error is not None:
            self.log_summary_var.set(f"Could not save activity log: {log_error}")
        if event.level == EventLevel.ERROR:
            self.tray.set_state("error", "MailArchive - problem detected")
            if self.settings.warn_on_error:
                self.tray.notify(event.message)
        elif event.level == EventLevel.WARNING:
            self.tray.set_state("warning", "MailArchive - attention required")
            if self.settings.warn_on_error:
                self.tray.notify(event.message)
        elif event.level == EventLevel.SUCCESS:
            self.tray.set_state("ok", "MailArchive - ready")

    def open_archive(self) -> None:
        try:
            path = Path(self.settings.archive_root)
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Could not open folder", str(exc))

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self) -> None:
        self._save_focused_setting()
        if self.settings.minimize_to_tray and self.tray.safe_to_hide:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        if self._closing:
            return
        self._save_focused_setting()
        self._closing = True
        self.runner.stop()
        self.tray.stop()
        self.root.destroy()
