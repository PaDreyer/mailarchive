from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mailarchive.account_form import AccountSubmission
from mailarchive.config import ConfigStore
from mailarchive.credential_data import account_credential_lock, store_account_credentials
from mailarchive.credentials import CredentialStore
from mailarchive.dialogs import AccountDialog, RuleDialog
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
    _condition_summary,
    _label_for,
)


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
        self.state = ArchiveState(config_store.state_database_path(settings))
        self.service = ArchiveService(credential_store, self.state, self.on_service_event)
        self.runner = BackgroundRunner(self.service, lambda: self.settings)
        self._closing = False
        self._authorizing_account_ids: set[str] = set()

        root.title("MailArchive")
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
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left", padx=(22, 0)
        )
        ttk.Button(header, text="Archive now", command=self.run_now).pack(side="right")

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
        ttk.Label(
            self.dashboard_tab,
            text="Note: Emails on the server are never deleted, moved, or marked as read.",
            foreground="#18794e",
        ).pack(anchor="w", pady=(10, 0))

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
            text="Rules are evaluated from top to bottom. The first match determines the destination and save mode.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        columns = ("order", "name", "condition", "destination", "mode", "active")
        self.rule_tree = ttk.Treeview(
            self.rules_tab, columns=columns, show="headings", selectmode="browse"
        )
        for key, title, width in [
            ("order", "#", 40),
            ("name", "Name", 160),
            ("condition", "When", 260),
            ("destination", "Destination", 140),
            ("mode", "Save as", 150),
            ("active", "Status", 70),
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
        ttk.Entry(general_page, textvariable=self.archive_var).grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8), pady=(6, 0)
        )
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
        ttk.Entry(general_page, textvariable=self.poll_var, width=12).grid(
            row=3,
            column=0,
            sticky="w",
        )
        ttk.Label(
            general_page,
            text="Used by every account without its own polling override.",
            style="Sub.TLabel",
        ).grid(row=3, column=1, columnspan=2, sticky="w")
        self.startup_var = tk.BooleanVar(value=self.settings.start_at_login)
        self.minimize_var = tk.BooleanVar(value=self.settings.minimize_to_tray)
        self.warning_var = tk.BooleanVar(value=self.settings.warn_on_error)
        ttk.Checkbutton(
            general_page, text="Start automatically at login", variable=self.startup_var
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(22, 6))
        ttk.Checkbutton(
            general_page,
            text="Keep running in the notification area when closed",
            variable=self.minimize_var,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Checkbutton(
            general_page,
            text="Show a desktop notification when an error occurs",
            variable=self.warning_var,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=6)

        ttk.Label(advanced_page, text="SQLite database file").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.database_var = tk.StringVar(
            value=str(self.config_store.state_database_path(self.settings))
        )
        ttk.Entry(advanced_page, textvariable=self.database_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0)
        )
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
                "This database tracks which messages were already archived. When the path "
                "changes, the existing processing history is copied into the selected database."
            ),
            style="Sub.TLabel",
            wraplength=720,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        ttk.Button(self.settings_tab, text="Save settings", command=self.save_settings).grid(
            row=2, column=0, sticky="w", pady=(18, 0)
        )
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.columnconfigure(1, weight=1)
        self.settings_tab.rowconfigure(1, weight=1)
        general_page.columnconfigure(0, weight=1)
        general_page.columnconfigure(1, weight=1)
        advanced_page.columnconfigure(0, weight=1)

    def _build_log(self) -> None:
        ttk.Label(self.log_tab, text="Activity log", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            self.log_tab,
            text="Successful checks and clear error messages appear here.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        columns = ("time", "level", "message")
        self.log_tree = ttk.Treeview(self.log_tab, columns=columns, show="headings")
        for key, title, width in [
            ("time", "Time", 130),
            ("level", "Status", 90),
            ("message", "Message", 650),
        ]:
            self.log_tree.heading(key, text=title)
            self.log_tree.column(key, width=width, anchor="w")
        self.log_tree.pack(fill="both", expand=True)

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
                    _condition_summary(rule),
                    rule.destination,
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
        dialog = RuleDialog(self.root, self.settings.archive_root)
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
        dialog = RuleDialog(self.root, self.settings.archive_root, rule)
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

    def choose_state_database(self) -> None:
        current = Path(self.database_var.get()).expanduser()
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="Choose SQLite database",
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

    def use_default_state_database(self) -> None:
        self.database_var.set(str(self.config_store.default_state_database_path))

    def save_settings(self) -> None:
        try:
            update = prepare_settings_update(
                self.settings,
                SettingsFormValues(
                    archive_root=self.archive_var.get(),
                    state_database_path=self.database_var.get(),
                    default_poll_minutes=self.poll_var.get(),
                    start_at_login=bool(self.startup_var.get()),
                    minimize_to_tray=bool(self.minimize_var.get()),
                    warn_on_error=bool(self.warning_var.get()),
                ),
                current_database_path=self.state.database_path,
                default_database_path=self.config_store.default_state_database_path,
            )
            update.archive_root.mkdir(parents=True, exist_ok=True)

            try:
                if update.database_changed:
                    self.state = self.service.relocate_state_database(update.database_path)
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
            self.database_var.set(str(update.database_path))
            self.refresh_all()
            messagebox.showinfo("Saved", "The settings have been saved.")
        except Exception as exc:
            messagebox.showerror("Settings not saved", str(exc))

    def _persist(self) -> None:
        self.config_store.save(self.settings)
        self.refresh_all()

    def run_now(self) -> None:
        self.status_var.set("Starting archive run...")
        self.tray.set_state("busy", "MailArchive - checking mail")
        self.runner.run_now()

    def on_service_event(self, event: ServiceEvent) -> None:
        self.post_ui(lambda: self._display_event(event))

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

    def _display_event(self, event: ServiceEvent) -> None:
        labels = {
            EventLevel.INFO: "Info",
            EventLevel.SUCCESS: "Success",
            EventLevel.WARNING: "Warning",
            EventLevel.ERROR: "Error",
        }
        self.status_var.set(event.message)
        self.log_tree.insert(
            "",
            0,
            values=(
                event.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                labels[event.level],
                event.message,
            ),
        )
        children = self.log_tree.get_children()
        if len(children) > 300:
            self.log_tree.delete(*children[300:])
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
        if self.settings.minimize_to_tray and self.tray.safe_to_hide:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.runner.stop()
        self.tray.stop()
        self.root.destroy()
